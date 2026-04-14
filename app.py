import hashlib
import json
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Literal

import cec
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

CEC_INITIALIZED = False
security = HTTPBasic()

app = FastAPI(
    title="HDMI CEC API",
    description=(
        "HTTPS API for controlling HDMI-CEC devices from a Raspberry Pi. "
        "Protected CEC endpoints use HTTP Basic authentication."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


class ActionRequest(BaseModel):
    action: Literal["turn_on", "turn_off", "change_source", "game", "default"] = Field(
        ...,
        description=(
            "Action to perform.\n\n"
            "- **turn_on**: Power on the target device\n"
            "- **turn_off**: Put the target device in standby\n"
            "- **change_source**: Switch active HDMI source to target\n"
            "- **game**: Power on `pc_target` if needed, switch to it, optionally raise volume\n"
            "- **default**: If `default_target` is on, switch to it, optionally lower volume"
        ),
        examples=["game"],
    )
    target: int | str | None = Field(
        default=None,
        description=(
            "Target CEC device.\n\n"
            "Accepted values:\n"
            "- integer logical address, for example `4`\n"
            "- `'default'` → configured `default_target`\n"
            "- `'pc'` → configured `pc_target`\n\n"
            "Used by `turn_on`, `turn_off`, and `change_source`.\n"
            "Ignored by `game` and `default`."
        ),
        examples=[4],
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
    print("Review config.json, run ./gen_certs.sh, then start the app again.")
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


def resolve_target(raw_target: int | str | None) -> int:
    if raw_target is None:
        return int(CONFIG["default_target"])

    if isinstance(raw_target, int):
        return raw_target

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


def get_device_info(target: int) -> dict[str, Any]:
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
    tags=["CEC"],
    summary="Get configured device statuses",
    description="Returns status for both the configured default target and pc target.",
)
async def cec_status(_: None = Depends(check_basic_auth)):
    try:
        return JSONResponse(
            {
                "ok": True,
                "default_target": int(CONFIG["default_target"]),
                "pc_target": int(CONFIG["pc_target"]),
                "default_status": get_device_info(int(CONFIG["default_target"])),
                "pc_status": get_device_info(int(CONFIG["pc_target"])),
                "volume_control_enabled": volume_enabled(),
            }
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post(
    "/cec/action",
    tags=["CEC"],
    summary="Perform a CEC action",
    description=(
        "Control HDMI-CEC devices.\n\n"
        "### Actions\n"
        "- `turn_on` → Power on a target device\n"
        "- `turn_off` → Put a target device in standby\n"
        "- `change_source` → Switch the active HDMI source to a target device\n"
        "- `game` → Power on `pc_target` if needed, switch to it, and optionally raise volume\n"
        "- `default` → If `default_target` is on, switch to it, and optionally lower volume\n\n"
        "### Target values\n"
        "- integer CEC logical address, such as `4`\n"
        "- `default`\n"
        "- `pc`\n\n"
        "### Notes\n"
        "- `game` and `default` ignore the request body's `target`\n"
        "- Volume changes only happen if `change_volume` is enabled in config"
    ),
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "turn_on": {
                            "summary": "Turn on a device",
                            "value": {"action": "turn_on", "target": 4},
                        },
                        "turn_off": {
                            "summary": "Turn off the configured pc target",
                            "value": {"action": "turn_off", "target": "pc"},
                        },
                        "change_source": {
                            "summary": "Switch to a specific source",
                            "value": {"action": "change_source", "target": 4},
                        },
                        "game": {
                            "summary": "Switch to the configured game source",
                            "value": {"action": "game"},
                        },
                        "default": {
                            "summary": "Return to the configured default source",
                            "value": {"action": "default"},
                        },
                    }
                }
            }
        }
    },
    responses={
        200: {
            "description": "Successful action",
            "content": {
                "application/json": {
                    "examples": {
                        "game_response": {
                            "summary": "Game action response",
                            "value": {
                                "ok": True,
                                "action": "game",
                                "target": 0,
                                "powered_on_before": False,
                                "powered_on_action_taken": True,
                                "source_changed_to": 0,
                                "volume": "volume_up_applied",
                                "status": {
                                    "target": 0,
                                    "osd_string": "TV",
                                    "vendor": "LG",
                                    "physical_address": "0.0.0.0",
                                    "active": True,
                                    "on": True,
                                },
                            },
                        }
                    }
                }
            },
        },
        400: {
            "description": "Invalid request",
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_target": {
                            "summary": "Invalid target",
                            "value": {"detail": "Invalid target"},
                        },
                        "unsupported_action": {
                            "summary": "Unsupported action",
                            "value": {"detail": "Unsupported action"},
                        },
                    }
                }
            },
        },
        401: {
            "description": "Authentication required or invalid credentials",
            "content": {
                "application/json": {
                    "example": {"detail": "Unauthorized"}
                }
            },
        },
        503: {
            "description": "CEC unavailable",
            "content": {
                "application/json": {
                    "example": {"detail": "No CEC adapters found"}
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
            target = resolve_target(body.target)
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
            target = resolve_target(body.target)
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
            target = resolve_target(body.target)
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

        raise HTTPException(status_code=400, detail="Unsupported action")

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