import argparse
import ipaddress
import json
import socket
import time
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_PAIRING_PATH = APP_DIR / "pairing.json"


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_mac(mac_address: str) -> str:
    cleaned = mac_address.replace(":", "").replace("-", "").strip().lower()
    if len(cleaned) != 12:
        raise ValueError("Invalid MAC address format")
    bytes.fromhex(cleaned)
    return cleaned


def derive_broadcast_from_ip(ip_address: str) -> str:
    return str(ipaddress.ip_network(f"{ip_address}/24", strict=False).broadcast_address)


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_ports(raw_ports: list[str] | None, config: dict[str, Any]) -> list[int]:
    ports: list[int] = []

    if raw_ports:
        for value in raw_ports:
            for part in value.split(","):
                part = part.strip()
                if part:
                    ports.append(int(part))
    else:
        configured = config.get("wake_ports") or [9, 7]
        for value in configured:
            ports.append(int(value))

    if not ports:
        ports = [9, 7]

    return ports


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = {}
    pairing: dict[str, Any] = {}

    if args.config:
        config = load_json_file(Path(args.config).resolve())
    elif DEFAULT_CONFIG_PATH.exists():
        config = load_json_file(DEFAULT_CONFIG_PATH)

    if args.pairing:
        pairing = load_json_file(Path(args.pairing).resolve())
    elif DEFAULT_PAIRING_PATH.exists():
        pairing = load_json_file(DEFAULT_PAIRING_PATH)

    mac_address = args.mac or str(config.get("tv_mac") or "").strip()
    if not mac_address:
        raise SystemExit("No MAC address found. Pass --mac or provide it in config.json.")

    host = args.host or str(pairing.get("host") or "").strip()

    broadcast_addresses: list[str] = []
    configured_addresses = config.get("wake_broadcast_addresses") or []
    if isinstance(configured_addresses, list):
        broadcast_addresses.extend(str(value).strip() for value in configured_addresses if str(value).strip())

    if args.broadcast:
        for value in args.broadcast:
            for part in value.split(","):
                part = part.strip()
                if part:
                    broadcast_addresses.append(part)

    broadcast_addresses.append("255.255.255.255")

    derived_broadcast = None
    if host:
        try:
            derived_broadcast = derive_broadcast_from_ip(host)
            broadcast_addresses.append(derived_broadcast)
        except ValueError:
            pass

    return {
        "config": config,
        "pairing": pairing,
        "mac": normalize_mac(mac_address),
        "host": host,
        "broadcasts": dedupe_preserve_order(broadcast_addresses),
        "ports": parse_ports(args.port, config),
        "attempts": max(1, args.attempts or int(config.get("wake_attempts", 3))),
        "interval": max(0.0, args.interval if args.interval is not None else float(config.get("wake_attempt_interval_seconds", 2.0))),
        "derived_broadcast": derived_broadcast,
    }


def send_magic_packet(mac_address: str, broadcast: str, port: int) -> None:
    packet = b"\xff" * 6 + bytes.fromhex(mac_address) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))


def try_connect(host: str, port: int = 3001, timeout: float = 2.0) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def print_extract(context: dict[str, Any]) -> None:
    printable_mac = ":".join(context["mac"][index:index + 2] for index in range(0, 12, 2))
    print("Wake-on-LAN context")
    print(f"MAC: {printable_mac}")
    print(f"TV host: {context['host'] or '(unknown)'}")
    print(f"Derived broadcast: {context['derived_broadcast'] or '(unavailable)'}")
    print("Broadcast targets:")
    for address in context["broadcasts"]:
        print(f"- {address}")
    print("Ports:")
    for port in context["ports"]:
        print(f"- {port}")


def run_test(context: dict[str, Any], debug: bool) -> None:
    for attempt in range(1, context["attempts"] + 1):
        print(f"Wake attempt {attempt}/{context['attempts']}")
        for address in context["broadcasts"]:
            for port in context["ports"]:
                if debug:
                    print(f"  sending magic packet to {address}:{port}")
                send_magic_packet(context["mac"], address, port)
        if attempt < context["attempts"] and context["interval"] > 0:
            time.sleep(context["interval"])

    if context["host"]:
        print("Connectivity check:")
        print(f"- port 3001 reachable: {try_connect(context['host'], 3001)}")
        print(f"- port 3000 reachable: {try_connect(context['host'], 3000)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect and test Wake-on-LAN settings for the LG TV. "
            "The script can use the default config.json, an explicit JSON file, or direct CLI arguments."
        )
    )
    parser.add_argument(
        "mode",
        choices=["extract", "test", "all"],
        help="`extract` prints the derived WOL settings, `test` sends WOL packets, `all` does both.",
    )
    parser.add_argument(
        "--config",
        help="Path to a config JSON file. Defaults to ./config.json if present.",
    )
    parser.add_argument(
        "--pairing",
        help="Path to a pairing JSON file. Defaults to ./pairing.json if present.",
    )
    parser.add_argument("--mac", help="TV MAC address to use for Wake-on-LAN.")
    parser.add_argument("--host", help="TV IP address used to derive a /24 broadcast and test connectivity.")
    parser.add_argument(
        "--broadcast",
        action="append",
        help="Broadcast address to add. Can be passed multiple times or as a comma-separated list.",
    )
    parser.add_argument(
        "--port",
        action="append",
        help="UDP port to use for WOL. Can be passed multiple times or as a comma-separated list.",
    )
    parser.add_argument("--attempts", type=int, help="Number of WOL rounds to send.")
    parser.add_argument("--interval", type=float, help="Delay in seconds between WOL rounds.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print each broadcast target and port while testing.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    context = build_context(args)

    if args.mode in {"extract", "all"}:
        print_extract(context)

    if args.mode in {"test", "all"}:
        run_test(context, debug=args.debug)


if __name__ == "__main__":
    main()
