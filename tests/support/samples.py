from wechat_bridge.adapters.models import SourceRef

# HMAC identity-lookup key for fixtures; distinct in role from the SQLCipher
# database key in tests.support.encrypted.KEY (do not conflate the two).
IDENTITY_KEY = bytes(range(32))

QUOTE = b'''<msg><appmsg><title>A reply</title><type>57</type><refermsg><type>1</type><svrid>9007199254740993</svrid><displayname>Duplicate name</displayname><content>Quoted text</content></refermsg><cdnurl>private-download-token</cdnurl></appmsg></msg>'''


def source(db="db1", local_id="1", server_id="9007199254740994"):
    return SourceRef("account", db, "Msg_synthetic", local_id, server_id, "2026-01-01T00:00:00+00:00")


def row(content="发票 🐈".encode(), **values):
    base = {"local_id": 1, "server_id": 9007199254740994, "local_type": 1, "real_sender_id": 1,
            "create_time": 1700000000, "timestamp_unit": "seconds", "message_content": content,
            "WCDB_CT_message_content": 0, "packed_info_data": None}
    return base | values


def identities(group=True):
    from wechat_bridge.adapters.identity import IdentityLookup
    ids = IdentityLookup("account", IDENTITY_KEY)
    ids.add_sender("db1", 1, "alice")
    ids.add_conversation("db1", "Msg_synthetic", "room@chatroom" if group else "alice")
    return ids


# Synthetic WeChat system notices (local_type 10000) and a pat (appmsg type 62), shaped like the tested build's.
REVOKE = '<sysmsg type="revokemsg"><revokemsg><content><![CDATA["Alice" recalled a message]]></content><revoketime>0</revoketime></revokemsg></sysmsg>'


def template(text, links=""):
    """A sysmsgtemplate notice with the given template text and link_list XML."""
    return (f'<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template type="tmpl_type_profile"><plain></plain>'
            f'<template><![CDATA[{text}]]></template><link_list>{links}</link_list></content_template></sysmsgtemplate></sysmsg>')


def profile_link(name, *nicknames, separator=None):
    """A link_profile link naming members by nickname (usernames are synthetic)."""
    members = "".join(f"<member><username>wxid_synthetic{i}</username><nickname><![CDATA[{n}]]></nickname></member>"
                      for i, n in enumerate(nicknames))
    sep = f"<separator><![CDATA[{separator}]]></separator>" if separator is not None else ""
    return f'<link name="{name}" type="link_profile"><memberlist>{members}</memberlist>{sep}</link>'


INVITE = template('"$username$" invited "$names$" to the group',
                  profile_link("username", "Alice") + profile_link("names", "Bob", "Carol", separator=", "))


def pat(title='"Alice" patted "Bob"'):
    """A pat (拍一拍) appmsg; patinfo carries usernames and a ${...} template, the title the rendered line."""
    return (f'<msg><appmsg appid="" sdkver="0"><title>{title}</title><type>62</type><patinfo>'
            '<fromusername>wxid_synthetic0</fromusername><chatusername>room@chatroom</chatusername>'
            '<pattedusername>wxid_synthetic1</pattedusername><patsuffix></patsuffix><patsuffixversion>0</patsuffixversion>'
            '<template><![CDATA["${wxid_synthetic0}" patted "${wxid_synthetic1}"]]></template></patinfo></appmsg></msg>')
