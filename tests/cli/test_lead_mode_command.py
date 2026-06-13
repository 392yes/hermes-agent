from pathlib import Path


def test_cli_rejects_removed_lead_mode_slash_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {}

    assert cli.process_command("/clara-lead") is True

    output = capsys.readouterr().out
    assert "Unknown command" in output
    assert "hermes-clara" in output or "Did you mean" not in output


def test_cli_rejects_legacy_orchestrator_mode_command(capsys):
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {}

    assert cli.process_command("/orchestrator-mode 2") is True

    output = capsys.readouterr().out
    assert "Unknown command" in output


def test_cli_natural_current_mode_is_handled_before_llm(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_LEAD_MODE", raising=False)

    from cli import HermesCLI
    from gateway.orchestrator_modes import write_mode

    cli = HermesCLI.__new__(HermesCLI)
    write_mode("clara-lead", hermes_home=tmp_path, source="test")

    assert cli._handle_natural_lead_mode_text("현재모드") is True
    output = capsys.readouterr().out
    assert "clara-lead" in output
