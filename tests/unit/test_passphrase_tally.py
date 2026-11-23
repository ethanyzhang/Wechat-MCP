"""PassphraseTally reports how many databases each distinct passphrase opens,
without ever exposing a passphrase or a key."""
import json

from tests.support.encrypted import create_database
from wechat_bridge.setup.lldb_capture import PassphraseTally

SHARED = b"shared passphrase for every database"


def rows_for(tmp_path, passphrases):
    rows = []
    for i, passphrase in enumerate(passphrases):
        path = tmp_path / f"db{i}.db"
        create_database(path, wal=False, passphrase=passphrase).close()
        rows.append((path.name, path.read_bytes()[:4096]))
    return rows


def test_shared_passphrase_is_one_entry_opening_every_database(tmp_path):
    rows = rows_for(tmp_path, [SHARED] * 3)
    assert len({page[:16] for _, page in rows}) == 3
    tally = PassphraseTally()
    for _ in range(3):
        tally.observe(256000, SHARED)
    report = tally.report(rows)
    assert len(report) == 1
    assert report[0]["calls"] == 3 and report[0]["databases_verified"] == 3


def test_per_database_passphrases_are_separate_entries_opening_one_each(tmp_path):
    passphrases = [b"first passphrase", b"second passphrase", b"third passphrase"]
    report = report_for(rows_for(tmp_path, passphrases), passphrases)
    assert len(report) == 3
    assert all(e["databases_verified"] == 1 and e["calls"] == 1 for e in report)
    assert len({e["fp"] for e in report}) == 3


def report_for(rows, passphrases):
    tally = PassphraseTally()
    for passphrase in passphrases:
        tally.observe(256000, passphrase)
    return tally.report(rows)


def test_output_holds_no_secret_and_fingerprints_differ_between_runs(tmp_path):
    rows = rows_for(tmp_path, [SHARED])
    first, second = report_for(rows, [SHARED]), report_for(rows, [SHARED])
    text = json.dumps(first)
    assert SHARED.decode() not in text and SHARED.hex() not in text
    assert set(first[0]) == {"fp", "calls", "databases_verified"}
    assert len(first[0]["fp"]) == 16
    assert first[0]["fp"] != second[0]["fp"]


def test_unrelated_secret_opens_nothing(tmp_path):
    report = report_for(rows_for(tmp_path, [SHARED]), [b"not the passphrase"])
    assert report[0]["databases_verified"] == 0


def test_budget_marks_truncated_entries(tmp_path):
    rows = rows_for(tmp_path, [SHARED])
    tally = PassphraseTally()
    tally.observe(256000, SHARED)
    report = tally.report(rows, budget=-1)
    assert report[0]["truncated"] is True and report[0]["databases_verified"] == 0


def test_two_round_calls_are_counted_but_never_fingerprinted():
    tally = PassphraseTally()
    tally.observe(2, b"a derived page key, not a passphrase")
    tally.observe(256000, SHARED)
    tally.observe(256000, SHARED)
    assert tally.rounds == {"matched_salt_calls_256000": 2, "matched_salt_calls_2": 1}
    assert [e["calls"] for e in tally.report([])] == [2]
