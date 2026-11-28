import os
import pytest

LLDB_TESTS_ENV = "WECHAT_BRIDGE_LLDB_TESTS"
LLDB_SKIP_REASON = (f"LLDB test: attaching a debugger needs macOS debugger authorization "
                    f"(a password dialog when Developer mode is off); set {LLDB_TESTS_ENV}=1 "
                    "at the screen to run it")


def lldb_tests_enabled(environ=os.environ):
    return environ.get(LLDB_TESTS_ENV) == "1"


def pytest_collection_modifyitems(config, items):
    # A default run must never start LLDB: it can open an authorization
    # dialog on the owner's screen and then hang until the test timeout.
    if lldb_tests_enabled():
        return
    skip = pytest.mark.skip(reason=LLDB_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("lldb"):
            item.add_marker(skip)

@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "accounts" / "synthetic-account"
    for i, relative in enumerate(("contact/contact.db", "session/session.db",
            "message/message_resource.db", "hardlink/hardlink.db",
            "message/message_0.db", "message/message_1.db")):
        path = root / "db_storage" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([i + 1]) * 16 + b"fixture")
    (root / "msg" / "file").mkdir(parents=True)
    return root
