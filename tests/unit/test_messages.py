import compression.zstd as zstd
import pytest
from tests.support.samples import source, row, identities, QUOTE


def test_plain_and_zstandard_chinese_emoji_and_large_server_id():
    from wechat_bridge.adapters.messages import decode_message
    ids = identities()
    plain = decode_message(row(), source(), ids)
    compressed = decode_message(row(zstd.compress("发票 🐈".encode()), WCDB_CT_message_content=4), source(), ids)
    assert plain.text == compressed.text == "发票 🐈"
    assert plain.source.server_id == "9007199254740994"
    assert plain.utc_timestamp == "2023-11-14T22:13:20+00:00"
    assert plain.parse_status == compressed.parse_status == "ok"


def test_timestamp_unit_is_explicit_not_guessed_by_magnitude():
    from wechat_bridge.adapters.messages import decode_message
    value = decode_message(row(create_time=1700000000000, timestamp_unit="milliseconds"), source(), identities())
    assert value.utc_timestamp == "2023-11-14T22:13:20+00:00"
    assert value.source_timestamp == "1700000000000"
    assert value.timestamp_unit == "milliseconds"


@pytest.mark.parametrize("payload,flag,code", [(b"\xff",0,"invalid_text_encoding"), (b"a\0b",0,"invalid_text_content"),
    (b"bad-zstd",4,"malformed_compression"),(zstd.compress(b"x" * 1048577),4,"payload_too_large"),
    (zstd.compress(b"x")[:-1],4,"malformed_compression"),(b"x",99,"unsupported_compression")])
def test_rejected_payload_preserves_source_identity(payload,flag,code):
    from wechat_bridge.adapters.messages import decode_message
    message = decode_message(row(payload, WCDB_CT_message_content=flag), source(), identities())
    assert message.text is None
    assert message.source.local_id == "1"
    assert code in message.diagnostic_codes


def test_sender_prefix_requires_group_and_matching_structured_identity():
    from wechat_bridge.adapters.messages import decode_message
    assert decode_message(row(b"alice:\nHello"), source(), identities()).text == "Hello"
    conflict = decode_message(row(b"bob:\nHello"), source(), identities())
    assert conflict.text == "bob:\nHello"
    assert "sender_prefix_conflict" in conflict.diagnostic_codes
    assert decode_message(row(b"alice:\nHello"), source(), identities(False)).text == "alice:\nHello"
    missing = decode_message(row(real_sender_id=99), source(), identities())
    assert missing.sender_id is None
    assert missing.parse_status == "partial"


def test_unknown_type_is_retained_with_diagnostic():
    from wechat_bridge.adapters.messages import decode_message
    message = decode_message(row(b"opaque", local_type=99999), source(), identities())
    assert message.type == "unsupported"
    assert message.source_type == "99999"
    assert message.parse_status == "unsupported"
    assert message.text is None


def test_normalized_reply_does_not_expose_xml_or_download_credentials():
    from wechat_bridge.adapters.messages import decode_message
    from dataclasses import asdict
    message = decode_message(row(QUOTE, local_type=(57 << 32) | 49), source(), identities())
    assert message.text == "A reply"
    assert message.reply.quoted_text == "Quoted text"
    assert "private-download-token" not in str(asdict(message))
    assert "<msg>" not in str(asdict(message))


@pytest.mark.parametrize("payload", [b'<msg>', b'<!DOCTYPE msg [<!ENTITY a "secret">]><msg>&a;</msg>',
    b'<msg>' + b'<a>'*33 + b'</a>'*33 + b'</msg>'])
def test_bad_or_overdeep_xml_retains_malformed_message(payload):
    from wechat_bridge.adapters.messages import decode_message
    message = decode_message(row(payload, local_type=(57 << 32) | 49), source(), identities())
    assert message.parse_status == "malformed"
    assert message.text is None


def test_malformed_packed_info_keeps_message_with_diagnostic():
    from wechat_bridge.adapters.messages import decode_message
    message = decode_message(row(packed_info_data=b'\x12\x7f'), source(), identities())
    assert message.text == "发票 🐈"
    assert message.parse_status == "partial"
    assert "malformed_protobuf" in message.diagnostic_codes


def _notice(body, group=True, **values):
    from wechat_bridge.adapters.messages import decode_message
    payload = (("room@chatroom:\n" if group else "") + body).encode()
    return decode_message(row(payload, local_type=10000, real_sender_id=99, **values), source(), identities(group))


def test_plain_system_notice_has_no_sender_or_sender_diagnostic():
    message = _notice("Alice joined the group", group=False)
    assert (message.type, message.text, message.sender_id) == ("system", "Alice joined the group", None)
    assert message.parse_status == "ok" and message.diagnostic_codes == ()


def test_plain_notice_with_a_colon_line_is_kept_whole():
    message = _notice("Group rules:\nbe kind", group=False)
    assert (message.type, message.text) == ("system", "Group rules:\nbe kind")


@pytest.mark.parametrize("group,prefixed", [(False, False), (True, False), (True, True)])
def test_recall_notice_reads_its_content(group, prefixed):
    from wechat_bridge.adapters.messages import decode_message
    from tests.support.samples import REVOKE
    payload = (("room@chatroom:\n" if prefixed else "") + REVOKE).encode()
    message = decode_message(row(payload, local_type=10000), source(), identities(group))
    assert (message.type, message.text, message.parse_status) == ("system", '"Alice" recalled a message', "ok")


def test_group_template_notice_is_filled_from_its_links_without_xml():
    from tests.support.samples import INVITE
    message = _notice(INVITE)
    assert message.type == "system"
    assert message.text == '"Alice" invited "Bob, Carol" to the group'
    assert message.parse_status == "ok" and message.diagnostic_codes == ()
    assert "<" not in message.text and "room@chatroom" not in message.text


def test_template_links_plain_default_separator_and_one_pass_substitution():
    from tests.support.samples import template, profile_link
    links = (profile_link("username", "$from$") + profile_link("from", "Bob", "Carol")
             + '<link name="remark" type="link_plain"><plain><![CDATA[note]]></plain></link>')
    message = _notice(template("$username$ / $from$ / $remark$", links))
    assert message.text == "$from$ / Bob、Carol / note"


@pytest.mark.parametrize("links", [
    "",  # placeholder with no link
    '<link name="username" type="link_revoke"><title>x</title></link>',  # link kind not supported
    "PROFILE_EMPTY",  # member without a nickname: never fall back to a username
    "DUPLICATE",  # two links with one name: ambiguous
])
def test_unresolvable_template_stays_unsupported(links):
    from tests.support.samples import template, profile_link
    links = {"PROFILE_EMPTY": profile_link("username", ""),
             "DUPLICATE": profile_link("username", "Alice") + profile_link("username", "Bob")}.get(links, links)
    message = _notice(template('"$username$" joined', links))
    assert (message.type, message.text, message.parse_status) == ("unsupported", None, "unsupported")
    assert message.diagnostic_codes == ("unsupported_system_template",)


def test_template_output_is_bounded():
    from tests.support.samples import template
    links = '<link name="x" type="link_plain"><plain>' + "a" * 100000 + "</plain></link>"
    message = _notice(template("$x$" * 20, links))
    assert (message.text, message.parse_status) == (None, "malformed")
    assert "payload_too_large" in message.diagnostic_codes


@pytest.mark.parametrize("body,code", [
    ('<sysmsg type="mmchatroomtopmsg"><mmchatroomtopmsg><op>1</op></mmchatroomtopmsg></sysmsg>', "unsupported_system_type"),
    ('<sysmsg type="pat"><pat><template>x</template></pat></sysmsg>', "unsupported_system_type"),
    ('<sysmsg><content>x</content></sysmsg>', "unsupported_system_type"),
    ("<msg><appmsg/></msg>", "unsupported_system_message"),
    ('<sysmsg type="revokemsg"><revokemsg><content></content></revokemsg></sysmsg>', "unsupported_system_variant"),
    ('<sysmsg type="revokemsg"><revokemsg><content><![CDATA[<msg><token>t</token></msg>]]></content></revokemsg></sysmsg>',
     "unsupported_system_variant"),
])
def test_other_system_notices_stay_unsupported_with_a_specific_code(body, code):
    message = _notice(body)
    assert (message.type, message.text, message.parse_status) == ("unsupported", None, "unsupported")
    assert message.diagnostic_codes == (code,)


@pytest.mark.parametrize("body", ['<sysmsg type="revokemsg"><revokemsg>', '<!DOCTYPE x [<!ENTITY a "b">]><sysmsg/>'])
def test_malformed_system_xml_is_retained_without_text(body):
    message = _notice(body)
    assert (message.type, message.text, message.parse_status) == ("unsupported", None, "malformed")


@pytest.mark.parametrize("prefix", ["other@chatroom", "alice"])
def test_xml_behind_a_foreign_prefix_never_becomes_text(prefix):
    from tests.support.samples import REVOKE
    message = _notice(prefix + ":\n" + REVOKE, group=False)
    assert (message.type, message.text) == ("unsupported", None)
    assert message.diagnostic_codes == ("unsupported_system_prefix",)
    group = _notice(prefix + ":\n" + REVOKE)  # inside a group, only room@chatroom is stripped
    assert (group.type, group.text) == ("unsupported", None)


def test_pat_is_a_system_message_with_the_rendered_title():
    from wechat_bridge.adapters.messages import decode_message
    from tests.support.samples import pat
    for local_type in ((62 << 32) | 49, 49):
        message = decode_message(row(pat().encode(), local_type=local_type), source(), identities(False))
        assert (message.type, message.text, message.parse_status) == ("system", '"Alice" patted "Bob"', "ok")
        assert message.sender_id is not None
    empty = decode_message(row(pat("").encode(), local_type=(62 << 32) | 49), source(), identities(False))
    assert (empty.type, empty.text, empty.diagnostic_codes) == ("unsupported", None, ("unsupported_system_variant",))


def test_in_notice_action_links_keep_only_their_label():
    link = '<_wc_custom_link_ href="weixin://synthetic/token">Re-edit</_wc_custom_link_>'
    plain = _notice("You recalled a message " + link, group=False)
    assert plain.text == "You recalled a message Re-edit"
    recall = '<sysmsg type="revokemsg"><revokemsg><content><![CDATA[You recalled a message ' + link + ']]></content></revokemsg></sysmsg>'
    message = _notice(recall)
    assert (message.type, message.text) == ("system", "You recalled a message Re-edit")
    assert "weixin://" not in message.text


@pytest.mark.parametrize("payload", ["\ufeff" + '<sysmsg type="x"/>', "\nother@chatroom:\n<sysmsg/>",
                                     "Notice text then other@chatroom:\n<sysmsg/>"])
def test_hidden_xml_never_becomes_text(payload):
    message = _notice(payload, group=False)
    assert (message.type, message.text) == ("unsupported", None)


def test_bom_before_a_recall_notice_is_ignored():
    from tests.support.samples import REVOKE
    assert _notice("\ufeff" + REVOKE, group=False).text == '"Alice" recalled a message'


def test_profile_separator_expansion_is_bounded_before_joining():
    from tests.support.samples import template, profile_link
    message = _notice(template("$names$", profile_link("names", *["a"] * 200, separator="x" * 6000)))
    assert (message.text, message.parse_status) == (None, "malformed")
    assert "payload_too_large" in message.diagnostic_codes


def test_nickname_that_is_an_element_is_refused_but_angle_names_stay():
    from tests.support.samples import template, profile_link
    for nickname in ("<msg><token>t</token></msg>", '<a title=">"/>', '<a title=">">x</a>'):
        refused = _notice(template("$username$ joined", profile_link("username", nickname)))
        assert (refused.text, refused.diagnostic_codes) == (None, ("unsupported_system_template",))
    from wechat_bridge.adapters.messages import readable
    from wechat_bridge.core.errors import BridgeError
    for value in ('<a title=">"/>', "<a/>", "<b>x</b>"):
        with pytest.raises(BridgeError):
            readable(value)
    assert readable("<a> and <b>") == "<a> and <b>"
    kept = _notice(template("$username$ invited $names$", profile_link("username", "<Alice>") + profile_link("names", "<Bob>")))
    assert (kept.type, kept.text) == ("system", "<Alice> invited <Bob>")


def test_quoted_angle_in_a_link_attribute_does_not_leak_the_link():
    link = '<_wc_custom_link_ title=">" href="weixin://synthetic/token">Re-edit</_wc_custom_link_>'
    message = _notice("You recalled a message " + link, group=False)
    assert message.text == "You recalled a message Re-edit"
    broken = _notice('You recalled a message <_wc_custom_link_ href="weixin://synthetic/token">Re-edit', group=False)
    assert (broken.text, broken.diagnostic_codes) == (None, ("unsupported_system_variant",))
