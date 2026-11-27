"""Bounded decoding; malformed/unsupported records retain their source identity."""
import compression.zstd as zstd
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import re
import xml.etree.ElementTree as ET

from ..core.errors import BridgeError
from .models import Message
from .wire import parse_fields

MAX_PAYLOAD = 1048576


def decode_payload(content: bytes | str | None, flag: int) -> bytes:
    """Return the raw payload, zstd-decompressed when flag is 4; output is capped at 1 MiB.

    Raises invalid_payload, payload_too_large, unsupported_compression or malformed_compression.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    if data is None:
        data = b""
    if not isinstance(data, bytes):
        raise BridgeError("invalid_payload")
    if len(data) > MAX_PAYLOAD:
        raise BridgeError("payload_too_large")
    if flag == 0:
        return data
    if flag != 4:
        raise BridgeError("unsupported_compression")
    try:
        decoder = zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: 23})
        result = decoder.decompress(data, MAX_PAYLOAD + 1)
        if len(result) > MAX_PAYLOAD:
            raise BridgeError("payload_too_large")
        if not decoder.eof or decoder.unused_data:
            raise BridgeError("malformed_compression")
        return result
    except zstd.ZstdError:
        raise BridgeError("malformed_compression") from None


def payload_text(data: bytes) -> str:
    """Decode strict UTF-8 without NUL; raises invalid_text_encoding or invalid_text_content."""
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise BridgeError("invalid_text_encoding") from None
    if "\0" in text:
        raise BridgeError("invalid_text_content")
    return text


def parse_xml(data: bytes):
    """Parse untrusted XML with DOCTYPE and ENTITY refused, depth at most 32 and 4096 nodes.

    Raises payload_too_large, xml_declaration_denied, xml_structure_limit or malformed_xml.
    """
    if len(data) > MAX_PAYLOAD:
        raise BridgeError("payload_too_large")
    text = payload_text(data)
    if "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise BridgeError("xml_declaration_denied")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = nodes = 0; root = None
    try:
        for offset in range(0, len(text), 4096):
            parser.feed(text[offset:offset + 4096])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1; nodes += 1
                    if root is None:
                        root = element
                    if depth > 32 or nodes > 4096:
                        raise BridgeError("xml_structure_limit")
                else:
                    depth -= 1
        parser.close()
        if root is None or depth:
            raise BridgeError("malformed_xml")
        return root
    except ET.ParseError:
        raise BridgeError("malformed_xml") from None


# A template placeholder is $name$; a link name is a word.
PLACEHOLDER = re.compile(r"\$(\w+)\$")
# A group notice is "<conversation username>:\n<body>"; a first line that ends in ":" before an XML body is a prefix too.
PREFIXED_XML = re.compile(r"[^\n<]*:\n\s*<")


# WeChat marks an action inside a notice ("Re-edit", "Undo") as <_wc_custom_link_ href="...">label</_wc_custom_link_>;
# the readable text keeps the label and drops the link.
CUSTOM_LINK = re.compile(r"<_wc_custom_link_\b[^>]*>(.*?)</_wc_custom_link_>", re.S)


def unlink(text: str) -> str:
    """Notice text with WeChat's in-notice action links reduced to their labels."""
    return CUSTOM_LINK.sub(r"\1", text)


def _notice_value(value: str | None) -> str:
    """One rendered piece of a notice; refuses an empty value or one that is itself an XML element."""
    value = unlink(value or "").strip()
    if not value or (value.startswith("<") and value.endswith(">")):
        raise BridgeError("unsupported_system_variant")
    return value


def _fill_template(content) -> str:
    """Fill a sysmsgtemplate's template from its link_list in one pass over the template.

    Every placeholder needs exactly one link of a known kind whose names are all present; anything else raises
    unsupported_system_template, so a name is never guessed. Output is bounded by MAX_PAYLOAD characters.
    """
    template = content.findtext("template") or ""
    links = {}
    for link in content.findall("link_list/link"):
        name = link.get("name")
        if name in links:
            links[name] = None
        else:
            links[name] = link
    values = {}
    for name in set(PLACEHOLDER.findall(template)):
        link = links.get(name)
        if link is None:
            raise BridgeError("unsupported_system_template")
        kind = link.get("type")
        if kind == "link_profile":
            names = [(member.findtext("nickname") or "").strip() for member in link.findall("memberlist/member")]
            if not names or not all(names):
                raise BridgeError("unsupported_system_template")
            separator = link.findtext("separator")
            values[name] = (separator if separator else "、").join(names)
        elif kind == "link_plain":
            values[name] = _notice_value(link.findtext("plain"))
        else:
            raise BridgeError("unsupported_system_template")
    pieces = PLACEHOLDER.split(template)
    # split alternates literal text and placeholder names: even indexes are literal, odd are names.
    size = sum(len(piece) if index % 2 == 0 else len(values[piece]) for index, piece in enumerate(pieces))
    if size > MAX_PAYLOAD:
        raise BridgeError("payload_too_large")
    return _notice_value("".join(piece if index % 2 == 0 else values[piece] for index, piece in enumerate(pieces)))


def system_text(data: bytes) -> str:
    """Readable text of an XML system notice (local_type 10000 body starting with "<").

    Supports recall notices (revokemsg) and filled templates (sysmsgtemplate). Raises unsupported_system_message for
    a root other than sysmsg, unsupported_system_type for any other sysmsg type, and the parse_xml codes.
    """
    root = parse_xml(data)
    if root.tag != "sysmsg":
        raise BridgeError("unsupported_system_message")
    kind = root.get("type")
    if kind == "revokemsg":
        return _notice_value(root.findtext("revokemsg/content"))
    if kind == "sysmsgtemplate":
        content = root.find("sysmsgtemplate/content_template")
        if content is None:
            raise BridgeError("unsupported_system_template")
        return _fill_template(content)
    raise BridgeError("unsupported_system_type")


def decode_message(row, source, identities) -> Message:
    """Decode one source row into a Message; never raises for bad content.

    Unsupported or malformed rows keep their identity and carry a status and diagnostic code with text=None.
    """
    conversation_id, username, kind = identities.conversation(source.database_id, source.table)
    raw_type = row.get("local_type")
    # A system notice (local_type 10000) is WeChat's, not a member's: its real_sender_id names no Name2Id row in the
    # tested builds, so it has no sender and no sender diagnostic.
    notice = raw_type == 10000
    sender = None if notice else identities.sender(source.database_id, row.get("real_sender_id"))
    codes = [] if notice or sender.status == "ok" else ["sender_" + sender.status]
    value = row.get("create_time"); unit = row.get("timestamp_unit")
    utc = None
    try:
        if type(value) is not int or unit not in ("seconds", "milliseconds"):
            raise BridgeError("invalid_timestamp")
        utc = (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value * (1000000 if unit == "seconds" else 1000))).isoformat()
    except (OverflowError, ValueError, BridgeError):
        codes.append("invalid_timestamp")
    sender_id = None if notice else sender.contact_id
    message = Message(identities.message_id(source), conversation_id, sender_id, str(value), str(unit), utc,
                      str(raw_type), "unsupported", None, source, "partial" if codes else "ok", tuple(codes))
    try:
        data = decode_payload(row.get("message_content"), row.get("WCDB_CT_message_content"))
        text = payload_text(data)
        if notice:
            # A group notice may carry the conversation's own username as a prefix; only that prefix is removed.
            if kind == "group" and username and text.startswith(username + ":\n"):
                text = text[len(username) + 2:]
            body = text.lstrip()
            if body.startswith("<"):
                text = system_text(body.encode())
            elif PREFIXED_XML.match(text):
                raise BridgeError("unsupported_system_prefix")
            else:
                text = unlink(text)
        elif kind == "group" and ":\n" in text:
            prefix, rest = text.split(":\n", 1)
            if sender.status == "ok" and prefix == sender.source_username:
                text = rest; data = rest.encode()
            else:
                codes.append("sender_prefix_conflict")
        packed = row.get("packed_info_data")
        if packed:
            try:
                parse_fields(packed)
            except BridgeError as error:
                codes.append(error.code)
        base_type = raw_type & 0xffffffff if type(raw_type) is int else None
        if notice:
            message = replace(message, type="system", text=text)
        elif raw_type == 1:
            message = replace(message, type="text", text=text)
        elif base_type == 3:
            root = parse_xml(data)
            if root.tag != "msg" or root.find("img") is None:
                raise BridgeError("unsupported_image_message")
            message = replace(message, type="image")
        elif base_type == 49:
            root = parse_xml(data)
            if root.tag != "msg" or root.find("appmsg") is None:
                raise BridgeError("unsupported_app_message")
            app = root.find("appmsg"); subtype = app.findtext("type")
            source_subtype = raw_type >> 32
            if source_subtype and subtype != str(source_subtype):
                raise BridgeError("appmsg_type_mismatch")
            if subtype == "57":
                from .replies import parse_reply
                message = replace(message, type="reply", text=app.findtext("title") or None)
                reply = parse_reply(message, data)
                message = replace(message, reply=reply)
                if reply is None or reply.resolution_status == "unsupported":
                    codes.append("reply_variant_unsupported")
            elif subtype == "62" and app.find("patinfo") is not None:
                # A pat (拍一拍): WeChat renders who patted whom into the title; patinfo holds only usernames.
                message = replace(message, type="system", text=_notice_value(app.findtext("title")))
            elif subtype == "6":
                message = replace(message, type="file", text=app.findtext("title") or None)
            else:
                raise BridgeError("unsupported_app_message")
        else:
            raise BridgeError("unsupported_message_type")
        return replace(message, parse_status="partial" if codes else "ok", diagnostic_codes=tuple(codes))
    except BridgeError as error:
        codes.append(error.code)
        status = "unsupported" if error.code.startswith("unsupported_") else "malformed"
        return replace(message, text=None, parse_status=status, diagnostic_codes=tuple(codes))
