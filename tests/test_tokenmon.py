import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenmon import __version__
from tokenmon.adapters import ClaudeAdapter, CodexAdapter, connect_claude
from tokenmon.cli import sync
from tokenmon.core import SCHEMA_VERSION, Store, UsageEvent
from tokenmon.gauge import Spring
from tokenmon.credits import weekly_capacity
from tokenmon.periods import credits_since, local_midnight, period_report


class TokenmonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "tokenmon.db")
        self.cfg = self.root / "cfg"  # stands in for the Claude config dir; empty unless a test writes a config
        self.cfg.mkdir()
        self._old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)

    def tearDown(self):
        if self._old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_config_dir
        self.store.close()
        self.temp.cleanup()

    def test_claude_parser_and_deduplication(self):
        source = self.root / "claude" / "project" / "session.jsonl"
        source.parent.mkdir(parents=True)
        record = {
            "type": "assistant", "uuid": "turn-1", "sessionId": "session-1",
            "timestamp": "2026-10-28T00:00:00+00:00", "cwd": "C:/secret/acme",
            "entrypoint": "cli", "version": "1", "message": {
                "model": "sonnet", "usage": {"input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2},
                "content": [{"text": "must never be stored"}],
            },
        }
        source.write_text(json.dumps(record) + "\n", encoding="utf-8")
        adapter = ClaudeAdapter(self.root / "claude")
        first, _ = adapter.scan(self.store)
        source.touch()
        second, _ = adapter.scan(self.store)
        self.assertEqual((first, second), (1, 0))
        row = self.store.db.execute("SELECT * FROM usage_events").fetchone()
        self.assertEqual(row["workspace_name"], "acme")
        self.assertNotIn("secret", repr(dict(row)))
        self.assertNotIn("must never", repr(dict(row)))

    def test_codex_parser_and_allowance(self):
        source = self.root / "codex" / "2026" / "session.jsonl"
        source.parent.mkdir(parents=True)
        records = [
            {"type": "session_meta", "payload": {"session_id": "s1", "cwd": "/private/repo"}},
            {"type": "token_usage_record", "timestamp": "2026-10-28T00:00:00+00:00",
             "payload": {"turn_id": "t1", "usage": {"input_tokens": 20,
                "output_tokens": 8, "cached_input_tokens": 4, "cache_write_input_tokens": 1},
                "turn_token_usage": {"input_tokens": 200, "output_tokens": 80}}},
            {"type": "event_msg", "timestamp": "2026-10-28T00:01:00+00:00",
             "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 25, "resets_at": 1793145600}}}},
        ]
        source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        added, _ = CodexAdapter(self.root / "codex").scan(self.store)
        self.assertEqual(added, 1)
        usage = self.store.db.execute("SELECT input_tokens FROM usage_events").fetchone()
        self.assertEqual(usage["input_tokens"], 20)
        aggregate = self.store.usage("2026-01-01T00:00:00+00:00", "device")[0]
        self.assertEqual(aggregate["total_tokens"], 28)
        self.assertEqual(aggregate["cache_tokens"], 5)
        allowance = self.store.latest_allowances()[0]
        self.assertEqual(allowance["remaining_percent"], 75)

    @staticmethod
    def claude_record(uid, output, message_id=None, **extra):
        return {"type": "assistant", "uuid": uid, "sessionId": "s", "cwd": "/x/app", "entrypoint": "cli",
                "timestamp": "2026-10-29T00:00:00+00:00",
                "message": {"id": message_id, "model": "m", "usage": {"input_tokens": 1, "output_tokens": output}},
                **extra}

    def test_incremental_tail_reads_only_new_lines(self):
        source = self.root / "claude" / "p" / "s.jsonl"
        source.parent.mkdir(parents=True)
        adapter = ClaudeAdapter(self.root / "claude", limits=self.root / "none.json")
        source.write_text(json.dumps(self.claude_record("a", 1)) + "\n", encoding="utf-8")
        self.assertEqual(adapter.scan(self.store)[0], 1)
        half = json.dumps(self.claude_record("c", 3))
        with source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.claude_record("b", 2)) + "\n" + half[:20])
        self.assertEqual(adapter.scan(self.store)[0], 1)  # "c" is still being written
        with source.open("a", encoding="utf-8") as handle:
            handle.write(half[20:] + "\n")
        self.assertEqual(adapter.scan(self.store)[0], 1)
        self.assertEqual(adapter.scan(self.store)[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 3)

    def test_claude_streaming_keeps_final_output(self):
        source = self.root / "claude" / "p" / "s.jsonl"
        source.parent.mkdir(parents=True)
        adapter = ClaudeAdapter(self.root / "claude", limits=self.root / "none.json")
        lines = [self.claude_record("u1", 3, "msg1"), self.claude_record("u2", 120, "msg1")]
        source.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
        adapter.scan(self.store)
        with source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.claude_record("u3", 50, "msg1")) + "\n")
        adapter.scan(self.store)
        rows = self.store.db.execute("SELECT output_tokens FROM usage_events").fetchall()
        self.assertEqual([r[0] for r in rows], [120])

    def test_claude_session_limit_hit_becomes_allowance(self):
        source = self.root / "claude" / "p" / "s.jsonl"
        source.parent.mkdir(parents=True)
        hit = {"type": "assistant", "timestamp": "2026-10-29T01:00:00+00:00", "quotaLimits": {
            "status": "rejected", "resetsAt": 1793145600, "rateLimitType": "seven_day"}}
        source.write_text(json.dumps(hit) + "\n", encoding="utf-8")
        ClaudeAdapter(self.root / "claude", limits=self.root / "none.json").scan(self.store)
        row = self.store.latest_allowances()[0]
        self.assertEqual((row["provider"], row["allowance_type"], row["remaining_percent"]),
                         ("claude", "seven_day", 0.0))

    def test_codex_state_survives_incremental_scan(self):
        source = self.root / "codex" / "2026" / "session.jsonl"
        source.parent.mkdir(parents=True)
        adapter = CodexAdapter(self.root / "codex")
        source.write_text(json.dumps({"type": "session_meta", "payload": {
            "session_id": "s1", "cwd": "/private/repo"}}) + "\n", encoding="utf-8")
        self.assertEqual(adapter.scan(self.store)[0], 0)
        with source.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "token_usage_record", "timestamp": "2026-10-29T00:00:00+00:00",
                                     "payload": {"turn_id": "t1", "usage": {
                                         "input_tokens": 5, "output_tokens": 2}}}) + "\n")
        self.assertEqual(adapter.scan(self.store)[0], 1)
        row = self.store.db.execute("SELECT workspace_name, session_id FROM usage_events").fetchone()
        self.assertEqual((row[0], row[1]), ("repo", "s1"))

    def test_migration_from_old_source_files(self):
        old = self.root / "old.db"
        raw = sqlite3.connect(old)
        raw.executescript(
            "CREATE TABLE source_files (path_hash TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
            " provider TEXT NOT NULL, scanned_at TEXT NOT NULL);"
            "INSERT INTO source_files VALUES('h',1,1,'claude','x');")
        raw.close()
        migrated = Store(old)
        try:
            columns = {r[1] for r in migrated.db.execute("PRAGMA table_info(source_files)")}
            self.assertIn("read_offset", columns)
            self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 0)
        finally:
            migrated.close()

    # ---- per-day / per-week usage -------------------------------------------------------------

    def put(self, key, used, first, last=None, reset=None, provider="claude"):
        """Store one allowance reading first seen at `first`; `last` simulates it being seen again later."""
        item = {"id": f"{provider}-{key}-{used}-{first.isoformat()}", "provider": provider, "allowance_type": key,
                "remaining_percent": 100 - used, "resets_at": reset.isoformat() if reset else None,
                "observed_at": first.isoformat(), "source": "test", "status": "test"}
        self.store.add_allowance(item)
        if last:
            self.store.add_allowance({**item, "observed_at": last.isoformat()})

    def times(self):
        midnight = local_midnight(datetime.now(timezone.utc))
        return midnight, midnight + timedelta(hours=10)  # "now" is 10 hours into the local day

    def test_day_usage_uses_reading_from_before_midnight(self):
        midnight, now = self.times()
        reset = now + timedelta(days=3)
        self.put("seven_day", 30, midnight - timedelta(hours=2), reset=reset)
        # 40% began before midnight and was seen again after it: its first_seen_at must still win.
        self.put("seven_day", 40, midnight - timedelta(hours=1), last=midnight + timedelta(minutes=30), reset=reset)
        self.put("seven_day", 46, now - timedelta(minutes=10), reset=reset)
        report = period_report(self.store, "claude", now)
        self.assertEqual((report["week"]["official"], report["day"]["official"], report["day"]["note"]),
                         (46, 6, "sejak 00:00"))

    def test_day_usage_without_midnight_reading_says_where_it_starts(self):
        midnight, now = self.times()
        reset = now + timedelta(days=3)
        self.put("seven_day", 44, midnight + timedelta(hours=2), reset=reset)
        self.put("seven_day", 46, midnight + timedelta(hours=5), reset=reset)
        day = period_report(self.store, "claude", now)["day"]
        self.assertEqual(day["official"], 2)
        self.assertEqual(day["note"], "sejak " + (midnight + timedelta(hours=2)).astimezone().strftime("%H:%M"))

    def test_day_usage_after_weekly_reset_starts_at_zero(self):
        midnight, now = self.times()
        reset = midnight + timedelta(hours=3, days=7)  # the window began at 03:00 today
        self.put("seven_day", 90, midnight - timedelta(hours=5), reset=midnight + timedelta(hours=3))  # last window
        self.put("seven_day", 5, midnight + timedelta(hours=4), reset=reset)
        day = period_report(self.store, "claude", now)["day"]
        self.assertEqual((day["official"], day["note"]), (5, "sejak reset"))

    def test_usage_report_without_data_is_empty(self):
        _midnight, now = self.times()
        report = period_report(self.store, "claude", now)
        self.assertEqual((report["week"]["pct"], report["day"]["pct"], report["week"]["tokens"]), (None, None, 0))

    def test_week_tokens_start_at_the_window_start(self):
        midnight, now = self.times()
        reset = now + timedelta(days=3)
        week_start = reset - timedelta(days=7)
        self.put("seven_day", 20, now - timedelta(hours=1), reset=reset)

        def event(name, when_, tokens):
            return UsageEvent(name, "claude", self.store.device_id, "s", "app", "h", "cli", "m",
                              when_.isoformat(), tokens, 0, 0, 0, "t")

        self.store.add_events([event("old", week_start - timedelta(hours=1), 1000),
                               event("week", week_start + timedelta(hours=1), 200),
                               event("today", midnight + timedelta(hours=2), 30)])
        report = period_report(self.store, "claude", now)
        self.assertEqual((report["week"]["tokens"], report["day"]["tokens"]), (230, 30))

    # ---- this device's own part: Claude credits over the plan's weekly capacity ---------------------

    def add_peer(self):
        """A second device that sync has told us about."""
        self.store.db.execute("INSERT INTO devices VALUES('peer','Peer','x','x','x',1)")

    @staticmethod
    def event(name, device, when_, tokens, provider="claude", model="m"):
        return UsageEvent(name, provider, device, "s", "app", "h", "cli", model, when_.isoformat(), tokens, 0, 0, 0, "t")

    def claude_config(self, tier="t", five_hour=None, rates=None):
        """A fake ~/.claude.json: model "m" costs 1 credit per token, plan "t" holds 1000 credits per 5 hours."""
        data = {"organizationRateLimitTier": tier, "cachedGrowthBookFeatures": {"tengu_amber_tally": {
            "fiveHourCredits": five_hour if five_hour is not None else {"t": 1000},
            "creditsPerMillionTokens": rates if rates is not None else {"m": 1_000_000}}}}
        (self.cfg / ".claude.json").write_text(json.dumps(data), encoding="utf-8")

    def test_credits_follow_the_model_rates_and_count_cache_tokens(self):
        _midnight, now = self.times()
        self.claude_config(rates={"model-a": 1000, "model-b": 2000})
        when_, me = (now - timedelta(hours=1)).isoformat(), self.store.device_id
        self.store.add_events([
            UsageEvent("a", "claude", me, "s", "x", "h", "cli", "model-a", when_, 100_000, 50_000, 800_000, 50_000, "t"),
            UsageEvent("b", "claude", me, "s", "x", "h", "cli", "model-b", when_, 500_000, 0, 0, 0, "t"),
            UsageEvent("c", "claude", me, "s", "x", "h", "cli", "unlisted", when_, 10_000_000, 0, 0, 0, "t"),  # no rate: 0
        ])
        self.assertAlmostEqual(credits_since(self.store, "claude", now - timedelta(days=1)), 1000 + 1000)

    def test_weekly_capacity_is_five_times_the_five_hour_credits_of_the_tier(self):
        self.assertIsNone(weekly_capacity())  # no config at all
        self.claude_config(tier="max20", five_hour={"max5": 50_000, "max20": 200_000})
        self.assertEqual(weekly_capacity(), 1_000_000)
        self.claude_config(tier="max5", five_hour={"max5": 50_000, "max20": 200_000})
        self.assertEqual(weekly_capacity(), 250_000)
        self.claude_config(tier="something-new", five_hour={"max5": 50_000, "max20": 200_000})
        self.assertIsNone(weekly_capacity())  # unknown plan: no guess

    def test_claude_gauge_is_credits_over_the_weekly_capacity(self):
        midnight, now = self.times()
        reset = now + timedelta(days=3)
        self.claude_config()  # capacity 5000 credits
        self.put("seven_day", 30, now - timedelta(minutes=5), reset=reset)
        me = self.store.device_id
        self.store.add_events([self.event("w", me, reset - timedelta(days=7) + timedelta(hours=1), 1500),
                               self.event("d", me, midnight + timedelta(hours=2), 1000)])
        report = period_report(self.store, "claude", now)
        self.assertEqual((report["week"]["pct"], report["day"]["pct"]), (50, 20))  # 2500 / 5000 and 1000 / 5000
        self.assertEqual(report["week"]["official"], 30)                           # the account figure stays
        self.assertEqual((report["device"]["basis"], report["device"]["capacity"]), ("kredit", 5000))

    def test_peer_events_do_not_add_credits(self):
        midnight, now = self.times()
        self.claude_config()
        self.add_peer()
        self.put("seven_day", 30, now - timedelta(minutes=5), reset=now + timedelta(days=3))
        self.store.add_events([self.event("p", "peer", midnight + timedelta(hours=2), 4000),
                               self.event("m", self.store.device_id, midnight + timedelta(hours=3), 500)])
        self.assertEqual(period_report(self.store, "claude", now)["week"]["pct"], 10)  # only 500 of 5000

    def test_without_a_known_plan_the_gauge_falls_back_to_the_account_percent(self):
        _midnight, now = self.times()
        self.put("seven_day", 46, now - timedelta(minutes=5), reset=now + timedelta(days=3))  # no config written
        report = period_report(self.store, "claude", now)
        self.assertEqual((report["week"]["pct"], report["device"]["basis"], report["device"]["capacity"]),
                         (46, "akun", None))

    def test_codex_has_no_credit_table_so_its_gauge_is_the_account_percent(self):
        _midnight, now = self.times()
        self.claude_config()
        self.put("seven_day", 14, now - timedelta(minutes=5), reset=now + timedelta(days=3), provider="codex")
        report = period_report(self.store, "codex", now)
        self.assertEqual((report["week"]["pct"], report["device"]["basis"]), (14, "akun"))

    def test_tokens_by_device_uses_the_same_total_as_usage(self):
        _midnight, now = self.times()
        when_ = (now - timedelta(hours=1)).isoformat()
        me = self.store.device_id
        self.store.add_events([
            UsageEvent("c", "claude", me, "s", "a", "h", "cli", "m", when_, 10, 5, 3, 2, "t"),   # cache counts for Claude
            UsageEvent("x", "codex", me, "s", "a", "h", "cli", "m", when_, 10, 5, 3, 0, "t"),    # but not for Codex
        ])
        since = (now - timedelta(days=1)).isoformat()
        self.assertEqual(self.store.tokens_by_device("claude", since), {me: 20})
        self.assertEqual(self.store.tokens_by_device("codex", since), {me: 15})
        totals = {r["label"]: r["total_tokens"] for r in self.store.usage(since, "provider")}
        self.assertEqual(totals, {"claude": 20, "codex": 15})

    def test_codex_window_minutes_name_the_allowance(self):
        source = self.root / "codex" / "2026" / "s.jsonl"
        source.parent.mkdir(parents=True)
        limits = {"primary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": 1793774273},
                  "secondary": {"used_percent": 3.0, "window_minutes": 300, "resets_at": 1793000000}}
        source.write_text(json.dumps({"type": "event_msg", "timestamp": "2026-10-29T01:00:00Z",
                                      "payload": {"type": "token_count", "rate_limits": limits}}) + "\n",
                          encoding="utf-8")
        CodexAdapter(self.root / "codex").scan(self.store)
        found = {r["allowance_type"]: r["remaining_percent"] for r in self.store.latest_allowances()}
        self.assertEqual(found, {"seven_day": 92.0, "five_hour": 97.0})

    def test_migration_adds_first_seen_and_forces_a_reread(self):
        old = self.root / "v1.db"
        raw = sqlite3.connect(old)
        raw.executescript(
            "CREATE TABLE source_files (path_hash TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,"
            " provider TEXT NOT NULL, scanned_at TEXT NOT NULL, read_offset INTEGER NOT NULL DEFAULT 0, parse_state TEXT);"
            "INSERT INTO source_files VALUES('h',1,1,'codex','x',5,'{}');"
            "CREATE TABLE allowance_snapshots (id TEXT PRIMARY KEY, provider TEXT NOT NULL, allowance_type TEXT NOT NULL,"
            " remaining_percent REAL, resets_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,"
            " device_id TEXT NOT NULL);"
            "INSERT INTO allowance_snapshots VALUES('a','codex','primary',90,NULL,'2026-10-29T01:00:00Z','s','s','d');"
            "PRAGMA user_version = 1;")
        raw.close()
        migrated = Store(old)
        try:
            self.assertEqual(migrated.db.execute("SELECT first_seen_at FROM allowance_snapshots").fetchone()[0],
                             "2026-10-29T01:00:00Z")
            self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 0)
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            migrated.close()

    def test_spring_settles_and_keeps_velocity_when_retargeted(self):
        spring = Spring(0.0)
        spring.set_target(80.0)
        for _ in range(15):
            spring.step(1 / 60)
        self.assertTrue(0 < spring.value < 80 and spring.velocity > 0)
        speed = spring.velocity
        spring.set_target(60.0)  # interrupted mid-flight: position and velocity carry over
        self.assertEqual(spring.velocity, speed)
        for _ in range(240):
            spring.step(1 / 60)
        self.assertTrue(spring.settled and spring.value == 60.0)

    def test_version_has_a_single_source(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIsNone(re.search(r'^version\s*=\s*"', text, re.M))

    def test_claude_usage_command_output_becomes_allowance(self):
        report = {"type": "system", "usage_report": {"rate_limits": {"limits": [
            {"kind": "session", "percent": 16, "resets_at": "2026-10-29T05:29:59.815307+00:00"},
            {"kind": "weekly_all", "percent": 45, "resets_at": "2026-11-02T15:59:59.815326+00:00"},
            {"kind": "weekly_scoped", "percent": 0, "resets_at": "2026-11-02T16:00:00+00:00"},
        ]}}}
        fake = self.root / "fake_usage.py"
        fake.write_text("import json\nprint('not json')\nprint(json.dumps(%r))\n" % report, encoding="utf-8")
        adapter = ClaudeAdapter(self.root / "missing", limits=self.root / "none.json", poll_usage=True,
                                usage_command=[sys.executable, str(fake)])
        adapter.scan(self.store)  # starts the background run
        adapter._thread.join(10)
        adapter._polled = float("-inf")
        adapter.scan(self.store)  # stores its result
        adapter._thread.join(10)
        rows = {r["allowance_type"]: r for r in self.store.latest_allowances() if r["provider"] == "claude"}
        self.assertEqual(rows["five_hour"]["remaining_percent"], 84)
        self.assertEqual(rows["five_hour"]["resets_at"], "2026-10-29T05:30:00+00:00")
        self.assertEqual(rows["seven_day"]["remaining_percent"], 55)
        self.assertEqual(adapter.note, "")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM allowance_snapshots").fetchone()[0], 2)  # scoped limit ignored, rerun adds no rows

    def test_claude_usage_missing_binary_sets_note(self):
        adapter = ClaudeAdapter(self.root / "missing", limits=self.root / "none.json", poll_usage=True,
                                usage_command=[str(self.root / "no-such-claude.exe")])
        adapter.scan(self.store)
        adapter._thread.join(10)
        self.assertIn("gagal", adapter.note)

    def test_claude_statusline_limits(self):
        limits = self.root / "claude-statusline.json"
        limits.write_text(json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1793145600},
            "seven_day": {"used_percentage": 41, "resets_at": 1793577600},
        }}), encoding="utf-8")
        adapter = ClaudeAdapter(self.root / "missing", limits=limits)
        adapter.scan(self.store)
        adapter.scan(self.store)
        rows = {r["allowance_type"]: r for r in self.store.latest_allowances() if r["provider"] == "claude"}
        self.assertEqual(rows["five_hour"]["remaining_percent"], 76.5)
        self.assertEqual(rows["seven_day"]["remaining_percent"], 59)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM allowance_snapshots").fetchone()[0], 2)
        limits.write_text("{broken", encoding="utf-8")
        adapter.scan(self.store)  # a half-written file must not raise

    def test_connect_claude_only_touches_free_status_line(self):
        old = os.environ.get("TOKENMON_HOME")
        os.environ["TOKENMON_HOME"] = str(self.root / "home")
        try:
            settings = self.root / "settings.json"
            settings.write_text(json.dumps({"model": "x"}), encoding="utf-8")
            self.assertIn("terhubung", connect_claude(settings))
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "x")
            self.assertIn("claude-statusline.ps1", data["statusLine"]["command"])
            self.assertTrue((self.root / "settings.json.tokenmon-backup").exists())
            self.assertTrue((self.root / "home" / "claude-statusline.ps1").exists())
            self.assertIn("sudah terhubung", connect_claude(settings))

            good = data["statusLine"]["command"]
            data["statusLine"]["command"] = good.replace("home/", "elsewhere/")
            settings.write_text(json.dumps(data), encoding="utf-8")
            self.assertNotIn("sudah", connect_claude(settings))  # ours but misplaced: repaired
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"]["command"], good)
            self.assertEqual(json.loads((self.root / "settings.json.tokenmon-backup").read_text(encoding="utf-8")),
                             {"model": "x"})  # first backup is kept

            other = self.root / "other.json"
            other.write_text('{"statusLine": {"type": "command", "command": "mine"}}', encoding="utf-8")
            before = other.read_bytes()
            self.assertIn("Tidak diubah", connect_claude(other))
            broken = self.root / "broken.json"
            broken.write_text("{nope", encoding="utf-8")
            self.assertIn("Tidak diubah", connect_claude(broken))
            self.assertEqual((other.read_bytes(), broken.read_text(encoding="utf-8")), (before, "{nope"))
        finally:
            if old is None:
                os.environ.pop("TOKENMON_HOME", None)
            else:
                os.environ["TOKENMON_HOME"] = old

    def test_sync_is_idempotent_and_limited_to_three_devices(self):
        payload = self.store.export_payload()
        self.assertEqual(payload["export_device_id"], self.store.device_id)
        self.assertEqual(self.store.import_payload(payload), 0)
        payload["devices"] = [
            {"id": f"d{i}", "display_name": f"D{i}", "platform": "x", "created_at": "x",
             "last_seen_at": "x", "active": 1} for i in range(3)
        ]
        with self.assertRaises(ValueError):
            self.store.import_payload(payload)

    def test_sync_imports_peer_updates_after_peer_knows_local_device(self):
        peer = Store(self.root / "peer.db")
        sync_dir = self.root / "sync"
        old = os.environ.get("TOKENMON_SYNC_DIR")
        os.environ["TOKENMON_SYNC_DIR"] = str(sync_dir)
        try:
            self.assertEqual(sync(self.store), 0)
            self.assertEqual(sync(peer), 0)
            event = UsageEvent(
                "peer-event", "claude", peer.device_id, "session", "repo", "hash", "cli",
                "model", "2026-10-28T00:00:00+00:00", 10, 2, 3, 1, "test",
            )
            peer.add_events([event])
            peer.commit()
            self.assertEqual(sync(peer), 0)
            self.assertEqual(sync(self.store), 0)
            found = self.store.db.execute(
                "SELECT COUNT(*) FROM usage_events WHERE id='peer-event'"
            ).fetchone()[0]
            self.assertEqual(found, 1)
        finally:
            peer.close()
            if old is None:
                os.environ.pop("TOKENMON_SYNC_DIR", None)
            else:
                os.environ["TOKENMON_SYNC_DIR"] = old


if __name__ == "__main__":
    unittest.main()
