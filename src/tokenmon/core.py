from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def app_home() -> Path:
    override = os.environ.get("TOKENMON_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "tokenmon"
    return Path.home() / ".local" / "share" / "tokenmon"


@dataclass(frozen=True)
class UsageEvent:
    id: str
    provider: str
    device_id: str
    session_id: str
    workspace_name: str
    workspace_hash: str
    client: str
    model: str | None
    observed_at: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    source_version: str | None


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY, display_name TEXT NOT NULL, platform TEXT NOT NULL,
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS usage_events (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, device_id TEXT NOT NULL,
  session_id TEXT NOT NULL, workspace_name TEXT NOT NULL, workspace_hash TEXT NOT NULL,
  client TEXT NOT NULL, model TEXT, observed_at TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, source_version TEXT, synced_at TEXT
);
CREATE INDEX IF NOT EXISTS usage_observed_idx ON usage_events(observed_at);
CREATE INDEX IF NOT EXISTS usage_device_idx ON usage_events(device_id);
CREATE TABLE IF NOT EXISTS source_files (
  path_hash TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  provider TEXT NOT NULL, scanned_at TEXT NOT NULL,
  read_offset INTEGER NOT NULL DEFAULT 0, parse_state TEXT
);
CREATE TABLE IF NOT EXISTS allowance_snapshots (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, allowance_type TEXT NOT NULL,
  remaining_percent REAL, resets_at TEXT, observed_at TEXT NOT NULL,
  source TEXT NOT NULL, status TEXT NOT NULL, device_id TEXT NOT NULL,
  first_seen_at TEXT
);
"""
# 1: source_files cursors. 2: allowance types follow window_minutes and readings keep first_seen_at.
# Raising it re-reads every session file once.
SCHEMA_VERSION = 2

# Codex reports cached input inside input, so cache is added only for Claude.
TOTAL_TOKENS_SQL = """CASE WHEN provider='codex'
    THEN COALESCE(input_tokens,0)+COALESCE(output_tokens,0)
    ELSE COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+
         COALESCE(cache_read_tokens,0)+COALESCE(cache_write_tokens,0) END"""


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or app_home() / "tokenmon.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._keys: dict[Path, str] = {}
        self.db.executescript(SCHEMA)
        self._migrate()
        self.device_id = self._ensure_device()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        def columns(table: str) -> set[str]:
            return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

        if "read_offset" not in columns("source_files"):
            self.db.execute("ALTER TABLE source_files ADD COLUMN read_offset INTEGER NOT NULL DEFAULT 0")
            self.db.execute("ALTER TABLE source_files ADD COLUMN parse_state TEXT")
        if "first_seen_at" not in columns("allowance_snapshots"):
            self.db.execute("ALTER TABLE allowance_snapshots ADD COLUMN first_seen_at TEXT")
            self.db.execute("UPDATE allowance_snapshots SET first_seen_at=observed_at")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # Old cursors are unknown or stale: re-read every file once. Event ids are stable, so nothing doubles.
            self.db.execute("DELETE FROM source_files")
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _ensure_device(self) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key='device_id'").fetchone()
        if row:
            device_id = row[0]
        else:
            device_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO settings(key,value) VALUES('device_id',?)", (device_id,))
        stamp = now_iso()
        name = socket.gethostname() or "device"
        self.db.execute(
            "INSERT INTO devices(id,display_name,platform,created_at,last_seen_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (device_id, name, platform.platform(), stamp, stamp),
        )
        self.db.commit()
        return device_id

    def rename_device(self, name: str) -> None:
        self.db.execute("UPDATE devices SET display_name=? WHERE id=?", (name.strip(), self.device_id))
        self.db.commit()

    def cursor(self, path: Path, stat: os.stat_result) -> tuple[int, int, dict[str, Any]] | None:
        """Where to resume reading `path`: (offset, line, parser state), or None if it is unchanged."""
        key = self._key(path)
        row = self.db.execute(
            "SELECT size,mtime_ns,read_offset,parse_state FROM source_files WHERE path_hash=?", (key,)).fetchone()
        if not row:
            return 0, 0, {}
        if row[0] == stat.st_size and row[1] == stat.st_mtime_ns:
            return None
        if stat.st_size < row[2]:  # shortcut: only a shrinking file counts as rewritten; same-size rewrites are missed
            return 0, 0, {}
        state = json.loads(row[3]) if row[3] else {}
        return row[2], state.pop("_line", 0), state

    def _key(self, path: Path) -> str:
        # resolve() costs a syscall per file; polling every few seconds made it the main idle CPU cost.
        key = self._keys.get(path)
        if key is None:
            key = self._keys[path] = stable_hash(str(path.resolve()).casefold())
        return key

    def save_cursor(self, path: Path, provider: str, stat: os.stat_result, offset: int,
                    state: dict[str, Any]) -> None:
        # `stat` was taken before reading, so lines appended during the read are picked up next time.
        key = self._key(path)
        self.db.execute(
            "INSERT INTO source_files(path_hash,size,mtime_ns,provider,scanned_at,read_offset,parse_state) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(path_hash) DO UPDATE SET size=excluded.size,"
            "mtime_ns=excluded.mtime_ns,provider=excluded.provider,scanned_at=excluded.scanned_at,"
            "read_offset=excluded.read_offset,parse_state=excluded.parse_state",
            (key, stat.st_size, stat.st_mtime_ns, provider, now_iso(), offset, json.dumps(state)),
        )

    def add_events(self, events: Iterable[UsageEvent]) -> int:
        before = self.db.total_changes
        # Claude streams one message as several records; later ones carry the larger output_tokens.
        self.db.executemany(
            "INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET "
            "output_tokens=excluded.output_tokens WHERE excluded.output_tokens > COALESCE(usage_events.output_tokens,-1)",
            ((e.id, e.provider, e.device_id, e.session_id, e.workspace_name, e.workspace_hash,
              e.client, e.model, e.observed_at, e.input_tokens, e.output_tokens,
              e.cache_read_tokens, e.cache_write_tokens, e.source_version) for e in events),
        )
        return self.db.total_changes - before

    def add_allowance(self, item: dict[str, Any]) -> None:
        # The same reading seen again only moves observed_at (last seen); first_seen_at keeps when it began.
        self.db.execute(
            "INSERT INTO allowance_snapshots(id,provider,allowance_type,remaining_percent,resets_at,observed_at,"
            "source,status,device_id,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "observed_at=excluded.observed_at WHERE excluded.observed_at > allowance_snapshots.observed_at",
            (item["id"], item["provider"], item["allowance_type"], item.get("remaining_percent"),
             item.get("resets_at"), item["observed_at"], item["source"], item["status"],
             item.get("device_id", self.device_id), item["observed_at"]),
        )

    def readings(self, provider: str, key: str, since: str, until: str) -> list[sqlite3.Row]:
        """Readings of one allowance type that first appeared within [since, until], oldest first."""
        return self.db.execute(
            "SELECT remaining_percent, first_seen_at, resets_at FROM allowance_snapshots "
            "WHERE provider=? AND allowance_type=? AND remaining_percent IS NOT NULL "
            "AND first_seen_at BETWEEN ? AND ? ORDER BY first_seen_at", (provider, key, since, until)).fetchall()

    def commit(self) -> None:
        self.db.commit()

    def latest_allowances(self) -> list[sqlite3.Row]:
        return self.db.execute("""
          SELECT a.* FROM allowance_snapshots a
          JOIN (SELECT provider, allowance_type, MAX(observed_at) observed_at
                FROM allowance_snapshots GROUP BY provider, allowance_type) latest
          ON a.provider=latest.provider AND a.allowance_type=latest.allowance_type
             AND a.observed_at=latest.observed_at
          ORDER BY a.provider,a.allowance_type
        """).fetchall()

    def usage(self, since: str, group: str) -> list[sqlite3.Row]:
        columns = {
            "device": "d.display_name label, e.provider detail",
            "workspace": "e.workspace_name label, e.provider detail",
            "session": "substr(e.session_id,1,12) label, e.provider || '/' || e.client detail",
            "provider": "e.provider label, 'local tokens' detail",
            "client": "e.client label, e.provider detail",
        }[group]
        return self.db.execute(f"""
          SELECT {columns}, SUM(COALESCE(input_tokens,0)) input_tokens,
            SUM(COALESCE(output_tokens,0)) output_tokens,
            SUM(COALESCE(cache_read_tokens,0)+COALESCE(cache_write_tokens,0)) cache_tokens,
            SUM({TOTAL_TOKENS_SQL}) total_tokens,
            MAX(observed_at) last_seen
          FROM usage_events e JOIN devices d ON d.id=e.device_id
          WHERE observed_at>=? GROUP BY label,detail ORDER BY total_tokens DESC
        """, (since,)).fetchall()

    def tokens_by_device(self, provider: str, since: str) -> dict[str, int]:
        """Total tokens since `since` per device, so a device's share of the account can be worked out."""
        rows = self.db.execute(
            f"SELECT device_id, SUM({TOTAL_TOKENS_SQL}) FROM usage_events "
            "WHERE provider=? AND observed_at>=? GROUP BY device_id", (provider, since)).fetchall()
        return {row[0]: row[1] or 0 for row in rows}

    def device_tokens_by_model(self, provider: str, since: str) -> dict[str | None, int]:
        """This device's total tokens per model since `since` (the credit rates are per model)."""
        rows = self.db.execute(
            f"SELECT model, SUM({TOTAL_TOKENS_SQL}) FROM usage_events WHERE provider=? AND device_id=? "
            "AND observed_at>=? GROUP BY model", (provider, self.device_id, since)).fetchall()
        return {row[0]: row[1] or 0 for row in rows}

    def export_payload(self) -> dict[str, Any]:
        devices = [dict(r) for r in self.db.execute("SELECT * FROM devices")]
        events = [dict(r) for r in self.db.execute("SELECT * FROM usage_events")]
        allowances = [dict(r) for r in self.db.execute("SELECT * FROM allowance_snapshots")]
        return {"schema": 1, "export_device_id": self.device_id,
                "exported_at": now_iso(), "devices": devices,
                "usage_events": events, "allowance_snapshots": allowances}

    def import_payload(self, payload: dict[str, Any]) -> int:
        if payload.get("schema") != 1:
            raise ValueError("unsupported sync schema")
        incoming = payload.get("devices", [])
        known = {r[0] for r in self.db.execute("SELECT id FROM devices")}
        new_ids = {d["id"] for d in incoming} - known
        if len(known | new_ids) > 3:
            raise ValueError("MVP supports at most three devices")
        self.db.executemany(
            "INSERT INTO devices VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "display_name=excluded.display_name,last_seen_at=excluded.last_seen_at,active=excluded.active",
            ((d["id"], d["display_name"], d["platform"], d["created_at"], d["last_seen_at"], d["active"])
             for d in incoming),
        )
        before = self.db.total_changes
        cols = ("id","provider","device_id","session_id","workspace_name","workspace_hash","client",
                "model","observed_at","input_tokens","output_tokens","cache_read_tokens",
                "cache_write_tokens","source_version","synced_at")
        self.db.executemany(
            "INSERT OR IGNORE INTO usage_events VALUES(" + ",".join("?" for _ in cols) + ")",
            (tuple(e.get(c) for c in cols) for e in payload.get("usage_events", [])),
        )
        added = self.db.total_changes - before
        acols = ("id","provider","allowance_type","remaining_percent","resets_at","observed_at",
                 "source","status","device_id")
        self.db.executemany(
            "INSERT OR IGNORE INTO allowance_snapshots(" + ",".join(acols) + ",first_seen_at) VALUES("
            + ",".join("?" for _ in range(len(acols) + 1)) + ")",
            (tuple(a.get(c) for c in acols) + (a.get("first_seen_at") or a.get("observed_at"),)
             for a in payload.get("allowance_snapshots", [])),
        )
        self.db.commit()
        return added


class Tail:
    """Yields (line_number, record) for the JSON lines of `path` after byte `offset`.

    Line numbers count every physical line (blank or broken too), as a full read always did, so event
    ids stay stable. After iterating, `offset` and `line` sit past everything consumed. A last line
    without a newline that is not valid JSON yet is left for the next read.
    """

    def __init__(self, path: Path, offset: int = 0, line: int = 0):
        self.path, self.offset, self.line = path, offset, line

    def __iter__(self) -> Iterator[tuple[int, dict[str, Any]]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                for raw in handle:
                    complete = raw.endswith(b"\n")
                    text = raw.decode("utf-8", errors="replace").strip()
                    value = None
                    if text:
                        try:
                            value = json.loads(text)
                        except json.JSONDecodeError:
                            if not complete:
                                return
                    elif not complete:
                        return
                    # shortcut: a valid last line without "\n" is taken now; its "\n" arriving later counts
                    # as one extra blank line (only shifts Codex line numbers, once, for such files).
                    self.offset += len(raw)
                    self.line += 1
                    if isinstance(value, dict):
                        yield self.line, value
        except OSError:
            return

