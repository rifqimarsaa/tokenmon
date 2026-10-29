from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import adapters
from .core import Store, now_iso


def since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value/1_000:.1f}K"
    return str(value)


def bar(percent: float | None, width: int = 18) -> str:
    if percent is None:
        return "[" + "?" * width + "]"
    value = max(0.0, min(100.0, percent))
    filled = round(value / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def styled(value: str, code: str) -> str:
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{value}\033[0m"
    return value


def green(value: str) -> str:
    return styled(value, "32")


def bold(value: str) -> str:
    return styled(value, "1")


def dim(value: str) -> str:
    return styled(value, "2")


def rule() -> str:
    return "-" * min(78, max(48, shutil.get_terminal_size((80, 24)).columns - 2))


def meter(percent: float, width: int = 20) -> str:
    value = max(0.0, min(100.0, percent))
    filled = round(value / 100 * width)
    return green("#" * filled) + "-" * (width - filled)


def scan(store: Store, quiet: bool = False) -> int:
    total = files = 0
    for adapter in adapters():
        added, changed = adapter.scan(store)
        total += added
        files += changed
        if not quiet:
            print(f"{adapter.provider}: {added} new events from {changed} changed files")
    return total


def show_allowance(store: Store) -> None:
    rows = store.latest_allowances()
    print("ACCOUNT ALLOWANCE")
    if not rows:
        print(f"Claude Max   {bar(None)} unavailable")
        print(f"Codex Pro    {bar(None)} unavailable")
    else:
        providers = {r["provider"] for r in rows}
        if "claude" not in providers:
            print(f"Claude Max   {bar(None)} unavailable")
        for row in rows:
            reset = row["resets_at"] or "unknown"
            remaining = "N/A" if row["remaining_percent"] is None else f"{row['remaining_percent']:.0f}% remaining"
            print(f"{row['provider'].title():12} {bar(row['remaining_percent'])} {remaining:14} "
                  f"{row['allowance_type']} reset: {reset}")
    print()


def show_usage(store: Store, days: int, group: str, limit: int | None = None) -> None:
    rows = store.usage(since(days), group)
    label = group.upper()
    print(f"LOCAL USAGE - LAST {days} DAY(S) BY {label}")
    if not rows:
        print("No local usage found. Run `tokenmon scan`.")
        return
    total = sum(r["total_tokens"] for r in rows)
    print(f"{'Name':22} {'Provider/client':16} {'Input':>8} {'Output':>8} {'Cache':>8} {'Total':>8}  Local share")
    shown = rows[:limit] if limit else rows
    for row in shown:
        share = row["total_tokens"] / total * 100 if total else 0
        print(f"{row['label'][:22]:22} {row['detail'][:16]:16} {compact(row['input_tokens']):>8} "
              f"{compact(row['output_tokens']):>8} {compact(row['cache_tokens']):>8} "
              f"{compact(row['total_tokens']):>8}  {bar(share, 12)} {share:5.1f}%")
    if limit and len(rows) > limit:
        print(f"... {len(rows) - limit} more; run `tokenmon {group}s` for details")
    print(f"\nLocal total: {compact(total)} tokens")


def summary(store: Store, days: int = 7, group: str = "device", do_scan: bool = True) -> None:
    if do_scan:
        scan(store, quiet=True)
    print(f"TOKENMON\nUpdated: {now_iso()}\n")
    show_allowance(store)
    show_usage(store, days, group)


def dashboard(store: Store) -> None:
    scan(store, quiet=True)
    provider_rows = store.usage(since(7), "provider")
    workspace_rows = store.usage(since(7), "workspace")
    client_rows = store.usage(since(7), "client")
    device_rows = store.usage(since(7), "device")
    total = sum(row["total_tokens"] for row in provider_rows)

    print(" " + bold("TOKENMON") + "  " + dim("Settings   Status   Config") + "  " + green("[Usage]") + "  " + dim("Stats"))
    print(" " + dim(rule()))
    print()
    print("   " + bold("What's contributing to usage?"))
    print("   " + dim("Approximate - local and synced sessions only"))
    print()
    print("   " + bold("Last 7 days") + dim(" - share of local token volume"))
    print()
    if not provider_rows:
        print("   No local usage found · run tokenmon scan")
    for row in provider_rows:
        share = row["total_tokens"] / total * 100 if total else 0
        total_text = f"{compact(row['total_tokens']):>8}"
        print(f"   {share:3.0f}%  {meter(share)}  {row['label'].title():8} {bold(total_text)}")

    def section(title: str, rows, top: int = 6) -> None:
        print()
        print("   " + bold(f"{title:<28}") + " % of usage")
        for row in rows[:top]:
            share = row["total_tokens"] / total * 100 if total else 0
            detail = f" - {row['detail']}" if row["detail"] else ""
            label = f"{row['label']}{detail}"
            print(f"   {label[:34]:34} {share:6.1f}%")
        if len(rows) > top:
            print(f"   + {len(rows)-top} more")

    section("Workspaces", workspace_rows)
    section("Clients", client_rows, 4)
    section("Devices", device_rows, 6)

    print()
    print("   " + bold("Usage allowance"))
    allowances = store.latest_allowances()
    if not allowances:
        print("   Claude Max   unavailable")
        print("   Codex Pro    unavailable")
    else:
        providers = {row["provider"] for row in allowances}
        if "claude" not in providers:
            print("   Claude Max   unavailable")
        for row in allowances:
            remaining = row["remaining_percent"]
            if remaining is None:
                print(f"   {row['provider'].title():12} unavailable")
            else:
                print(f"   {row['provider'].title():12} {meter(remaining)} {remaining:3.0f}% remaining")
                if row["resets_at"]:
                    print(f"   {'':12} resets {row['resets_at']}")

    print()
    print(" " + dim(rule()))
    print("   " + dim("devices: tokenmon devices  |  workspaces: tokenmon workspaces"))
    print("   " + dim("sessions: tokenmon sessions  |  live: tokenmon watch"))
    print("   " + dim("Local shares are not subscription allowance"))


def sync(store: Store) -> int:
    raw = os.environ.get("TOKENMON_SYNC_DIR")
    if not raw:
        print("TOKENMON_SYNC_DIR is not configured", file=sys.stderr)
        return 2
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    added = 0
    for path in root.glob("tokenmon-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("export_device_id") == store.device_id:
                continue
            added += store.import_payload(payload)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    target = root / f"tokenmon-{store.device_id}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(store.export_payload(), separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)
    print(f"sync complete: {added} peer events imported; export {target.name} updated")
    return 0


def doctor(store: Store) -> int:
    healthy = True
    print("TOKENMON DOCTOR")
    print(f"database: OK ({store.path})")
    print(f"device:   OK ({store.device_id})")
    for adapter in adapters():
        item = adapter.discover()
        source = "OK" if item["exists"] else "MISSING"
        command = "OK" if item["command"] else "MISSING"
        print(f"{item['provider']:8} source={source} command={command} root={item['root']}")
        healthy &= item["exists"]
    sync_dir = os.environ.get("TOKENMON_SYNC_DIR")
    print(f"sync:     {'OK ' + sync_dir if sync_dir else 'NOT CONFIGURED (optional)'}")
    return 0 if healthy else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokenmon", description="Local Claude Code and Codex usage monitor")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("scan")
    sub.add_parser("devices")
    sub.add_parser("workspaces")
    sub.add_parser("sessions")
    sub.add_parser("today")
    sub.add_parser("week")
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=int, default=30)
    sub.add_parser("sync")
    sub.add_parser("doctor")
    device = sub.add_parser("device")
    device_sub = device.add_subparsers(dest="device_command", required=True)
    rename = device_sub.add_parser("rename")
    rename.add_argument("name")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Store()
    try:
        if args.command == "scan":
            scan(store)
        elif args.command == "devices":
            summary(store, group="device")
        elif args.command == "workspaces":
            summary(store, group="workspace")
        elif args.command == "sessions":
            summary(store, group="session")
        elif args.command == "today":
            summary(store, days=1)
        elif args.command is None:
            dashboard(store)
        elif args.command == "week":
            summary(store)
        elif args.command == "watch":
            try:
                while True:
                    os.system("cls" if os.name == "nt" else "clear")
                    dashboard(store)
                    time.sleep(max(5, args.interval))
            except KeyboardInterrupt:
                return 0
        elif args.command == "sync":
            scan(store, quiet=True)
            return sync(store)
        elif args.command == "doctor":
            return doctor(store)
        elif args.command == "device" and args.device_command == "rename":
            if not args.name.strip():
                print("device name cannot be empty", file=sys.stderr)
                return 2
            store.rename_device(args.name)
            print(f"device renamed to {args.name.strip()}")
        return 0
    finally:
        store.close()

