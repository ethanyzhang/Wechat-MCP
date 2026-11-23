import pytest


@pytest.mark.parametrize("text,expected", [
    ("  Ａｂｃ \t\n 发票  ", "abc 发票"),
    ("", ""),
    ("   ", ""),
    ("ＣＡＦＥ́", "café"),
    ("ﬁ", "fi"),
    ("ǅ", "dž"),
])
def test_normalize_folds_width_case_and_whitespace(text, expected):
    from wechat_bridge.search.tokenize import normalize
    assert normalize(text) == expected


@pytest.mark.parametrize("text", ["ＡＢＣ  x", "café", "ẞ ß", "한국어 ᄒᆞᆫ", "🐈‍⬛ ok"])
def test_normalize_is_idempotent(text):
    from wechat_bridge.search.tokenize import normalize
    assert normalize(normalize(text)) == normalize(text)


@pytest.mark.parametrize("text,offset,expected", [
    ("abc", 0, 0), ("abc", 2, 2), ("abc", 3, 3),
    ("a  b", 1, 1), ("a  b", 2, 3),       # collapsed whitespace run maps to its start / next char
    ("café!", 3, 3), ("café!", 4, 5),  # combining mark stays with its starter
    ("ＡＢ", 1, 1),
    ("", 0, 0),
])
def test_source_offset_maps_normalized_position_to_source(text, offset, expected):
    from wechat_bridge.search.tokenize import source_offset
    assert source_offset(text, offset) == expected
