from pathlib import Path

from gateway.orchestrator_modes import (
    MODE_CLARA_LEAD,
    MODE_HUGO_LEAD,
    handle_mode_text,
    mode_system_note,
    parse_mode_request,
    read_mode,
    write_mode,
)


def test_natural_korean_mode_commands(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_LEAD_MODE", raising=False)

    reply = handle_mode_text("2번 모드", hermes_home=tmp_path, source="test")
    assert reply is not None
    assert "clara-lead" in reply
    assert read_mode(tmp_path)["mode"] == MODE_CLARA_LEAD

    reply = handle_mode_text("현재 모드", hermes_home=tmp_path, source="test")
    assert reply is not None
    assert "clara-lead" in reply

    reply = handle_mode_text("기본 모드로", hermes_home=tmp_path, source="test")
    assert reply is not None
    assert "hugo-lead" in reply
    assert read_mode(tmp_path)["mode"] == MODE_HUGO_LEAD


def test_parse_is_conservative_for_normal_sentences():
    assert parse_mode_request("2번 모드가 좋은 것 같아") is None
    assert parse_mode_request("클라라가 코딩하고 휴고가 리뷰하자") is None


def test_mode_system_note_after_programmatic_mode_write(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_LEAD_MODE", raising=False)

    write_mode("hugo-lead", hermes_home=tmp_path, source="test")
    assert "1번 hugo-lead" in mode_system_note(tmp_path)
    write_mode("clara-lead", hermes_home=tmp_path, source="test")
    assert "2번 clara-lead" in mode_system_note(tmp_path)


def test_env_pin_overrides_global_mode_for_reads(tmp_path: Path, monkeypatch):
    write_mode("clara-lead", hermes_home=tmp_path, source="test")
    monkeypatch.setenv("HERMES_LEAD_MODE", "hugo-lead")
    data = read_mode(tmp_path)
    assert data["mode"] == MODE_HUGO_LEAD
    assert data.get("pinned") is True
    assert "hugo-lead" in mode_system_note(tmp_path)


def test_env_pin_blocks_mode_switch_and_keeps_global_file(tmp_path: Path, monkeypatch):
    write_mode("clara-lead", hermes_home=tmp_path, source="test")
    monkeypatch.setenv("HERMES_LEAD_MODE", "hugo-lead")
    reply = handle_mode_text("1번", hermes_home=tmp_path, source="test")
    assert reply is not None and "고정" in reply
    monkeypatch.delenv("HERMES_LEAD_MODE")
    assert read_mode(tmp_path)["mode"] == MODE_CLARA_LEAD


def test_env_pin_invalid_value_is_ignored(tmp_path: Path, monkeypatch):
    write_mode("clara-lead", hermes_home=tmp_path, source="test")
    monkeypatch.setenv("HERMES_LEAD_MODE", "banana")
    assert read_mode(tmp_path)["mode"] == MODE_CLARA_LEAD
