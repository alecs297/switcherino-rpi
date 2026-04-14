import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pywebostv.connection import WebOSClient
from pywebostv.controls import SourceControl, SystemControl

APP_DIR = Path(__file__).resolve().parent.parent
PAIRING_PATH = APP_DIR / "pairing.json"


def prompt_yes_no(message: str) -> bool:
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in {"y", "yes", "o", "oui"}


def discover_tv() -> tuple[str, bool]:
    discovery_attempts = [(False, "plain"), (True, "secure")]

    for secure, label in discovery_attempts:
        print(f"Searching for TVs on the hotspot with {label} discovery...")
        try:
            clients = WebOSClient.discover(secure=secure)
        except TypeError:
            clients = WebOSClient.discover()
        except Exception as exc:
            print(f"Discovery failed ({label}): {exc}")
            continue

        if not clients:
            continue

        if len(clients) > 1:
            raise RuntimeError(
                "More than one WebOS TV was found. Re-run the script and enter the TV IP manually."
            )

        client = clients[0]
        host = getattr(client, "host", None) or getattr(client, "ip", None)
        if not host:
            raise RuntimeError("Discovery succeeded but the TV host could not be extracted.")
        return str(host), secure

    raise RuntimeError("No LG WebOS TV was discovered on the hotspot.")


def connect_and_pair(host: str, secure: bool) -> dict[str, Any]:
    client = WebOSClient(host, secure=secure)
    client.connect()

    store: dict[str, Any] = {}
    pairing_code = ""
    prompted = False

    try:
        for status in client.register(store):
            if status == WebOSClient.PROMPTED:
                prompted = True
                print("A pairing prompt should now be visible on the TV.")
                pairing_code = input(
                    "Enter the code shown on the TV, or press Enter if the TV only asks for confirmation: "
                ).strip()
            elif status == WebOSClient.REGISTERED:
                break
    finally:
        try:
            system = SystemControl(client)
            tv_info = system.info()
        except Exception:
            tv_info = {}

        try:
            source_control = SourceControl(client)
            sources = [serialize_source(source) for source in source_control.list_sources()]
        except Exception:
            sources = []

        try:
            client.close()
        except Exception:
            pass

    if not prompted and not store.get("client_key"):
        raise RuntimeError("Pairing did not complete and no client key was returned by the TV.")

    return {
        "host": host,
        "secure": secure,
        "client_key": store.get("client_key"),
        "store": store,
        "pairing_code": pairing_code,
        "paired_at": datetime.now(timezone.utc).isoformat(),
        "tv_info": tv_info,
        "sources": sources,
    }


def serialize_source(source: Any) -> dict[str, Any]:
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
        "raw": raw,
    }


def main() -> None:
    print("LG WebOS pairing helper")

    if not prompt_yes_no("Is the TV already connected to the Raspberry Pi hotspot?"):
        print("Connect the TV to the hotspot first, then run this script again.")
        raise SystemExit(1)

    manual_ip = input("TV IP address (leave blank to auto-discover it): ").strip()

    if manual_ip:
        host = manual_ip
        secure = prompt_yes_no("Use a secure WebOS connection on port 3001?")
    else:
        host, secure = discover_tv()
        print(f"Discovered TV at {host} (secure={secure})")

    pairing = connect_and_pair(host, secure)
    PAIRING_PATH.write_text(json.dumps(pairing, indent=2) + "\n", encoding="utf-8")

    print(f"Pairing completed. Saved credentials to {PAIRING_PATH}")
    if pairing.get("sources"):
        print("Available sources detected:")
        for source in pairing["sources"]:
            label = source.get("label") or source.get("id")
            print(f"- {label} ({source.get('id')})")


if __name__ == "__main__":
    main()
