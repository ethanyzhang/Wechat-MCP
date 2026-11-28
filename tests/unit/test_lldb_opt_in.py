"""A default test run must never start LLDB (issue #7): LLDB tests are opt-in."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conftest import LLDB_TESTS_ENV
from tests.integration import test_capture_lifecycle as lifecycle

ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = "tests/integration/test_capture_lifecycle.py"


def collect_lldb_items(env_value):
    # --collect-only never runs a test, so this check cannot start LLDB.
    env = {k: v for k, v in os.environ.items() if k != LLDB_TESTS_ENV}
    if env_value is not None:
        env[LLDB_TESTS_ENV] = env_value
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-p", "tests.support.lldb_report", "tests"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return [json.loads(line.removeprefix("LLDB-ITEM "))
            for line in result.stdout.splitlines() if line.startswith("LLDB-ITEM ")]


@pytest.mark.parametrize("env_value", [None, "", "0", "true"])
def test_default_run_skips_every_lldb_test(env_value):
    items = collect_lldb_items(env_value)
    assert sum(i["nodeid"].startswith(LIFECYCLE) for i in items) == 3
    assert all(i["skipped"] for i in items), items


def test_opt_in_selects_lldb_tests_without_skip():
    items = collect_lldb_items("1")
    assert sum(i["nodeid"].startswith(LIFECYCLE) for i in items) == 3
    assert not any(i["skipped"] for i in items), items


def test_every_test_file_that_starts_lldb_is_marked():
    for path in (ROOT / "tests").rglob("*.py"):
        text = path.read_text()
        if "/usr/bin/lldb" in text:
            assert "pytestmark = pytest.mark.lldb" in text, path


@pytest.mark.parametrize("env_value", [None, "true"])
def test_run_probe_skips_before_any_subprocess(tmp_path, monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv(LLDB_TESTS_ENV, raising=False)
    else:
        monkeypatch.setenv(LLDB_TESTS_ENV, env_value)

    def refuse(*args, **kwargs):
        raise AssertionError(f"subprocess started: {args}")
    monkeypatch.setattr(lifecycle.subprocess, "run", refuse)
    monkeypatch.setattr(lifecycle.subprocess, "check_output", refuse)
    with pytest.raises(pytest.skip.Exception):
        lifecycle.run_probe(tmp_path, "return 0;")
