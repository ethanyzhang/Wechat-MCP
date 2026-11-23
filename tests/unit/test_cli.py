import json


def test_missing_manifest_returns_setup_required(tmp_path, source_root, capsys):
    from wechat_bridge.core.config import create_config
    from wechat_bridge.cli import main
    cfg = create_config(tmp_path / "state", source_root)
    code = main(["--state-dir", str(cfg.state_dir), "verify-keys"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "setup_required"


def test_inspect_schema_prints_aggregates_only(tmp_path, capsys):
    from tests.support.schemas import account_case
    from wechat_bridge.cli import main
    cfg = account_case(tmp_path)
    assert main(["--state-dir", str(cfg.state_dir), "inspect-schema"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["schema_count"] == 6
    assert "fictional" not in output
    assert "Msg_" not in output
    assert str(cfg.account_root) not in output


def test_archive_commands_are_gone(tmp_path, capsys):
    import pytest
    from wechat_bridge.cli import main
    for command in (['archive-refresh'], ['archive-status'], ['search-index'], ['archive-watch'], ['serve', '--no-watch']):
        with pytest.raises(SystemExit) as info:
            main(['--state-dir', str(tmp_path), *command])
        assert info.value.code == 2
    capsys.readouterr()


def test_search_cli_reads_live_and_validates_its_limit(tmp_path, capsys, monkeypatch):
    from tests.support.live import import_case, sync_text_index
    from tests.support.proof import installed_build
    from wechat_bridge.cli import main
    cfg = import_case(tmp_path); args = ['--state-dir', str(cfg.state_dir)]
    installed_build(monkeypatch, tmp_path)
    sync_text_index(cfg)
    assert main(args + ['search', '发票', '--limit', '1']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['items'][0]['excerpt'] == '发票 invoice' and result['source'] == 'live'
    assert main(args + ['search', '发票', '--limit', '201']) == 2
    assert json.loads(capsys.readouterr().out) == {'error': 'invalid_limit'}
    assert not (cfg.state_dir / 'archive.sqlite3').exists()


def test_server_startup_failure_never_prints_nonprotocol_stdout(tmp_path, capsys):
    from wechat_bridge.cli import main
    assert main(['--state-dir', str(tmp_path.resolve() / 'missing'), 'serve']) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert json.loads(output.err) == {'error': 'invalid_private_state'}


def test_unexpected_exception_prints_redacted_internal_error(tmp_path, capsys, monkeypatch):
    from wechat_bridge.cli import main
    private = str(tmp_path / "private-account-folder")

    def fail(*args, **kwargs):
        raise PermissionError(13, "denied", private)
    monkeypatch.setattr("wechat_bridge.cli.discover", fail)
    assert main(["--state-dir", str(tmp_path / "state"), "discover"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "internal_error"}
    assert private not in captured.out + captured.err
