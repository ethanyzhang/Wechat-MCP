"""Loaded only by test subprocesses (via PYTHONPATH): pins the 'installed' WeChat build to a fixture plist.

A `serve` subprocess cannot be monkeypatched, and live queries read the installed build; without this the tests would
depend on the WeChat app of the machine running them.
"""
import os
from pathlib import Path

if os.environ.get("WECHAT_BRIDGE_TEST_PLIST"):
    from wechat_bridge.adapters import wechat_4_1
    wechat_4_1.INFO_PLIST = Path(os.environ["WECHAT_BRIDGE_TEST_PLIST"])
