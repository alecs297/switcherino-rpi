import hashlib
import json
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pywebostv.connection import WebOSClient
from pywebostv.controls import ApplicationControl, MediaControl, SourceControl, SystemControl

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
PAIRING_PATH = APP_DIR / "pairing.json"

security = HTTPBasic()

app = FastAPI(
    title="LG WebOS TV API",
    description=(
        "HTTPS API for controlling an LG TV from a Raspberry Pi over WebOS. "
        "Protected TV endpoints use HTTP Basic authentication."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


class ActionRequest(BaseModel):
    action: Literal["turn_on", "turn_off", "change_source", "game", "default"] = Field(
        ...,
        description=(
            "Action to perform.\n\n"
            "- **turn_on**: Wake the TV over the network if `tv_mac` is configured\n"
            "- **turn_off**: Turn the TV off through WebOS\n"
            "- **change_source**: Switch the TV to a configured or explicit source\n"
            "- **game**: Switch to `pc_target`, optionally raise volume\n"
            "- **default**: Switch to `default_target`, optionally lower volume"
        ),
        examples=["game"],
    )
    target: str | None = Field(
        default=None,
        description=(
            "Target source.\n\n"
            "Accepted values:\n"
            "- configured alias: `default` or `pc`\n"
            "- WebOS source id, such as `HDMI_1`\n"
            "- source label, such as `HDMI 1`\n\n"
            "Used by `change_source` and optionally by `turn_on`.\n"
            "Ignored by `game` and `default`."
        ),
        examples=["HDMI_1"],
    )


def detect_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def generate_admin_key() -> str:
    return secrets.token_hex(32)


def write_default_config_and_exit() -> None:
    local_ip = detect_local_ip()

    config = {
        "host": "0.0.0.0",
        "port": 8443,
        "admin_username": "admin",
        "admin_key": generate_admin_key(),
        "default_target": "HDMI_1",
        "pc_target": "HDMI_2",
        "tv_mac": "",
        "cert_file": str(APP_DIR / "certs" / "server.crt"),
        "key_file": str(APP_DIR / "certs" / "server.key"),
        "suggested_base_url": f"https://{local_ip}:8443",
        "volume_up_presses": 10,
        "volume_down_presses": 30,
        "volume_press_delay_seconds": 0.08,
        "change_volume": False,
        "wake_wait_seconds": 8.0,
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    print(f"Created config at: {CONFIG_PATH}")
    print(f"Detected IP: {local_ip}")
    print(f"Generated admin key: {config['admin_key']}")
    print("Before starting the app, create pairing.json with `python3 scripts/pairing.py`.")
    print("Then review config.json, run ./scripts/gen_certs.sh, and start the app again.")
    raise SystemExit(0)


def load_json_file(path: Path, missing_message: str) -> dict[str, Any]:
    if not path.exists():
        print(missing_message)
        raise SystemExit(1)

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}") from exc


if not CONFIG_PATH.exists():
    write_default_config_and_exit()

CONFIG = load_json_file(
    CONFIG_PATH,
    f"Missing config file at {CONFIG_PATH}. Start the app once to create it.",
)
PAIRING = load_json_file(
    PAIRING_PATH,
    f"Missing pairing file at {PAIRING_PATH}. Run `python3 scripts/pairing.py` before starting the app.",
)


def save_pairing(pairing: dict[str, Any]) -> None:
    PAIRING_PATH.write_text(json.dumps(pairing, indent=2) + "\n", encoding="utf-8")


def check_basic_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> None:
    if not (
        secrets.compare_digest(credentials.username, CONFIG["admin_username"])
        and secrets.compare_digest(credentials.password, CONFIG["admin_key"])
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def volume_enabled() -> bool:
    return bool(CONFIG.get("change_volume", False))


def normalize_target(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def read_pairing() -> dict[str, Any]:
    return load_json_file(
        PAIRING_PATH,
        f"Missing pairing file at {PAIRING_PATH}. Run `python3 scripts/pairing.py` before starting the app.",
    )


def build_store(pairing: dict[str, Any]) -> dict[str, Any]:
    store = dict(pairing.get("store") or {})
    client_key = pairing.get("client_key")
    if client_key and not store.get("client_key"):
        store["client_key"] = client_key
    return store


class WebOSTVSession:
    def __init__(self) -> None:
        self.pairing = read_pairing()
        self.client: WebOSClient | None = None
        self.store: dict[str, Any] = {}

    def __enter__(self) -> "WebOSTVSession":
        host = str(self.pairing.get("host") or "").strip()
        if not host:
            raise RuntimeError("pairing.json is missing the TV host")

        secure = bool(self.pairing.get("secure", True))
        self.store = build_store(self.pairing)
        self.client = WebOSClient(host, secure=secure)
        self.client.connect()

        prompted = False
        for status in self.client.register(self.store):
            if status == WebOSClient.PROMPTED:
                prompted = True
            elif status == WebOSClient.REGISTERED:
                break

        if prompted and not self.store.get("client_key"):
            raise RuntimeError(
                "The TV requested a new pairing. Run `python3 scripts/pairing.py` to refresh pairing.json."
            )

        if self.store != self.pairing.get("store"):
            self.pairing["store"] = self.store
            if self.store.get("client_key"):
                self.pairing["client_key"] = self.store["client_key"]
            save_pairing(self.pairing)

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass

    @property
    def system(self) -> SystemControl:
        if self.client is None:
            raise RuntimeError("TV client is not connected")
        return SystemControl(self.client)

    @property
    def media(self) -> MediaControl:
        if self.client is None:
            raise RuntimeError("TV client is not connected")
        return MediaControl(self.client)

    @property
    def source(self) -> SourceControl:
        if self.client is None:
            raise RuntimeError("TV client is not connected")
        return SourceControl(self.client)

    @property
    def application(self) -> ApplicationControl:
        if self.client is None:
            raise RuntimeError("TV client is not connected")
        return ApplicationControl(self.client)


def source_payload(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        raw = dict(source)
    elif hasattr(source, "data") and isinstance(source.data, dict):
        raw = dict(source.data)
    else:
        raw = {}
        for attribute in ("id", "label", "name", "connected", "icon"):
            if hasattr(source, attribute):
                raw[attribute] = getattr(source, attribute)

    return {
        "id": raw.get("id"),
        "label": raw.get("label") or raw.get("name"),
        "connected": raw.get("connected"),
        "icon": raw.get("icon"),
        "raw": raw,
    }


def match_source(source: Any, target: str) -> bool:
    normalized_target = normalize_target(target)
    payload = source_payload(source)
    candidates = [
        payload.get("id"),
        payload.get("label"),
    ]
    return any(
        candidate and normalize_target(str(candidate)) == normalized_target
        for candidate in candidates
    )


def resolve_target(raw_target: str | None) -> str:
    if raw_target is None:
        return str(CONFIG["default_target"])

    value = raw_target.strip()
    normalized = normalize_target(value)

    if normalized == "default":
        return str(CONFIG["default_target"])

    if normalized == "pc":
        return str(CONFIG["pc_target"])

    return value


def find_source(session: WebOSTVSession, target: str) -> Any:
    sources = session.source.list_sources()
    for source in sources:
        if match_source(source, target):
            return source
    raise HTTPException(status_code=404, detail=f"Source not found: {target}")


def get_tv_status(session: WebOSTVSession) -> dict[str, Any]:
    info = session.system.info()
    sources = [source_payload(source) for source in session.source.list_sources()]
    try:
        current_app = session.application.get_current()
    except Exception:
        current_app = None

    try:
        volume = session.media.get_volume()
    except Exception:
        volume = None

    return {
        "host": session.pairing.get("host"),
        "secure": bool(session.pairing.get("secure", True)),
        "system": info,
        "current_app": current_app,
        "volume": volume,
        "sources": sources,
        "default_target": resolve_target("default"),
        "pc_target": resolve_target("pc"),
        "volume_control_enabled": volume_enabled(),
    }


def send_wol_packet(mac_address: str) -> None:
    cleaned = mac_address.replace(":", "").replace("-", "").strip().lower()
    if len(cleaned) != 12:
        raise HTTPException(status_code=400, detail="Invalid tv_mac format")

    try:
        mac_bytes = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid tv_mac format") from exc

    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ("255.255.255.255", 9))


def press_volume_up(session: WebOSTVSession, presses: int | None = None, delay: float | None = None) -> None:
    presses = int(presses or CONFIG.get("volume_up_presses", 10))
    delay = float(delay or CONFIG.get("volume_press_delay_seconds", 0.08))

    for _ in range(presses):
        session.media.volume_up()
        time.sleep(delay)


def press_volume_down(
    session: WebOSTVSession, presses: int | None = None, delay: float | None = None
) -> None:
    presses = int(presses or CONFIG.get("volume_down_presses", 30))
    delay = float(delay or CONFIG.get("volume_press_delay_seconds", 0.08))

    for _ in range(presses):
        session.media.volume_down()
        time.sleep(delay)


def load_cert_info() -> dict[str, str | None]:
    cert_path = Path(CONFIG["cert_file"])
    if not cert_path.exists():
        raise HTTPException(status_code=503, detail="Certificate file not found")

    pem_text = cert_path.read_text(encoding="utf-8")
    der_bytes = ssl.PEM_cert_to_DER_cert(pem_text)
    sha256_fingerprint = hashlib.sha256(der_bytes).hexdigest()

    return {
        "suggested_base_url": CONFIG.get("suggested_base_url"),
        "sha256_fingerprint": sha256_fingerprint,
        "pem": pem_text,
    }


@app.get(
    "/certs",
    tags=["Public"],
    summary="Get certificate pinning material",
    description="Returns certificate information clients can use for TOFU-style pinning.",
)
async def certs():
    return JSONResponse(
        {
            "ok": True,
            "certs": load_cert_info(),
        }
    )


@app.get(
    "/cec/status",
    tags=["TV"],
    summary="Get LG TV status",
    description="Returns WebOS information, source list, and configured target aliases.",
)
async def cec_status(_: None = Depends(check_basic_auth)):
    try:
        with WebOSTVSession() as session:
            return JSONResponse(
                {
                    "ok": True,
                    "status": get_tv_status(session),
                }
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"TV unreachable: {exc}") from exc


@app.post(
    "/cec/action",
    tags=["TV"],
    summary="Perform a TV action",
    description=(
        "Control the LG TV through WebOS while keeping the original API shape.\n\n"
        "### Actions\n"
        "- `turn_on` -> Send Wake-on-LAN if `tv_mac` is configured\n"
        "- `turn_off` -> Turn the TV off\n"
        "- `change_source` -> Switch to a configured or explicit source\n"
        "- `game` -> Switch to `pc_target` and optionally raise volume\n"
        "- `default` -> Switch to `default_target` and optionally lower volume"
    ),
    responses={
        401: {
            "description": "Authentication required or invalid credentials",
            "content": {"application/json": {"example": {"detail": "Unauthorized"}}},
        },
        503: {
            "description": "TV unavailable or not paired",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Missing pairing file at /path/pairing.json. Run `python3 scripts/pairing.py` before starting the app."
                    }
                }
            },
        },
    },
)
async def cec_action(
    body: ActionRequest,
    _: None = Depends(check_basic_auth),
):
    action = body.action

    try:
        if action == "turn_on":
            tv_mac = str(CONFIG.get("tv_mac") or "").strip()
            if not tv_mac:
                raise HTTPException(
                    status_code=503,
                    detail="Cannot turn on the TV over WebOS alone. Set `tv_mac` in config.json to enable Wake-on-LAN.",
                )

            send_wol_packet(tv_mac)
            wait_seconds = float(CONFIG.get("wake_wait_seconds", 8.0))
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            return JSONResponse(
                {
                    "ok": True,
                    "action": "turn_on",
                    "wake_signal_sent": True,
                    "tv_mac": tv_mac,
                }
            )

        with WebOSTVSession() as session:
            if action == "turn_off":
                session.system.power_off()
                return JSONResponse(
                    {
                        "ok": True,
                        "action": "turn_off",
                    }
                )

            if action == "change_source":
                target = resolve_target(body.target)
                source = find_source(session, target)
                session.source.set_source(source)
                time.sleep(0.25)
                return JSONResponse(
                    {
                        "ok": True,
                        "action": "change_source",
                        "target": target,
                        "source": source_payload(source),
                        "status": get_tv_status(session),
                    }
                )

            if action == "game":
                target = resolve_target("pc")
                source = find_source(session, target)
                session.source.set_source(source)
                time.sleep(0.25)

                volume_action = "skipped"
                if volume_enabled():
                    press_volume_up(session)
                    volume_action = "volume_up_applied"

                return JSONResponse(
                    {
                        "ok": True,
                        "action": "game",
                        "target": target,
                        "source_changed_to": source_payload(source),
                        "volume": volume_action,
                        "status": get_tv_status(session),
                    }
                )

            if action == "default":
                target = resolve_target("default")
                source = find_source(session, target)
                session.source.set_source(source)
                time.sleep(0.25)

                volume_action = "skipped"
                if volume_enabled():
                    press_volume_down(session)
                    volume_action = "volume_down_applied"

                return JSONResponse(
                    {
                        "ok": True,
                        "action": "default",
                        "target": target,
                        "source_changed_to": source_payload(source),
                        "volume": volume_action,
                        "status": get_tv_status(session),
                    }
                )

        raise HTTPException(status_code=400, detail="Unsupported action")

    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"TV unreachable: {exc}") from exc


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=CONFIG["host"],
        port=int(CONFIG["port"]),
        ssl_certfile=CONFIG["cert_file"],
        ssl_keyfile=CONFIG["key_file"],
    )
