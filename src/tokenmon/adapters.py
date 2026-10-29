from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .core import Store, Tail, UsageEvent, app_home, stable_hash

USAGE_EVERY = 120  # seconds between `claude /usage` runs; each takes about a second and costs no tokens
USAGE_KINDS = {"session": "five_hour", "weekly_all": "seven_day"}


def event_id(provider: str, path: Path, line: int, native_id: str | None) -> str:
    raw = f"{provider}|{path.resolve()}|{line}|{native_id or ''}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def workspace(cwd: str | None, fallback: str) -> tuple[str, str]:
    raw = cwd or fallback
    path = Path(raw)
    name = path.name or "workspace"
    return name, stable_hash(str(path).casefold())


def number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_usage_command() -> list[str] | None:
    """Command that prints Claude Code's plan usage as JSON (`/usage`), using Claude Code's own login."""
    exe = shutil.which("claude")
    if not exe:
        # The VS Code extension ships its own binary even when `claude` is not on PATH.
        home = Path.home()
        found = [p for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf")
                 for p in (home / editor / "extensions").glob("anthropic.claude-code-*/resources/native-binary/claude*")
                 if p.is_file()]
        exe = str(max(found, key=lambda p: p.stat().st_mtime)) if found else None
    if not exe:
        return None
    return [exe, "-p", "/usage", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]


def scan_files(store: Store, root: Path, provider: str, handle: Callable[[Path, Tail, dict], int]) -> tuple[int, int]:
    """Feed `handle` only the lines appended to each session file since the last scan."""
    added = files = 0
    # os.walk is about twice as fast as Path.rglob, which matters when this runs every couple of seconds.
    for folder, _dirs, names in os.walk(root):
        for name in names:
            if not name.lower().endswith(".jsonl"):
                continue
            path = Path(folder, name)
            try:
                stat = path.stat()
            except OSError:
                continue
            cursor = store.cursor(path, stat)
            if cursor is None:
                continue
            offset, line, state = cursor
            tail = Tail(path, offset, line)
            added += handle(path, tail, state)
            store.save_cursor(path, provider, stat, tail.offset, {**state, "_line": tail.line})
            files += 1
    store.commit()
    return added, files


class ClaudeAdapter:
    provider = "claude"

    def __init__(self, root: Path | None = None, limits: Path | None = None,
                 poll_usage: bool = False, usage_command: list[str] | None = None):
        self.root = root or claude_home() / "projects"
        # Written by the Claude Code status line script that `connect_claude` installs.
        self.limits = limits or app_home() / "claude-statusline.json"
        self.poll_usage, self.usage_command = poll_usage, usage_command
        self.note = ""  # why plan usage is missing, shown on the Claude card
        self._pending: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self._polled = float("-inf")

    def discover(self) -> dict[str, Any]:
        return {"provider": self.provider, "root": str(self.root), "exists": self.root.is_dir(),
                "command": shutil.which("claude")}

    def read_usage(self, store: Store) -> None:
        """Store the result of the last background `/usage` run, and start the next one when due."""
        pending, self._pending = self._pending, []
        for item in pending:
            store.add_allowance(item)
        if (self._thread and self._thread.is_alive()) or time.monotonic() - self._polled < USAGE_EVERY:
            return
        self._polled = time.monotonic()
        self._thread = threading.Thread(target=self._fetch_usage, args=(store.device_id,), daemon=True)
        self._thread.start()

    def _fetch_usage(self, device_id: str) -> None:
        command = self.usage_command or claude_usage_command()
        if not command:
            self.note = "claude.exe tidak ditemukan"
            return
        try:
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=30, cwd=tempfile.gettempdir(),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.note = f"/usage gagal: {exc.__class__.__name__}"
            return
        limits = None
        for line in run.stdout.splitlines():
            try:
                report = json.loads(line).get("usage_report")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(report, dict):
                limits = (report.get("rate_limits") or {}).get("limits")
                break
        if not isinstance(limits, list):
            self.note = "/usage tidak memberi data kuota (sudah login dengan akun claude.ai?)"
            return
        snapshots = []
        for item in limits:
            key = USAGE_KINDS.get(item.get("kind")) if isinstance(item, dict) else None
            percent, reset = (item.get("percent"), item.get("resets_at")) if key else (None, None)
            if not isinstance(percent, (int, float)):
                continue
            try:
                # The server's reset time jitters by a fraction of a second; round to the minute so an
                # unchanged reading keeps one id instead of adding a row per poll.
                moment = datetime.fromisoformat(str(reset).replace("Z", "+00:00")) + timedelta(seconds=30)
                resets_at = moment.replace(second=0, microsecond=0).isoformat()
            except ValueError:
                resets_at = None
            raw = f"claude|{key}|{resets_at}|{percent}|{device_id}|usage"
            snapshots.append({
                "id": hashlib.sha256(raw.encode()).hexdigest(), "provider": "claude", "allowance_type": key,
                "remaining_percent": max(0.0, 100.0 - float(percent)), "resets_at": resets_at,
                "observed_at": datetime.now(timezone.utc).isoformat(), "source": "claude-usage",
                "status": "official-local-snapshot", "device_id": device_id,
            })
        self.note = "" if snapshots else "/usage tidak memuat sesi 5 jam atau mingguan"
        self._pending = snapshots

    def scan(self, store: Store) -> tuple[int, int]:
        self.read_limits(store)
        if self.poll_usage:
            self.read_usage(store)

        def handle(path: Path, tail: Tail, state: dict) -> int:
            events, allowances = self.parse(path, tail, store.device_id)
            for item in allowances:
                store.add_allowance(item)
            return store.add_events(events)

        return scan_files(store, self.root, self.provider, handle)

    def read_limits(self, store: Store) -> None:
        try:
            data = json.loads(self.limits.read_text(encoding="utf-8"))
            observed = datetime.fromtimestamp(self.limits.stat().st_mtime, timezone.utc).isoformat()
        except (OSError, json.JSONDecodeError):
            return
        limits = data.get("rate_limits") if isinstance(data, dict) else None
        if not isinstance(limits, dict):
            return
        for key in ("five_hour", "seven_day"):
            limit = limits.get(key)
            used = limit.get("used_percentage") if isinstance(limit, dict) else None
            if not isinstance(used, (int, float)):
                continue
            reset = limit.get("resets_at")
            resets_at = datetime.fromtimestamp(reset, timezone.utc).isoformat() if isinstance(reset, (int, float)) else None
            raw = f"claude|{key}|{resets_at}|{used}|{store.device_id}"
            store.add_allowance({
                "id": hashlib.sha256(raw.encode()).hexdigest(), "provider": "claude",
                "allowance_type": key, "remaining_percent": max(0.0, 100.0 - float(used)),
                "resets_at": resets_at, "observed_at": observed, "source": "claude-statusline",
                "status": "official-local-snapshot",
            })

    def parse(self, path: Path, tail: Tail, device_id: str) -> tuple[list[UsageEvent], list[dict[str, Any]]]:
        events: list[UsageEvent] = []
        allowances: list[dict[str, Any]] = []
        for line, row in tail:
            # Session files carry no usage percent, only the limit that was hit (status "rejected").
            params = row.get("apiErrorParams")
            info = row.get("quotaLimits") or (params.get("rate_limit_info") if isinstance(params, dict) else None)
            if isinstance(info, dict) and info.get("status") == "rejected":
                key, reset = info.get("rateLimitType"), info.get("resetsAt")
                if key in ("five_hour", "seven_day") and isinstance(reset, (int, float)):
                    resets_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                    raw = f"claude|{key}|{resets_at}|rejected|{device_id}"
                    allowances.append({
                        "id": hashlib.sha256(raw.encode()).hexdigest(), "provider": "claude",
                        "allowance_type": key, "remaining_percent": 0.0, "resets_at": resets_at,
                        "observed_at": str(row.get("timestamp") or datetime.now(timezone.utc).isoformat()),
                        "source": "claude-session-limit", "status": "official-local-snapshot",
                        "device_id": device_id,
                    })
            message = row.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if row.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            session = str(row.get("sessionId") or row.get("session_id") or path.stem)
            name, path_hash = workspace(row.get("cwd"), path.parent.name)
            entrypoint = str(row.get("entrypoint") or "unknown").lower()
            client = "vscode" if "vscode" in entrypoint or "ide" in entrypoint else (
                "cli" if entrypoint == "cli" else "unknown")
            events.append(UsageEvent(
                # Claude may emit the same API message in multiple JSONL records.
                event_id(self.provider, path, 0, str(message.get("id") or row.get("uuid") or line)),
                self.provider, device_id, session, name, path_hash, client, message.get("model"),
                str(row.get("timestamp") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()),
                number(usage.get("input_tokens")), number(usage.get("output_tokens")),
                number(usage.get("cache_read_input_tokens")),
                number(usage.get("cache_creation_input_tokens")), row.get("version"),
            ))
        return events, allowances


class CodexAdapter:
    provider = "codex"

    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"

    def discover(self) -> dict[str, Any]:
        return {"provider": self.provider, "root": str(self.root), "exists": self.root.is_dir(),
                "command": shutil.which("codex")}

    def scan(self, store: Store) -> tuple[int, int]:
        def handle(path: Path, tail: Tail, state: dict) -> int:
            events, allowances = self.parse(path, tail, store.device_id, state)
            added = store.add_events(events)
            for item in allowances:
                store.add_allowance(item)
            return added

        return scan_files(store, self.root, self.provider, handle)

    def parse(self, path: Path, tail: Tail, device_id: str,
              state: dict) -> tuple[list[UsageEvent], list[dict[str, Any]]]:
        """`state` carries session/cwd/model/client between scans: they are set once, early in the file."""
        events: list[UsageEvent] = []
        allowances: list[dict[str, Any]] = []
        session = state.get("session", path.stem)
        cwd: str | None = state.get("cwd")
        model: str | None = state.get("model")
        client = state.get("client", "unknown")
        for line, row in tail:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("cwd"):
                cwd = payload["cwd"]
            settings = payload.get("thread_settings") or payload.get("state") or {}
            if isinstance(settings, dict):
                cwd = settings.get("cwd") or cwd
                model = settings.get("model") or model
            session = str(payload.get("session_id") or session)
            origin = str(payload.get("originator") or payload.get("source") or "").lower()
            if "vscode" in origin:
                client = "vscode"
            elif origin:
                client = "cli" if "cli" in origin else client

            # `usage` is the request delta; turn/thread fields are cumulative snapshots.
            usage = payload.get("usage") if row.get("type") == "token_usage_record" else None
            if isinstance(usage, dict):
                name, path_hash = workspace(cwd, path.parent.name)
                events.append(UsageEvent(
                    event_id(self.provider, path, line, str(payload.get("turn_id") or row.get("ordinal") or "")),
                    self.provider, device_id, session, name, path_hash, client, model,
                    str(row.get("timestamp") or payload.get("timestamp") or
                        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()),
                    number(usage.get("input_tokens")), number(usage.get("output_tokens")),
                    number(usage.get("cached_input_tokens")), number(usage.get("cache_write_input_tokens")),
                    "token_usage_record-v1",
                ))

            if payload.get("type") == "token_count" and isinstance(payload.get("rate_limits"), dict):
                observed = str(row.get("timestamp") or datetime.now(timezone.utc).isoformat())
                for key in ("primary", "secondary"):
                    limit = payload["rate_limits"].get(key)
                    if not isinstance(limit, dict) or not isinstance(limit.get("used_percent"), (int, float)):
                        continue
                    reset = limit.get("resets_at")
                    resets_at = datetime.fromtimestamp(reset, timezone.utc).isoformat() if isinstance(reset, (int, float)) else None
                    # Name the window by its length (this account's only window is the weekly 10080 minutes).
                    kind = {10080: "seven_day", 300: "five_hour"}.get(limit.get("window_minutes"), key)
                    raw = f"codex|{kind}|{observed}|{limit.get('used_percent')}|{device_id}"
                    allowances.append({
                        "id": hashlib.sha256(raw.encode()).hexdigest(), "provider": "codex",
                        "allowance_type": kind, "remaining_percent": max(0.0, 100.0-float(limit["used_percent"])),
                        "resets_at": resets_at, "observed_at": observed, "source": "codex-session-rate-limits",
                        "status": "official-local-snapshot", "device_id": device_id,
                    })
        state.update(session=session, cwd=cwd, model=model, client=client)
        return events, allowances


def adapters(poll_usage: bool = False) -> list[ClaudeAdapter | CodexAdapter]:
    """`poll_usage` is for long-running callers (the GUI); a one-shot CLI scan would exit before it finishes."""
    return [ClaudeAdapter(poll_usage=poll_usage), CodexAdapter()]


def connect_claude(settings: Path | None = None) -> str:
    """Install a status line that saves Claude Code's rate_limits JSON for Tokenmon."""
    settings = settings or claude_home() / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return f"Tidak diubah: {settings} tidak bisa dibaca ({exc})."
    if not isinstance(data, dict):
        return f"Tidak diubah: isi {settings} bukan objek JSON."
    home = app_home()
    script = home / "claude-statusline.ps1"
    command = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script.as_posix()}"'
    current = data.get("statusLine")
    if current:
        existing = str(current.get("command") if isinstance(current, dict) else "")
        if "claude-statusline.ps1" not in existing:
            return "Tidak diubah: settings.json sudah punya statusLine lain."
        if existing == command:
            return "Claude sudah terhubung."
        # Ours, but pointing at another folder (e.g. a moved TOKENMON_HOME): fall through and repair it.
    target = str(home / "claude-statusline.json").replace("'", "''")
    try:
        home.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$raw = [Console]::In.ReadToEnd()\n"
            f"$out = '{target}'\n"
            "[IO.File]::WriteAllText(\"$out.tmp\", $raw)\n"
            "Move-Item -Force \"$out.tmp\" $out\n",
            encoding="utf-8",
        )
        backup = settings.with_name(settings.name + ".tokenmon-backup")
        if settings.exists() and not backup.exists():  # never overwrite an earlier backup
            shutil.copy2(settings, backup)
        data["statusLine"] = {"type": "command", "command": command}
        settings.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings.with_name(settings.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(settings)
    except OSError as exc:
        return f"Gagal menghubungkan Claude: {exc}"
    return "Claude terhubung. Buka ulang Claude Code, lalu kirim satu pesan agar data kuota muncul."

