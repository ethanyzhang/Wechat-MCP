"""pytest plugin for tests/unit/test_lldb_opt_in.py: after collection, print
one JSON line per LLDB-marked item with whether it will be skipped."""
import json


def pytest_collection_finish(session):
    for item in session.items:
        if item.get_closest_marker("lldb"):
            print("LLDB-ITEM " + json.dumps({
                "nodeid": item.nodeid,
                "skipped": item.get_closest_marker("skip") is not None,
            }))
