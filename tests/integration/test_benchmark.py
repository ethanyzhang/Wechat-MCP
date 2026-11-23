import json

import pytest

from tests.support.live import repo_case, sync_text_index
from wechat_bridge.adapters.wechat_4_1 import open_adapter
from wechat_bridge.core.errors import BridgeError


def test_benchmark_reports_aggregate_warm_distribution_and_scope(tmp_path):
    from benchmarks.search import benchmark
    cfg, _ = repo_case(tmp_path)
    sync_text_index(cfg)
    plain = lambda c: open_adapter(c, build='270102')   # no index maintenance inside the timed queries
    result = benchmark(cfg, samples=200, corpus_scope='synthetic_fixture', adapter_factory=plain)
    assert result['message_count'] == 3
    assert result['sample_count'] == 200
    assert result['warm_query_ms']['p95'] >= result['warm_query_ms']['p50'] >= 0
    assert result['source'] == 'live'
    assert result['corpus_scope'] == 'synthetic_fixture'
    assert not result['representative_large_archive_claim']
    assert 'Known message' not in json.dumps(result)
    assert 'fictional' not in json.dumps(result)
    with pytest.raises(BridgeError, match='invalid_benchmark_samples'):
        benchmark(cfg, samples=199, adapter_factory=plain)
