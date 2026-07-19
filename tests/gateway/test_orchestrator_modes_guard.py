"""Guard tests for gateway.orchestrator_modes.write_mode auto-source blocking.

An automated / non-interactive caller (a ``source`` prefixed with one of
``AUTO_SOURCE_PREFIXES``) must never be able to *flip* the global orchestrator
lead mode; only a human / explicit source may switch it.  These tests pin that
behaviour by asserting against the on-disk mode file, not merely the return
value — so they fail loudly if the guard is ever removed.
"""

import json

import pytest

from gateway.orchestrator_modes import (
    MODE_CLARA_LEAD,
    MODE_HUGO_LEAD,
    mode_path,
    read_mode,
    write_mode,
)


@pytest.fixture(autouse=True)
def _no_env_pin(monkeypatch):
    # read_mode short-circuits to HERMES_LEAD_MODE when set, bypassing the
    # file; clear it so these tests exercise the persisted-file path.
    monkeypatch.delenv("HERMES_LEAD_MODE", raising=False)


def _seed(home, mode):
    """Seed an orchestrator-mode.json so read_mode picks it up."""
    path = mode_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"mode": mode, "updated_at": "2026-01-01T00:00:00+0000", "source": "seed"}
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _disk_mode(home):
    """Return the mode value actually persisted on disk (not via read_mode)."""
    return json.loads(mode_path(home).read_text(encoding="utf-8"))["mode"]


def test_auto_source_cannot_switch_lead(tmp_path):
    # (a) auto:sync must NOT flip hugo-lead -> clara-lead.
    _seed(tmp_path, MODE_HUGO_LEAD)
    result = write_mode(MODE_CLARA_LEAD, source="auto:sync", hermes_home=tmp_path)

    assert result["blocked"] is True
    assert result["mode"] == MODE_HUGO_LEAD
    assert "cannot switch lead mode" in result["reason"]
    # The file on disk must STILL be hugo-lead — the guard touched nothing.
    assert _disk_mode(tmp_path) == MODE_HUGO_LEAD
    assert read_mode(tmp_path)["mode"] == MODE_HUGO_LEAD


def test_human_source_switches_lead(tmp_path):
    # (b) an explicit human source (not an auto prefix) succeeds.
    _seed(tmp_path, MODE_HUGO_LEAD)
    result = write_mode(MODE_CLARA_LEAD, source="slack:manual", hermes_home=tmp_path)

    assert not result.get("blocked")
    assert result["mode"] == MODE_CLARA_LEAD
    assert _disk_mode(tmp_path) == MODE_CLARA_LEAD


def test_auto_source_same_mode_is_noop(tmp_path):
    # (c) cron:heartbeat requesting the mode already active: no error, no write.
    path = _seed(tmp_path, MODE_HUGO_LEAD)
    before = path.read_text(encoding="utf-8")

    result = write_mode(MODE_HUGO_LEAD, source="cron:heartbeat", hermes_home=tmp_path)

    assert not result.get("blocked")
    assert result.get("noop") is True
    assert result["mode"] == MODE_HUGO_LEAD
    # File is byte-for-byte unchanged (no unnecessary rewrite).
    assert path.read_text(encoding="utf-8") == before


def test_default_gateway_source_still_writes(tmp_path):
    # (d) regression: the default source="gateway" (human mode command) writes.
    _seed(tmp_path, MODE_HUGO_LEAD)
    result = write_mode(MODE_CLARA_LEAD, hermes_home=tmp_path)

    assert result["mode"] == MODE_CLARA_LEAD
    assert "blocked" not in result  # success path keeps its original dict shape
    assert _disk_mode(tmp_path) == MODE_CLARA_LEAD
