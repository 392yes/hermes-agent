"""Unit tests for the shared active ledger (agent/team_active_ledger.py)."""

import json
import threading

import pytest

from agent import team_active_ledger as ledger


def _read_raw_lines(hermes_home):
    path = hermes_home / "runtime" / "active_ledger.jsonl"
    return path.read_text(encoding="utf-8").splitlines()


class TestWriteRead:
    def test_write_then_read_peer_roundtrip(self, tmp_path):
        assert ledger.write_turn(
            runtime="codex",
            summary="implemented step A",
            session_id="s1",
            turn_id="t1",
            hermes_home=tmp_path,
        )
        peers = ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path)
        assert len(peers) == 1
        assert peers[0].runtime == "codex"
        assert peers[0].summary == "implemented step A"
        assert peers[0].session_id == "s1"

    def test_read_excludes_own_runtime(self, tmp_path):
        ledger.write_turn(runtime="claude", summary="my own work", hermes_home=tmp_path)
        # A claude reader must not be fed claude's own entries.
        assert ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path) == []
        # But a codex reader sees it.
        peers = ledger.read_peer_recent(self_runtime="codex", hermes_home=tmp_path)
        assert len(peers) == 1
        assert peers[0].runtime == "claude"

    def test_read_returns_most_recent_first_and_respects_limit(self, tmp_path):
        for i in range(5):
            ledger.write_turn(
                runtime="codex", summary=f"turn {i}", hermes_home=tmp_path
            )
        peers = ledger.read_peer_recent(
            self_runtime="claude", limit=2, hermes_home=tmp_path
        )
        assert [p.summary for p in peers] == ["turn 4", "turn 3"]

    def test_read_mixes_only_peer_entries(self, tmp_path):
        ledger.write_turn(runtime="codex", summary="codex-1", hermes_home=tmp_path)
        ledger.write_turn(runtime="claude", summary="claude-1", hermes_home=tmp_path)
        ledger.write_turn(runtime="codex", summary="codex-2", hermes_home=tmp_path)
        peers = ledger.read_peer_recent(
            self_runtime="claude", limit=5, hermes_home=tmp_path
        )
        assert [p.summary for p in peers] == ["codex-2", "codex-1"]


class TestRobustness:
    def test_missing_ledger_returns_empty(self, tmp_path):
        assert ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path) == []

    def test_empty_runtime_or_summary_is_noop(self, tmp_path):
        assert ledger.write_turn(runtime="", summary="x", hermes_home=tmp_path) is False
        assert ledger.write_turn(runtime="codex", summary="  ", hermes_home=tmp_path) is False
        assert ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path) == []

    def test_partial_trailing_line_is_skipped(self, tmp_path):
        ledger.write_turn(runtime="codex", summary="good entry", hermes_home=tmp_path)
        path = tmp_path / "runtime" / "active_ledger.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"runtime": "codex", "summary": "tru')  # truncated, no newline
        peers = ledger.read_peer_recent(
            self_runtime="claude", limit=5, hermes_home=tmp_path
        )
        assert [p.summary for p in peers] == ["good entry"]

    def test_summary_is_clamped(self, tmp_path):
        ledger.write_turn(
            runtime="codex", summary="x" * 5000, hermes_home=tmp_path
        )
        peers = ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path)
        assert len(peers[0].summary) <= ledger._MAX_SUMMARY_CHARS

    def test_summary_whitespace_is_normalized(self, tmp_path):
        ledger.write_turn(
            runtime="codex", summary="line1\n\n  line2\t", hermes_home=tmp_path
        )
        peers = ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path)
        assert peers[0].summary == "line1 line2"


class TestConcurrency:
    def test_concurrent_appends_do_not_lose_or_corrupt_rows(self, tmp_path):
        n_threads = 8
        per_thread = 25

        def worker(idx):
            for j in range(per_thread):
                ledger.write_turn(
                    runtime=f"rt{idx}",
                    summary=f"{idx}-{j}",
                    hermes_home=tmp_path,
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = _read_raw_lines(tmp_path)
        assert len(lines) == n_threads * per_thread
        # Every line must be valid JSON (no interleaved/torn writes).
        for raw in lines:
            obj = json.loads(raw)
            assert obj["runtime"].startswith("rt")
            assert "-" in obj["summary"]


class TestTrim:
    def test_trim_keeps_recent_when_over_byte_ceiling(self, tmp_path, monkeypatch):
        # Shrink ceilings so the test stays fast.
        monkeypatch.setattr(ledger, "_MAX_FILE_BYTES", 500)
        monkeypatch.setattr(ledger, "_TRIM_TO_LINES", 10)
        for i in range(100):
            ledger.write_turn(
                runtime="codex", summary=f"entry-{i:03d}", hermes_home=tmp_path
            )
        lines = _read_raw_lines(tmp_path)
        assert len(lines) <= 11  # trimmed to ~_TRIM_TO_LINES
        # The most recent entry must survive.
        peers = ledger.read_peer_recent(self_runtime="claude", hermes_home=tmp_path)
        assert peers[0].summary == "entry-099"
