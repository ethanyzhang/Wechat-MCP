"""Moments (朋友圈) records parsed from sns.db rows: a strict whitelist of fields, never the raw XML.

The post XML carries media URLs, keys, tokens, encryption indexes, hashes and precise coordinates; none of those is ever
copied into a record. Only text, author, time, a media summary (type and size), a link title, the venue name and city,
and the likes and comments are kept. Every value is untrusted text from other people.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..core.errors import BridgeError
from .messages import parse_xml

MAX_CONTENT_BYTES = 262144        # larger rows are never read (SQL side), only reported as oversize
MAX_TEXT = 20000
MAX_COMMENT_TEXT = 2000
MAX_NAME = 128
MAX_COMMENTS = 100
MAX_LIKES = 200
MAX_MEDIA = 30
MIN_TIME = -62135596800
MAX_TIME = 253402300799


@dataclass(frozen=True)
class Moment:
    """One Moments post. `parse_status` is ok, summary_only (only the feed summary exists), oversize or malformed."""
    moment_id: str
    author_id: str | None
    utc_timestamp: str | None
    time_sort: int
    text: str | None
    content_type: int | None = None
    link_title: str | None = None
    media: list = field(default_factory=list)
    media_count: int = 0
    location: dict | None = None
    private: bool | None = None
    likes: list = field(default_factory=list)
    like_count: int = 0
    likes_truncated: bool = False
    comments: list = field(default_factory=list)
    comment_count: int = 0
    comments_truncated: bool = False
    source_read_flag: int | None = None
    parse_status: str = "ok"
    diagnostic_codes: tuple = ()


@dataclass(frozen=True)
class MomentNotification:
    """One row of the account's Moments notification table. kind_inferred is derived from the type code and is not a source fact."""
    notification_id: str
    moment_id: str | None
    utc_timestamp: str | None
    time_sort: int
    type_code: int | None
    kind_inferred: str
    from_id: str | None
    from_nickname: str | None
    to_id: str | None
    text: str | None
    involves_me: bool | None
    deleted: bool | None
    source_unread_flag: int | None
    parse_status: str = "ok"
    diagnostic_codes: tuple = ()


def iso(seconds):
    """UTC ISO time and integer sort key for epoch seconds, or (None, 0) when not a plausible integer."""
    if type(seconds) is not int or not MIN_TIME <= seconds <= MAX_TIME:
        return None, 0
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat(), seconds


def _text(element, tag, limit=MAX_TEXT):
    child = element.find(tag) if element is not None else None
    value = child.text.strip() if child is not None and isinstance(child.text, str) else ""
    if "\0" in value:
        return None
    return value[:limit] if value else None


def _int(element, tag):
    value = _text(element, tag, 24)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _people(parent, identities, limit, *, with_text):
    """Likes or comments from one user_comment list: contact id, a bounded source nickname, time, and for comments the text.

    Entries the source marks deleted keep their time and author but never their text. The list is capped to the newest `limit`.
    """
    entries = []
    for item in (parent.findall("user_comment") if parent is not None else []):
        username = _text(item, "username", 256)
        created, sort = iso(_int(item, "create_time"))
        deleted = _int(item, "b_deleted")
        entry = {"user_id": identities.contact_id(username) if username else None,
                 "nickname": _text(item, "nickname", MAX_NAME), "utc_timestamp": created,
                 "deleted": None if deleted is None else deleted != 0}
        if with_text:
            reply = _text(item, "ref_username", 256)
            entry["reply_to_user_id"] = identities.contact_id(reply) if reply else None
            entry["text"] = None if deleted != 0 else _text(item, "content", MAX_COMMENT_TEXT)
        entries.append((sort, entry))
    total = len(entries)
    entries.sort(key=lambda pair: pair[0])
    kept = [entry for _, entry in entries[-limit:]]
    return kept, total, total > limit


def parse_moment(tid, username, content, identities, *, read_flag=None, fallback_time=None):
    """Build a Moment from one SnsTimeLine row. Never raises for bad content: it returns a status record without text.

    `content` is the XML text, or None when the row was over MAX_CONTENT_BYTES (it was never read).
    """
    moment_id = identities.moment_id(tid)
    author = identities.contact_id(username) if isinstance(username, str) and username and "\0" not in username else None
    fallback = iso(fallback_time)
    if content is None:
        return Moment(moment_id, author, fallback[0], fallback[1], None, source_read_flag=read_flag,
                      parse_status="oversize", diagnostic_codes=("moment_content_oversize",))
    try:
        root = parse_xml(content.encode("utf-8"))
    except (BridgeError, UnicodeError) as error:
        return Moment(moment_id, author, fallback[0], fallback[1], None, source_read_flag=read_flag,
                      parse_status="malformed", diagnostic_codes=("moment_" + getattr(error, "code", "malformed_xml"),))
    post = root.find("TimelineObject")
    extra = root.find("LocalExtraInfo")
    stamp, sort = iso(_int(post, "createTime"))
    codes = []
    if stamp is None:
        stamp, sort = fallback
        codes.append("moment_time_from_feed")
    media = []
    objects = post.find("ContentObject") if post is not None else None
    media_nodes = objects.findall("mediaList/media") if objects is not None else []
    for node in media_nodes[:MAX_MEDIA]:
        size = node.find("size")
        def number(name):
            try:
                return int(size.get(name)) if size is not None and size.get(name) is not None else None
            except ValueError:
                return None
        media.append({"type": _int(node, "type"), "width": number("width"), "height": number("height"), "size_bytes": number("totalSize")})
    place = post.find("location") if post is not None else None
    location = None
    if place is not None:
        # Only the venue label and city the feed itself shows; never coordinates or the street address.
        name, city = (place.get("poiName") or "").strip()[:MAX_NAME], (place.get("city") or "").strip()[:MAX_NAME]
        if name or city:
            location = {"poi_name": name or None, "city": city or None}
    likes, like_count, likes_cut = _people(extra.find("like_user_list") if extra is not None else None, identities, MAX_LIKES, with_text=False)
    comments, comment_count, comments_cut = _people(extra.find("comment_user_list") if extra is not None else None, identities, MAX_COMMENTS, with_text=True)
    private = _int(post, "private")
    return Moment(moment_id, author, stamp, sort, _text(post, "contentDesc"), _int(objects, "type"),
                  _text(objects, "title", 512), media, len(media_nodes), location,
                  None if private is None else private != 0, likes, like_count, likes_cut, comments, comment_count, comments_cut,
                  read_flag, "ok", tuple(codes))


def summary_moment(tid, username, summary, created, identities, *, read_flag=None):
    """A post that has only a feed summary row (no body in SnsTimeLine): author, time and the summary text."""
    stamp, sort = iso(created)
    author = identities.contact_id(username) if isinstance(username, str) and username and "\0" not in username else None
    text = summary.strip()[:MAX_TEXT] if isinstance(summary, str) and "\0" not in summary and summary.strip() else None
    return Moment(identities.moment_id(tid), author, stamp, sort, text, source_read_flag=read_flag,
                  parse_status="summary_only", diagnostic_codes=("moment_body_missing",))


def notification(row, identities):
    """Build a MomentNotification from one SnsMessage_tmp3 row (explicit columns only); never raises for bad values."""
    stamp, sort = iso(row.get("create_time"))
    code = row.get("type") if type(row.get("type")) is int else None
    text = row.get("content") if isinstance(row.get("content"), str) and "\0" not in row["content"] and row["content"].strip() else None
    deleted = row.get("del_status") if type(row.get("del_status")) is int else None
    def person(name):
        return identities.contact_id(name) if isinstance(name, str) and name and "\0" not in name else None
    feed = row.get("feed_id") if type(row.get("feed_id")) is int else None
    nickname = row.get("from_nickname")
    return MomentNotification(
        identities.notification_id(row["local_id"]), identities.moment_id(feed) if feed is not None else None, stamp, sort, code,
        {1: "like", 2: "comment"}.get(code, "unknown") if code != 2 or text else "unknown",
        person(row.get("from_username")), nickname[:MAX_NAME] if isinstance(nickname, str) and "\0" not in nickname else None,
        person(row.get("to_username")), text[:MAX_COMMENT_TEXT] if text and deleted == 0 else None,
        None if type(row.get("is_relative_me")) is not int else row["is_relative_me"] != 0,
        None if deleted is None else deleted != 0,
        row.get("is_unread") if type(row.get("is_unread")) is int else None,
        "ok" if stamp else "partial", () if stamp else ("notification_invalid_time",))
