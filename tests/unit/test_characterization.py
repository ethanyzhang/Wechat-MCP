"""Characterization tests pinning serializer, cursor-binding, availability and limit behavior."""
import json

import pytest

from wechat_bridge.core.errors import BridgeError


# --- Compact-JSON serializers are byte-identical ---

JSON_CORPUS = [
    {'b': 1, 'a': 2},                       # key order preserved (no sort_keys)
    {'text': '发票 🐈 café'},                 # non-ASCII kept literally (ensure_ascii=False)
    {'nested': {'x': [1, 2, {'y': None}]}},
    [1, True, False, None],
    {'big': 9007199254740993},              # int > 2**53
    {'s': 'a"b\\c\n\t'},                     # escaping
    {},
    'scalar 发票',
    123,
]


@pytest.mark.parametrize('value', JSON_CORPUS)
def test_compact_json_serializers_are_byte_identical(value):
    from wechat_bridge.core.serialize import compact_json
    from wechat_bridge.mcp.server import _json
    expected = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    assert compact_json(value) == expected
    assert _json(value) == expected


def test_compact_json_serializers_reject_nan():
    from wechat_bridge.core.serialize import compact_json
    from wechat_bridge.mcp.server import _json
    for serialize in (compact_json, _json):
        with pytest.raises(ValueError):
            serialize(float('nan'))


def test_cursor_binding_is_order_insensitive_for_filters():
    # cursor.binding uses sort_keys=True by design; this pins that so a future
    # "unify the serializers" refactor cannot fold cursor.binding into the
    # insertion-order-preserving compact-JSON helper.
    from wechat_bridge.search.cursor import binding
    assert binding('messages', {'a': 1, 'b': 2}, 50) == binding('messages', {'b': 2, 'a': 1}, 50)


# --- Attachment availability over the whole reachable domain ---

@pytest.mark.parametrize('provenance', ['conflicting', 'resource_hardlink', 'resource_layout', 'explicit_resource', 'unsupported'])
@pytest.mark.parametrize('relative_path', [None, 'msg/file/2023-11/test.bin'])
@pytest.mark.parametrize('declared_local', [True, False, None])
@pytest.mark.parametrize('file_identity', [None, [1, 2, 3, 4, 5]])
def test_availability_follows_the_mapping_rules(provenance, relative_path, declared_local, file_identity):
    from wechat_bridge.search.repository import attachment_availability
    record = {'mapping_provenance': provenance, 'relative_path': relative_path,
              'declared_local': declared_local, 'file_identity': file_identity}
    if provenance == 'conflicting':
        expected = 'identity_mismatch'
    elif provenance == 'unsupported':
        expected = 'unsupported_format'
    elif relative_path is None or declared_local is False or file_identity is None:
        expected = 'not_local'
    else:
        expected = 'local_unverified'
    assert attachment_availability(record) == expected


def test_availability_treats_an_empty_path_as_not_local():
    # mapping.py only ever emits relative_path=None or a well-formed path; '' is unreachable and stays not_local.
    from wechat_bridge.search.repository import attachment_availability
    record = {'mapping_provenance': 'resource_hardlink', 'relative_path': '',
              'declared_local': True, 'file_identity': [1, 2, 3, 4, 5]}
    assert attachment_availability(record) == 'not_local'


# --- Strict integer limits keep their distinct error codes (guards require_int_range) ---

@pytest.mark.parametrize('bad', [True, False, 1.0, '1', None, 0, 201])
def test_repository_valid_limit_rejects_nonint_and_out_of_range(bad):
    from wechat_bridge.search.repository import valid_limit
    with pytest.raises(BridgeError, match='invalid_limit'):
        valid_limit(bad)


@pytest.mark.parametrize('ok', [1, 50, 200])
def test_repository_valid_limit_accepts_boundaries(ok):
    from wechat_bridge.search.repository import valid_limit
    valid_limit(ok)


@pytest.mark.parametrize('bad', [True, False, 0, 52428801, 1.0, '1'])
def test_decode_image_rejects_bad_max_bytes_with_content_limit_code(bad):
    import io
    from wechat_bridge.attachments.images import decode_image
    with pytest.raises(BridgeError, match='invalid_content_limit'):
        decode_image(io.BytesIO(b'x'), material=None, max_bytes=bad)
