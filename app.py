import base64
import hashlib
import json
import secrets
import socket
import ssl
import time
from pathlib import Path

import cec
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

CEC_INITIALIZED = False
app = FastAPI()


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
        "default_target": 4,
        "pc_target": 0,
        "cert_file": str(APP_DIR / "certs" / "server.crt"),
        "key_file": str(APP_DIR / "certs" / "server.key"),
        "suggested_base_url": f"https://{local_ip}:8443",
        "volume_up_presses": 10,
        "volume_down_presses": 30,
        "volume_press_delay_seconds": 0.08,
        "change_volume": False,
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    print(f"Created config at: {CONFIG_PATH}")
    print(f"Detected IP: {local_ip}")
    print(f"Generated admin key: {config['admin_key']}")
    print("Review config.json, run ./scripts/gen_certs.sh, then start the app again.")
    raise SystemExit(0)


if not CONFIG_PATH.exists():
    write_default_config_and_exit()

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def ensure_cec_initialized() -> None:
    global CEC_INITIALIZED

    if CEC_INITIALIZED:
        return

    adapters = cec.list_adapters()
    if not adapters:
        raise RuntimeError("No CEC adapters found")

    cec.init()
    CEC_INITIALIZED = True


def check_basic_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        ) from exc

    if not (
        secrets.compare_digest(username, CONFIG["admin_username"])
        and secrets.compare_digest(password, CONFIG["admin_key"])
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def resolve_target(raw_target) -> int:
    if raw_target is None:
        return int(CONFIG["default_target"])

    if isinstance(raw_target, int):
        return raw_target

    if isinstance(raw_target, str):
        value = raw_target.strip().lower()

        if value.isdigit():
            return int(value)

        if value == "default":
            return int(CONFIG["default_target"])

        if value == "pc":
            return int(CONFIG["pc_target"])

    raise HTTPException(status_code=400, detail="Invalid target")


def get_device(target: int) -> cec.Device:
    ensure_cec_initialized()
    return cec.Device(target)


def get_device_info(target: int) -> dict:
    device = get_device(target)
    return {
        "target": target,
        "osd_string": getattr(device, "osd_string", None),
        "vendor": getattr(device, "vendor", None),
        "physical_address": getattr(device, "physical_address", None),
        "active": bool(device.is_active()),
        "on": bool(device.is_on()),
    }


def set_active_source(target: int) -> None:
    ensure_cec_initialized()
    cec.set_active_source(target)


def volume_enabled() -> bool:
    return bool(CONFIG.get("change_volume", False))


def press_volume_up(presses: int | None = None, delay: float | None = None) -> None:
    ensure_cec_initialized()
    presses = int(presses or CONFIG.get("volume_up_presses", 10))
    delay = float(delay or CONFIG.get("volume_press_delay_seconds", 0.08))

    for _ in range(presses):
        cec.volume_up()
        time.sleep(delay)


def press_volume_down(presses: int | None = None, delay: float | None = None) -> None:
    ensure_cec_initialized()
    presses = int(presses or CONFIG.get("volume_down_presses", 30))
    delay = float(delay or CONFIG.get("volume_press_delay_seconds", 0.08))

    for _ in range(presses):
        cec.volume_down()
        time.sleep(delay)


def load_cert_info() -> dict:
    cert_path = Path(CONFIG["cert_file"])
    if not cert_path.exists():
        raise HTTPException(status_code=503, detail="Certificate file not found")

    pem_text = cert_path.read_text(encoding="utf-8")
    der_bytes = ssl.PEM_cert_to_DER_cert(pem_text)

    sha256_fingerprint = hashlib.sha256(der_bytes).hexdigest()
    sha1_fingerprint = hashlib.sha1(der_bytes).hexdigest()

    return {
        "suggested_base_url": CONFIG.get("suggested_base_url"),
        "sha256_fingerprint": sha256_fingerprint,
        "sha1_fingerprint": sha1_fingerprint,
        "pem": pem_text,
    }


@app.get("/certs")
async def certs():
    return JSONResponse(
        {
            "ok": True,
            "certs": load_cert_info(),
        }
    )


@app.get("/cec/status")
async def cec_status(request: Request, target: str | None = None):
    check_basic_auth(request)

    try:
        resolved_target = resolve_target(target)
        return JSONResponse(
            {
                "ok": True,
                "status": get_device_info(resolved_target),
                "default_target": int(CONFIG["default_target"]),
                "pc_target": int(CONFIG["pc_target"]),
                "change_volume": volume_enabled(),
            }
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/cec/action")
async def cec_action(request: Request):
    check_basic_auth(request)

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    action = str(body.get("action", "")).strip().lower()

    try:
        if action == "turn_on":
            target = resolve_target(body.get("target"))
            device = get_device(target)
            device.power_on()
            return JSONResponse(
                {
                    "ok": True,
                    "action": "turn_on",
                    "target": target,
                    "status": get_device_info(target),
                }
            )

        if action == "turn_off":
            target = resolve_target(body.get("target"))
            device = get_device(target)
            device.standby()
            return JSONResponse(
                {
                    "ok": True,
                    "action": "turn_off",
                    "target": target,
                    "status": get_device_info(target),
                }
            )

        if action == "change_source":
            target = resolve_target(body.get("target"))
            set_active_source(target)
            return JSONResponse(
                {
                    "ok": True,
                    "action": "change_source",
                    "target": target,
                    "status": get_device_info(target),
                }
            )

        if action == "game":
            target = int(CONFIG["pc_target"])
            device = get_device(target)
            was_on = bool(device.is_on())

            if not was_on:
                device.power_on()
                time.sleep(1.0)

            set_active_source(target)
            time.sleep(0.25)

            volume_action = "skipped"
            if volume_enabled():
                press_volume_up()
                volume_action = "volume_up_applied"

            return JSONResponse(
                {
                    "ok": True,
                    "action": "game",
                    "target": target,
                    "powered_on_before": was_on,
                    "powered_on_action_taken": not was_on,
                    "source_changed_to": target,
                    "volume": volume_action,
                    "status": get_device_info(target),
                }
            )

        if action == "default":
            target = int(CONFIG["default_target"])
            device = get_device(target)
            was_on = bool(device.is_on())

            if not was_on:
                return JSONResponse(
                    {
                        "ok": True,
                        "action": "default",
                        "target": target,
                        "ignored": True,
                        "reason": "default target is powered off",
                        "status": get_device_info(target),
                    }
                )

            set_active_source(target)
            time.sleep(0.25)

            volume_action = "skipped"
            if volume_enabled():
                press_volume_down()
                volume_action = "volume_down_applied"

            return JSONResponse(
                {
                    "ok": True,
                    "action": "default",
                    "target": target,
                    "ignored": False,
                    "source_changed_to": target,
                    "volume": volume_action,
                    "status": get_device_info(target),
                }
            )

        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported action. Use one of: "
                "turn_on, turn_off, change_source, game, default"
            ),
        )

    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=CONFIG["host"],
        port=int(CONFIG["port"]),
        ssl_certfile=CONFIG["cert_file"],
        ssl_keyfile=CONFIG["key_file"],
    )