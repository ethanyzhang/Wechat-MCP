"""A master secret derives per-salt keys in the CLI only; it never leaves private state."""
import json
import stat

import pytest

from tests.support.encrypted import create_database
from wechat_bridge.core.config import create_config
from wechat_bridge.core.errors import BridgeError
from wechat_bridge.source.discovery import discover
from wechat_bridge.source import keys as k

PASSPHRASE = b"the one passphrase WeChat uses"
PATHS = ("contact/contact.db", "session/session.db", "message/message_resource.db",
         "hardlink/hardlink.db", "message/message_0.db")


def account(tmp_path, passphrase=PASSPHRASE):
    root = tmp_path / "account"
    for relative in PATHS:
        create_database(root / "db_storage" / relative, wal=False, passphrase=passphrase).close()
    return create_config(tmp_path / "state", root)


def rebuild(cfg, relative, passphrase=PASSPHRASE):
    target = cfg.account_root / "db_storage" / relative
    new = target.with_suffix(".new")
    create_database(new, wal=False, passphrase=passphrase).close()
    new.replace(target)


def published_keys(cfg):
    """What the earlier capture left in keys.json: one derived key per database."""
    inventory = discover(cfg)
    manifest = k.KeyManifest(tuple(k.derive_entry(db, PASSPHRASE) for db in inventory.databases))
    k.publish_verified_manifest(manifest, k.verify_keys(inventory, manifest, cfg), cfg)
    return k.load_manifest(cfg.state_dir / "keys.json")


def test_rebuilt_database_with_new_salt_is_opened_from_the_master_secret(tmp_path):
    cfg = account(tmp_path)
    manifest = published_keys(cfg)
    rebuild(cfg, "contact/contact.db")
    inventory = discover(cfg)
    stale = k.verify_keys(inventory, manifest, cfg)
    assert not stale.ready and "replaced" in stale.statuses()
    account_id = inventory.databases[0].account_id
    manifest, coverage = k.verify_with_master_secrets(inventory, manifest, stale, {account_id: PASSPHRASE}, cfg)
    assert coverage.ready and set(coverage.statuses()) == {"verified"}
    k.publish_verified_manifest(manifest, coverage, cfg)
    assert k.verify_keys(discover(cfg), k.load_manifest(cfg.state_dir / "keys.json"), cfg).ready


def test_new_shard_is_opened_but_a_rotated_passphrase_is_not(tmp_path):
    cfg = account(tmp_path)
    manifest = published_keys(cfg)
    create_database(cfg.account_root / "db_storage/message/message_1.db", wal=False, passphrase=PASSPHRASE).close()
    rebuild(cfg, "session/session.db", passphrase=b"a different passphrase")
    inventory = discover(cfg)
    account_id = inventory.databases[0].account_id
    stale = k.verify_keys(inventory, manifest, cfg)
    manifest, coverage = k.verify_with_master_secrets(inventory, manifest, stale, {account_id: PASSPHRASE}, cfg)
    by_path = {db.relative_path: item.status for db, item in zip(inventory.databases, coverage.databases)}
    assert by_path["message/message_1.db"] == "verified"
    assert by_path["session/session.db"] == "key_or_format_mismatch"
    assert not coverage.ready
    k.publish_verified_manifest(manifest, coverage, cfg)
    saved = {e.relative_path for e in k.load_manifest(cfg.state_dir / "keys.json").entries}
    assert "message/message_1.db" in saved


def test_another_accounts_secret_derives_nothing(tmp_path):
    cfg = account(tmp_path)
    inventory = discover(cfg)
    coverage = k.verify_keys(inventory, k.KeyManifest(()), cfg)
    manifest, after = k.verify_with_master_secrets(inventory, k.KeyManifest(()), coverage, {"other": PASSPHRASE}, cfg)
    assert after is coverage and manifest.entries == ()


def test_secret_file_is_private_and_round_trips(tmp_path):
    cfg = account(tmp_path)
    k.save_master_secret(cfg, "account-a", PASSPHRASE)
    k.save_master_secret(cfg, "account-b", b"another")
    path = cfg.state_dir / k.MASTER_SECRETS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert k.load_master_secrets(cfg) == {"account-a": PASSPHRASE, "account-b": b"another"}
    assert k.load_master_secrets(account(tmp_path / "empty")) == {}


@pytest.mark.parametrize("content", [{"version": 2, "secrets": {}}, {"version": 1, "secrets": []},
                                     {"version": 1, "secrets": {"a": "zz"}}, {"version": 1, "secrets": {"a": ""}}])
def test_invalid_secret_file_is_rejected(tmp_path, content):
    from wechat_bridge.core.config import write_private_json
    cfg = account(tmp_path)
    write_private_json(cfg.state_dir / k.MASTER_SECRETS_FILE, content)
    with pytest.raises(BridgeError, match="invalid_master_secrets"):
        k.load_master_secrets(cfg)


def test_confirmation_needs_two_databases(tmp_path):
    cfg = account(tmp_path)
    inventory = discover(cfg)
    account_id = inventory.databases[0].account_id
    assert k.confirm_master_secret(inventory, [b"wrong", PASSPHRASE], cfg) == (account_id, PASSPHRASE)
    assert k.confirm_master_secret(inventory, [b"wrong"], cfg) is None
    solo = account(tmp_path / "solo")
    for i, relative in enumerate(PATHS[1:]):  # only one database keeps the shared passphrase
        rebuild(solo, relative, passphrase=b"own passphrase %d" % i)
    assert k.confirm_master_secret(discover(solo), [PASSPHRASE], solo) is None


def test_cli_verify_keys_reopens_a_rebuilt_database(tmp_path, capsys):
    from wechat_bridge.cli import main
    cfg = account(tmp_path)
    published_keys(cfg)
    rebuild(cfg, "message/message_0.db")
    args = ["--state-dir", str(cfg.state_dir), "verify-keys"]
    assert main(args) == 2
    capsys.readouterr()
    k.save_master_secret(cfg, discover(cfg).databases[0].account_id, PASSPHRASE)
    assert main(args) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["ready"] is True
    assert PASSPHRASE.decode() not in output and PASSPHRASE.hex() not in output


def setup_case(tmp_path, secrets):
    """A prepared setup whose fake capture returns derived keys plus the given master-secret candidates."""
    from tests.support.setup import FakeBackend
    from wechat_bridge.setup.prepare import prepare_setup
    cfg = account(tmp_path)
    inventory = discover(cfg)
    backend = FakeBackend(tmp_path)
    backend.candidates = {db.relative_path: {"enc_key": k.derive_entry(db, PASSPHRASE).raw_key.hex()}
                          for db in inventory.databases}
    backend.master_secrets = [s.hex() for s in secrets]
    backend.running = False
    return prepare_setup(cfg, inventory, backend=backend), backend


def test_capture_stores_a_confirmed_secret_and_never_leaves_it_in_captured_json(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    assert run_capture(plan, backend=backend).status == "verified"
    stored = k.load_master_secrets(plan.config)
    assert list(stored.values()) == [PASSPHRASE]
    assert not (plan.directory / "captured.json").exists()   # removed after a verified capture
    for path in plan.directory.rglob("*"):
        if path.is_file():
            assert PASSPHRASE.hex().encode() not in path.read_bytes()


def test_partial_capture_strips_the_secret_from_captured_json(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    backend.candidates.popitem()   # one database has no key: coverage stays partial
    assert run_capture(plan, backend=backend).status == "partial_coverage"
    assert (plan.directory / "captured.json").exists()
    assert PASSPHRASE.hex().encode() not in (plan.directory / "captured.json").read_bytes()
    assert list(k.load_master_secrets(plan.config).values()) == [PASSPHRASE]


def test_unconfirmed_candidate_is_dropped(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [b"not a real passphrase"])
    assert run_capture(plan, backend=backend).status == "verified"
    assert k.load_master_secrets(plan.config) == {}
    assert not (plan.config.state_dir / k.MASTER_SECRETS_FILE).exists()


def test_malformed_candidates_fail_capture_without_storing(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [])
    backend.master_secrets = ["zz"]
    assert run_capture(plan, backend=backend).status == "invalid_capture_output"
    assert k.load_master_secrets(plan.config) == {}


def test_secret_is_scrubbed_when_a_later_step_fails(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    backend.master_secrets = [PASSPHRASE.hex(), "zz"]      # one real secret beside a malformed row
    assert run_capture(plan, backend=backend).status == "invalid_capture_output"
    assert PASSPHRASE.hex().encode() not in (plan.directory / "captured.json").read_bytes()
    assert k.load_master_secrets(plan.config) == {}


def test_secret_is_scrubbed_when_the_capture_step_itself_fails_after_writing(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    from wechat_bridge.core.errors import BridgeError
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    write = backend.capture
    def write_then_time_out(p):
        write(p)
        raise BridgeError("capture_timeout")
    backend.capture = write_then_time_out
    assert run_capture(plan, backend=backend).status == "capture_timeout"
    assert PASSPHRASE.hex().encode() not in (plan.directory / "captured.json").read_bytes()


def test_secret_is_scrubbed_when_the_source_changed_mid_capture(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    write = backend.capture
    def write_then_rebuild(p):
        write(p)
        rebuild(p.config, "contact/contact.db")
    backend.capture = write_then_rebuild
    assert run_capture(plan, backend=backend).status == "source_changed"
    assert PASSPHRASE.hex().encode() not in (plan.directory / "captured.json").read_bytes()
    assert k.load_master_secrets(plan.config) == {}


def test_rotated_id_with_the_same_salt_is_not_overridden_by_derivation(tmp_path):
    from dataclasses import replace
    cfg = account(tmp_path)
    manifest = published_keys(cfg)
    inventory = discover(cfg)
    target = manifest.entries[0]
    manifest = k.KeyManifest(tuple(replace(e, database_id="old-incarnation") if e is target else e
                                   for e in manifest.entries))
    stale = k.verify_keys(inventory, manifest, cfg)
    assert "replaced" in stale.statuses()
    account_id = inventory.databases[0].account_id
    after, coverage = k.verify_with_master_secrets(inventory, manifest, stale, {account_id: PASSPHRASE}, cfg)
    assert after is manifest and coverage is stale
    assert not coverage.ready


def test_a_killed_helpers_temp_file_is_removed_on_a_failed_capture(tmp_path):
    from wechat_bridge.setup.capture import run_capture
    from wechat_bridge.core.errors import BridgeError
    plan, backend = setup_case(tmp_path, [PASSPHRASE])
    def die_mid_write(p):
        (p.directory / "captured.new").write_text(PASSPHRASE.hex())
        raise BridgeError("capture_timeout")
    backend.capture = die_mid_write
    assert run_capture(plan, backend=backend).status == "capture_timeout"
    assert not (plan.directory / "captured.new").exists()
