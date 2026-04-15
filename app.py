import hashlib
import ipaddress
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

STATUS_EXAMPLE = {
    "ok": True,
    "status": {
        "host": "192.168.50.46",
        "secure": True,
        "system": {
            "product_name": "webOSTV 24",
            "model_name": "HE_DTV_W24G_AFABATAA",
            "major_ver": "23",
            "minor_ver": "20.39",
            "device_id": "f8:01:b4:d2:c6:5a",
        },
        "current_app": "com.webos.app.hdmi4",
        "volume": {
            "volumeStatus": {
                "volume": 13,
                "muteStatus": False,
                "soundOutput": "tv_speaker",
            },
            "callerId": "secondscreen.client",
        },
        "sources": [
            {
                "id": "HDMI_1",
                "label": "PC",
                "connected": True,
                "icon": "https://192.168.50.46:3001/resources/example/pc.png",
                "raw": {
                    "id": "HDMI_1",
                    "label": "PC",
                    "port": 1,
                    "connected": True,
                    "appId": "com.webos.app.hdmi1",
                },
            },
            {
                "id": "HDMI_2",
                "label": "PC",
                "connected": True,
                "icon": "https://192.168.50.46:3001/resources/example/pc.png",
                "raw": {
                    "id": "HDMI_2",
                    "label": "PC",
                    "port": 2,
                    "connected": True,
                    "appId": "com.webos.app.hdmi2",
                },
            },
            {
                "id": "HDMI_4",
                "label": "Apple OTT",
                "connected": True,
                "icon": "https://192.168.50.46:3001/resources/example/streamingbox.png",
                "raw": {
                    "id": "HDMI_4",
                    "label": "Apple OTT",
                    "port": 4,
                    "connected": True,
                    "appId": "com.webos.app.hdmi4",
                },
            },
        ],
        "default_target": "HDMI_1",
        "pc_target": "HDMI_2",
        "volume_control_enabled": False,
    },
}

ACTION_RESPONSE_EXAMPLES = {
    "change_source": {
        "summary": "Switch to a specific HDMI input",
        "value": {
            "ok": True,
            "action": "change_source",
            "target": "HDMI_4",
            "source": {
                "id": "HDMI_4",
                "label": "Apple OTT",
                "connected": True,
            },
            "status": {
                "current_app": "com.webos.app.hdmi4",
                "default_target": "HDMI_1",
                "pc_target": "HDMI_2",
            },
        },
    },
    "switch_to_game_mode": {
        "summary": "Switch to the configured gaming source",
        "value": {
            "ok": True,
            "action": "switch_to_game_mode",
            "target": "HDMI_2",
            "source_changed_to": {
                "id": "HDMI_2",
                "label": "PC",
                "connected": True,
            },
            "volume": {
                "changed": True,
                "target": 15,
            },
            "status": {
                "current_app": "com.webos.app.hdmi2",
                "default_target": "HDMI_1",
                "pc_target": "HDMI_2",
            },
        },
    },
    "turn_on": {
        "summary": "Wake the TV and switch to a target after it comes online",
        "value": {
            "ok": True,
            "action": "turn_on",
            "wake_signal_sent": True,
            "wake_attempts": 2,
            "wake_targets": [
                {"address": "255.255.255.255", "port": 9},
                {"address": "192.168.50.255", "port": 9},
            ],
            "tv_online": True,
            "tv_mac": "f8:01:b4:d2:c6:5a",
            "target_after_wake": "HDMI_2",
            "source_changed_to": {
                "id": "HDMI_2",
                "label": "PC",
                "connected": True,
            },
            "status": {
                "current_app": "com.webos.app.hdmi2",
                "default_target": "HDMI_1",
                "pc_target": "HDMI_2",
            },
        },
    },
}


class ActionRequest(BaseModel):
    action: Literal[
        "turn_on",
        "turn_off",
        "change_source",
        "switch_to_game_mode",
        "switch_to_default_mode",
    ] = Field(
        ...,
        description=(
            "Action to perform.\n\n"
            "- **turn_on**: Wake the TV over the network if `tv_mac` is configured\n"
            "- **turn_off**: Turn the TV off through WebOS\n"
            "- **change_source**: Switch the TV to a configured or explicit source\n"
            "- **switch_to_game_mode**: Switch to `pc_target` and optionally set the game volume\n"
            "- **switch_to_default_mode**: Switch to `default_target` and optionally set the default volume"
        ),
        examples=["switch_to_game_mode"],
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
            "For `turn_on`, the target is applied after the TV becomes reachable.\n"
            "Ignored by `switch_to_game_mode` and `switch_to_default_mode`."
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
        "change_volume_on_game_mode": False,
        "change_volume_on_default_mode": False,
        "game_mode_volume": 15,
        "default_mode_volume": 0,
        "wake_wait_seconds": 8.0,
        "wake_attempts": 3,
        "wake_attempt_interval_seconds": 2.0,
        "wake_connect_timeout_seconds": 20.0,
        "turn_on_target": "",
        "wake_broadcast_addresses": [],
        "wake_ports": [9, 7],
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


def volume_enabled_for_game_mode() -> bool:
    return bool(CONFIG.get("change_volume_on_game_mode", False))


def volume_enabled_for_default_mode() -> bool:
    return bool(CONFIG.get("change_volume_on_default_mode", False))


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


def resolve_mode_target(mode: Literal["default", "pc"]) -> str:
    if mode == "default":
        return str(CONFIG["default_target"])
    return str(CONFIG["pc_target"])


def find_source(session: WebOSTVSession, target: str) -> Any:
    sources = session.source.list_sources()
    for source in sources:
        if match_source(source, target):
            return source
    raise HTTPException(status_code=404, detail=f"Source not found: {target}")


def wait_for_tv_connection(timeout_seconds: float) -> WebOSTVSession:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    last_exception: Exception | None = None

    while time.monotonic() <= deadline:
        session = WebOSTVSession()
        try:
            session.__enter__()
            return session
        except Exception as exc:
            last_exception = exc
            session.__exit__(None, None, None)
            time.sleep(1.0)

    detail = "TV did not come online after Wake-on-LAN"
    if last_exception is not None:
        detail = f"{detail}: {last_exception}"
    raise RuntimeError(detail)


def resolve_turn_on_target(raw_target: str | None) -> str | None:
    if raw_target and raw_target.strip():
        return resolve_target(raw_target)

    config_target = str(CONFIG.get("turn_on_target") or "").strip()
    if config_target:
        return resolve_target(config_target)

    return None


def switch_to_target(session: WebOSTVSession, target: str) -> dict[str, Any]:
    source = find_source(session, target)
    session.source.set_source(source)
    time.sleep(0.25)
    return source_payload(source)


def send_wol_sequence(tv_mac: str) -> tuple[int, list[tuple[str, int]]]:
    wake_attempts = max(1, int(CONFIG.get("wake_attempts", 3)))
    wake_attempt_interval = max(
        0.0, float(CONFIG.get("wake_attempt_interval_seconds", 2.0))
    )
    last_targets: list[tuple[str, int]] = []

    for attempt_index in range(wake_attempts):
        last_targets = send_wol_packet(tv_mac)
        if attempt_index < wake_attempts - 1 and wake_attempt_interval > 0:
            time.sleep(wake_attempt_interval)

    return wake_attempts, last_targets


def wake_tv_and_wait(tv_mac: str) -> tuple[WebOSTVSession, int, list[tuple[str, int]]]:
    wake_attempts, wake_targets = send_wol_sequence(tv_mac)
    wait_seconds = float(CONFIG.get("wake_wait_seconds", 8.0))
    connect_timeout = float(CONFIG.get("wake_connect_timeout_seconds", 20.0))

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    return wait_for_tv_connection(connect_timeout), wake_attempts, wake_targets


def open_session_for_source_change() -> tuple[WebOSTVSession, bool, int]:
    try:
        session = WebOSTVSession()
        session.__enter__()
        return session, False, 0
    except Exception:
        session.__exit__(None, None, None)
        tv_mac = str(CONFIG.get("tv_mac") or "").strip()
        if not tv_mac:
            raise

        session, wake_attempts, _wake_targets = wake_tv_and_wait(tv_mac)
        return session, True, wake_attempts


def set_volume_target(session: WebOSTVSession, target_volume: int) -> dict[str, Any]:
    volume = max(0, min(100, int(target_volume)))
    session.media.set_volume(volume)
    return {
        "changed": True,
        "target": volume,
    }


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
        "default_target": resolve_mode_target("default"),
        "pc_target": resolve_mode_target("pc"),
        "change_volume_on_game_mode": volume_enabled_for_game_mode(),
        "change_volume_on_default_mode": volume_enabled_for_default_mode(),
    }


def get_wake_targets() -> list[tuple[str, int]]:
    configured_addresses = CONFIG.get("wake_broadcast_addresses") or []
    configured_ports = CONFIG.get("wake_ports") or [9, 7]

    addresses: list[str] = []
    if isinstance(configured_addresses, list):
        addresses.extend(str(value).strip() for value in configured_addresses if str(value).strip())

    if "255.255.255.255" not in addresses:
        addresses.append("255.255.255.255")

    pairing_host = str(read_pairing().get("host") or "").strip()
    if pairing_host:
        try:
            subnet_broadcast = str(
                ipaddress.ip_network(f"{pairing_host}/24", strict=False).broadcast_address
            )
            if subnet_broadcast not in addresses:
                addresses.append(subnet_broadcast)
        except ValueError:
            pass

    ports: list[int] = []
    if isinstance(configured_ports, list):
        for value in configured_ports:
            try:
                ports.append(int(value))
            except (TypeError, ValueError):
                continue
    if not ports:
        ports = [9, 7]

    return [(address, port) for address in addresses for port in ports]


def send_wol_packet(mac_address: str) -> list[tuple[str, int]]:
    cleaned = mac_address.replace(":", "").replace("-", "").strip().lower()
    if len(cleaned) != 12:
        raise HTTPException(status_code=400, detail="Invalid tv_mac format")

    try:
        mac_bytes = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid tv_mac format") from exc

    packet = b"\xff" * 6 + mac_bytes * 16
    wake_targets = get_wake_targets()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for address, port in wake_targets:
            sock.sendto(packet, (address, port))

    return wake_targets


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
    "/tv/status",
    tags=["TV"],
    summary="Get LG TV status",
    description=(
        "Returns the current WebOS session status, system info, active app, volume state, "
        "and the list of available sources.\n\n"
        "Prefer `sources[*].id` such as `HDMI_1` or `HDMI_2` when configuring targets, "
        "because labels like `PC` may be duplicated."
    ),
    responses={
        200: {
            "description": "Current TV status and source metadata",
            "content": {
                "application/json": {
                    "examples": {
                        "status": {
                            "summary": "Typical status payload from a paired LG TV",
                            "value": STATUS_EXAMPLE,
                        }
                    }
                }
            },
        },
        401: {
            "description": "Authentication required or invalid credentials",
            "content": {"application/json": {"example": {"detail": "Unauthorized"}}},
        },
        503: {
            "description": "TV unavailable or pairing missing",
            "content": {
                "application/json": {
                    "examples": {
                        "missing_pairing": {
                            "summary": "Pairing file is missing",
                            "value": {
                                "detail": "Missing pairing file at /path/pairing.json. Run `python3 scripts/pairing.py` before starting the app."
                            },
                        },
                        "tv_unreachable": {
                            "summary": "TV cannot be reached on the hotspot",
                            "value": {"detail": "TV unreachable: timed out"},
                        },
                    }
                }
            },
        },
    },
)
async def tv_status(_: None = Depends(check_basic_auth)):
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
    "/tv/action",
    tags=["TV"],
    summary="Perform a TV action",
    description=(
        "Control the LG TV through WebOS while keeping the original API shape.\n\n"
        "### Actions\n"
        "- `turn_on` -> Send Wake-on-LAN, wait for the TV to come online, and optionally switch source\n"
        "- `turn_off` -> Turn the TV off\n"
        "- `change_source` -> Switch to a configured or explicit source, waking the TV first if needed\n"
        "- `switch_to_game_mode` -> Switch to `pc_target`, waking the TV first if needed, and optionally set volume\n"
        "- `switch_to_default_mode` -> Switch to `default_target`, waking the TV first if needed, and optionally set volume\n\n"
        "### Target advice\n"
        "- prefer source ids such as `HDMI_1` over labels such as `PC`\n"
        "- labels can be duplicated on LG TVs\n"
        "- use `GET /tv/status` to inspect available sources\n\n"
        "### Wake-on-LAN configuration\n"
        "- `tv_mac` is required for `turn_on`\n"
        "- `wake_attempts`, `wake_attempt_interval_seconds`, `wake_wait_seconds`, and `wake_connect_timeout_seconds` tune the wake flow\n"
        "- `wake_broadcast_addresses` and `wake_ports` control where WOL packets are sent\n"
        "- `turn_on_target` is used when `turn_on` is called without a request target\n\n"
        "### Mode volume configuration\n"
        "- `change_volume_on_game_mode` and `game_mode_volume` control `switch_to_game_mode`\n"
        "- `change_volume_on_default_mode` and `default_mode_volume` control `switch_to_default_mode`"
    ),
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "change_source_by_id": {
                            "summary": "Switch to a source by id",
                            "value": {"action": "change_source", "target": "HDMI_4"},
                        },
                        "change_source_by_alias": {
                            "summary": "Switch using a configured alias",
                            "value": {"action": "change_source", "target": "default"},
                        },
                        "switch_to_game_mode": {
                            "summary": "Switch to the configured PC source and set game volume",
                            "value": {"action": "switch_to_game_mode"},
                        },
                        "switch_to_default_mode": {
                            "summary": "Return to the default source and set default volume",
                            "value": {"action": "switch_to_default_mode"},
                        },
                        "turn_off": {
                            "summary": "Power the TV off",
                            "value": {"action": "turn_off"},
                        },
                        "turn_on": {
                            "summary": "Wake the TV and switch to the configured PC source",
                            "value": {"action": "turn_on", "target": "pc"},
                        },
                    }
                }
            }
        }
    },
    responses={
        200: {
            "description": "Action completed successfully",
            "content": {
                "application/json": {
                    "examples": ACTION_RESPONSE_EXAMPLES
                }
            },
        },
        400: {
            "description": "Invalid request payload",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_mac": {
                            "summary": "Invalid Wake-on-LAN MAC address",
                            "value": {"detail": "Invalid tv_mac format"},
                        }
                    }
                }
            },
        },
        404: {
            "description": "Requested source could not be found",
            "content": {
                "application/json": {
                    "example": {"detail": "Source not found: HDMI_9"}
                }
            },
        },
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
async def tv_action(
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

            target_after_wake = resolve_turn_on_target(body.target)
            session, wake_attempts, wake_targets = wake_tv_and_wait(tv_mac)
            try:
                source_changed_to = None
                if target_after_wake is not None:
                    source_changed_to = switch_to_target(session, target_after_wake)

                return JSONResponse(
                    {
                        "ok": True,
                        "action": "turn_on",
                        "wake_signal_sent": True,
                        "wake_attempts": wake_attempts,
                        "wake_targets": [
                            {"address": address, "port": port}
                            for address, port in wake_targets
                        ],
                        "tv_online": True,
                        "tv_mac": tv_mac,
                        "target_after_wake": target_after_wake,
                        "source_changed_to": source_changed_to,
                        "status": get_tv_status(session),
                    }
                )
            finally:
                session.__exit__(None, None, None)

        if action == "turn_off":
            with WebOSTVSession() as session:
                session.system.power_off()
                return JSONResponse(
                    {
                        "ok": True,
                        "action": "turn_off",
                    }
                )

        if action in {
            "change_source",
            "switch_to_game_mode",
            "switch_to_default_mode",
        }:
            session, wake_signal_sent, wake_attempts = open_session_for_source_change()
            try:
                if action == "change_source":
                    target = resolve_target(body.target)
                    source = switch_to_target(session, target)
                    return JSONResponse(
                        {
                            "ok": True,
                            "action": "change_source",
                            "target": target,
                            "wake_signal_sent": wake_signal_sent,
                            "wake_attempts": wake_attempts,
                            "source": source,
                            "status": get_tv_status(session),
                        }
                    )

                if action == "switch_to_game_mode":
                    target = resolve_mode_target("pc")
                    source = switch_to_target(session, target)
                    volume = {"changed": False, "target": None}
                    if volume_enabled_for_game_mode():
                        volume = set_volume_target(
                            session, int(CONFIG.get("game_mode_volume", 15))
                        )

                    return JSONResponse(
                        {
                            "ok": True,
                            "action": "switch_to_game_mode",
                            "target": target,
                            "wake_signal_sent": wake_signal_sent,
                            "wake_attempts": wake_attempts,
                            "source_changed_to": source,
                            "volume": volume,
                            "status": get_tv_status(session),
                        }
                    )

                if action == "switch_to_default_mode":
                    target = resolve_mode_target("default")
                    source = switch_to_target(session, target)
                    volume = {"changed": False, "target": None}
                    if volume_enabled_for_default_mode():
                        volume = set_volume_target(
                            session, int(CONFIG.get("default_mode_volume", 0))
                        )

                    return JSONResponse(
                        {
                            "ok": True,
                            "action": "switch_to_default_mode",
                            "target": target,
                            "wake_signal_sent": wake_signal_sent,
                            "wake_attempts": wake_attempts,
                            "source_changed_to": source,
                            "volume": volume,
                            "status": get_tv_status(session),
                        }
                    )
            finally:
                session.__exit__(None, None, None)

        raise HTTPException(status_code=400, detail="Unsupported action")

    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"TV unreachable: {exc}") from exc


@app.get(
    "/webos/status",
    tags=["TV"],
    include_in_schema=False,
)
async def webos_status_alias(_: None = Depends(check_basic_auth)):
    return await tv_status(_)


@app.post(
    "/webos/action",
    tags=["TV"],
    include_in_schema=False,
)
async def webos_action_alias(
    body: ActionRequest,
    _: None = Depends(check_basic_auth),
):
    return await tv_action(body, _)


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=CONFIG["host"],
        port=int(CONFIG["port"]),
        ssl_certfile=CONFIG["cert_file"],
        ssl_keyfile=CONFIG["key_file"],
    )
