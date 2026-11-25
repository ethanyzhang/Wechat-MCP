"""Small explicit local commands; output is JSON on stdout.

One subcommand per MCP tool (status, list-conversations, ..., send-message) calls the same ToolService and QueryRepository
methods the tools call and prints the same JSON object. Every command reads WeChat's databases when it runs and exits; nothing
stays in the background. Only send-message changes anything; it uses the send guards of send/sender.py and send/log.py.
"""
import argparse
import json
import os
import sys
from pathlib import Path
import platform
import plistlib

from .core.config import DEFAULT_STATE, DEFAULT_LIBRARY, create_config, load_config
from .core.errors import BridgeError
from .core.limits import DEFAULT_RECORDS
from .source.discovery import discover


def doctor(state_dir: Path) -> dict:
    """Redacted environment summary: versions, library availability and whether config exists."""
    result = {"python_version": platform.python_version(), "architecture": platform.machine(),
              "sqlcipher_available": DEFAULT_LIBRARY.is_file(),
              "configured": (state_dir / "config.json").is_file()}
    try:
        with Path("/Applications/WeChat.app/Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        result["wechat_version"] = info.get("CFBundleShortVersionString")
        result["wechat_build"] = info.get("CFBundleVersion")
    except (OSError, plistlib.InvalidFileException):
        result["wechat_version"] = None
    return result


class _Parser(argparse.ArgumentParser):
    """Argument errors print a fixed code, never argparse's text, which echoes the caller's value."""
    def error(self, message):
        print(json.dumps({"error": "invalid_arguments"}))
        raise SystemExit(2)


EXIT_UNCONFIRMED = 3   # send-message: the press may or may not have sent; never resend


def _add_tool_commands(subs) -> None:
    """Subcommands mirroring the MCP tools; flags are the tool arguments with dashes."""
    def paged(parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_RECORDS)
        parser.add_argument("--cursor")

    def window(parser):
        parser.add_argument("--start", help="ISO-8601 time with a timezone")
        parser.add_argument("--end", help="ISO-8601 time with a timezone")

    subs.add_parser("status")
    c = subs.add_parser("list-conversations")
    c.add_argument("--query", default="")
    c.add_argument("--kind")
    c.add_argument("--unread-only", action="store_true")
    c.add_argument("--sort", default="name")
    paged(c)
    c = subs.add_parser("list-contacts")
    c.add_argument("--query", default="")
    paged(c)
    c = subs.add_parser("get-group")
    c.add_argument("conversation_id")
    paged(c)
    c = subs.add_parser("search-messages", aliases=["search"])
    c.add_argument("query")
    c.add_argument("--conversation-id")
    c.add_argument("--sender-id")
    window(c)
    c.add_argument("--type")
    paged(c)
    c = subs.add_parser("get-messages")
    c.add_argument("conversation_id")
    c.add_argument("--sender-id")
    window(c)
    c.add_argument("--type")
    paged(c)
    c = subs.add_parser("recent-messages")
    c.add_argument("--start", required=True, help="ISO-8601 time with a timezone")
    c.add_argument("--end", help="ISO-8601 time with a timezone")
    c.add_argument("--type")
    c.add_argument("--sender-id")
    c.add_argument("--order", default="desc")
    paged(c)
    c = subs.add_parser("list-moments")
    c.add_argument("--author-id")
    c.add_argument("--query", default="")
    window(c)
    paged(c)
    c = subs.add_parser("get-moment")
    c.add_argument("moment_id")
    c = subs.add_parser("list-moment-notifications")
    c.add_argument("--moment-id")
    window(c)
    paged(c)
    c = subs.add_parser("get-context")
    c.add_argument("message_id")
    c.add_argument("--before", type=int, default=10)
    c.add_argument("--after", type=int, default=10)
    c = subs.add_parser("list-attachments")
    c.add_argument("--message-id")
    c.add_argument("--kind")
    c.add_argument("--representation")
    c.add_argument("--availability")
    paged(c)
    c = subs.add_parser("get-attachment")
    c.add_argument("attachment_id")
    c.add_argument("--representation", default="auto")
    c.add_argument("--max-bytes", type=int)
    c.add_argument("--output", type=Path, help="write the bytes to this new file (never overwrites)")
    c = subs.add_parser("send-message", help="send one text message, text on stdin")
    c.add_argument("--conversation-id", required=True)
    c.add_argument("--recipient-name", required=True)


# Command -> call of the ToolService/QueryRepository method behind the MCP tool of the same name; the one list of read commands.
TOOLS = {
    "status": lambda s, a: s.status(),
    "list-conversations": lambda s, a: s.queries.list_conversations(a.query, kind=a.kind, unread_only=a.unread_only, sort=a.sort,
                                                                    limit=a.limit, cursor=a.cursor),
    "list-contacts": lambda s, a: s.queries.list_contacts(a.query, limit=a.limit, cursor=a.cursor),
    "get-group": lambda s, a: s.queries.get_group(a.conversation_id, limit=a.limit, cursor=a.cursor),
    "search-messages": lambda s, a: s.queries.search_messages(a.query, conversation_id=a.conversation_id, sender_id=a.sender_id,
                                                              start=a.start, end=a.end, type=a.type, limit=a.limit, cursor=a.cursor),
    "get-messages": lambda s, a: s.queries.get_messages(a.conversation_id, sender_id=a.sender_id, start=a.start, end=a.end,
                                                        type=a.type, limit=a.limit, cursor=a.cursor),
    "recent-messages": lambda s, a: s.queries.recent_messages(a.start, end=a.end, type=a.type, sender_id=a.sender_id,
                                                              order=a.order, limit=a.limit, cursor=a.cursor),
    "list-moments": lambda s, a: s.queries.list_moments(author_id=a.author_id, query=a.query, start=a.start, end=a.end,
                                                        limit=a.limit, cursor=a.cursor),
    "get-moment": lambda s, a: s.queries.get_moment(a.moment_id),
    "list-moment-notifications": lambda s, a: s.queries.list_moment_notifications(moment_id=a.moment_id, start=a.start, end=a.end,
                                                                                  limit=a.limit, cursor=a.cursor),
    "get-context": lambda s, a: s.queries.get_context(a.message_id, before=a.before, after=a.after),
    "list-attachments": lambda s, a: s.queries.list_attachments(a.message_id, kind=a.kind, representation=a.representation,
                                                                availability=a.availability, limit=a.limit, cursor=a.cursor),
    "get-attachment": lambda s, a: _get_attachment(s, a),
}
ALIASES = {"search": "search-messages"}


def _get_attachment(service, args) -> dict:
    """Metadata always; the bytes only into --output, a new private file (O_EXCL, 0600) written after the size checks."""
    from .mcp.service import DEFAULT_CONTENT_BYTES
    max_bytes = DEFAULT_CONTENT_BYTES if args.max_bytes is None else args.max_bytes
    metadata, data = service.attachment(args.attachment_id, representation=args.representation, max_bytes=max_bytes)
    metadata.pop("resource_uri", None)   # an MCP resource address; the CLI delivers bytes through --output instead
    if args.output is not None and data is not None:
        try:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise BridgeError("output_exists") from None
        except OSError:
            raise BridgeError("output_unwritable") from None
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except OSError:
            os.close(fd)
            args.output.unlink(missing_ok=True)
            raise BridgeError("output_unwritable") from None
        os.close(fd)
        metadata["output_written"] = True
    else:
        metadata["output_written"] = False
    return metadata


def _send_message(config, args, *, driver=None, log_directory=None) -> dict:
    """Send stdin's text (trailing newlines removed) through the Sender the MCP tool uses, with the same log, limits and lock."""
    from .mcp.service import ToolService
    from .send.log import DEFAULT_DIR
    from .send.sender import build_sender
    text = sys.stdin.read().rstrip("\n")
    if driver is None:
        from .send.macos import MacDriver
        driver = MacDriver(config)
        driver.prepare()   # build the helper (swiftc, first use only) before the send lock, as `serve` does at start
    sender = build_sender(ToolService(config).queries, driver, log_directory or DEFAULT_DIR)
    return sender.send(args.conversation_id, args.recipient_name, text)


def main(argv: list[str] | None = None) -> int:
    """Run one command and return its exit code: 0 ok, 2 blocked or error, 1 failed proof, 3 send unconfirmed, 130 interrupted.

    Errors print a code only; `serve` writes them to stderr so stdout stays protocol-only.
    """
    parser = _Parser(prog="wechat-bridge")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WECHAT_BRIDGE_STATE_DIR", DEFAULT_STATE)))
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    subs.add_parser("discover")
    subs.add_parser("inspect-schema")
    _add_tool_commands(subs)
    serve = subs.add_parser("serve")
    serve.add_argument("--enable-send", action="store_true", help="also register the text-only send_message tool (drives the WeChat app; see docs/mcp.md)")
    prove = subs.add_parser("prove")
    prove.add_argument("--selection", type=Path, required=True)
    verify = subs.add_parser("verify-keys")
    verify.add_argument("--manifest", type=Path)
    setup = subs.add_parser("setup")
    setup_subs = setup.add_subparsers(dest="setup_command", required=True)
    setup_subs.add_parser("prepare")
    for name in ("run", "recover"):
        setup_subs.add_parser(name).add_argument("--plan-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args.state_dir)
        elif args.command == "serve":
            from .mcp.server import serve
            serve(load_config(args.state_dir), enable_send=args.enable_send)
            return 0
        elif args.command == "send-message":
            result = _send_message(load_config(args.state_dir), args)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["state"] == "sent" else EXIT_UNCONFIRMED
        elif ALIASES.get(args.command, args.command) in TOOLS:
            from .mcp.service import ToolService
            result = TOOLS[ALIASES.get(args.command, args.command)](ToolService(load_config(args.state_dir)), args)
        elif args.command == "inspect-schema":
            from .adapters.wechat_4_1 import open_adapter
            result = open_adapter(load_config(args.state_dir)).public_dict()
        elif args.command == "prove":
            from .proof import load_selection, run_proof
            report = run_proof(load_config(args.state_dir), load_selection(args.selection))
            print(json.dumps(report.public_dict()))
            return {"passed": 0, "blocked": 2, "failed": 1}[report.status]
        elif args.command == "setup":
            from .setup.prepare import prepare_setup, load_setup
            cfg = load_config(args.state_dir)
            if args.setup_command == "prepare":
                result = prepare_setup(cfg, discover(cfg)).public_dict()
            else:
                plan = load_setup(cfg, args.plan_id)
                if args.setup_command == "run":
                    from .setup.capture import run_capture
                    outcome = run_capture(plan)
                    result = {"status": outcome.status, "recovery_status": outcome.recovery_status,
                              "key_copies_removed": outcome.key_copies_removed}
                else:
                    from .setup.recovery import recover_setup
                    outcome = recover_setup(plan)
                    result = {"status": outcome.status, "key_copies_removed": outcome.key_copies_removed}
                print(json.dumps(result))
                return 0 if result["status"] in ("verified", "recovered") and result.get("recovery_status", "recovered") == "recovered" else 2
        elif args.command == "verify-keys":
            from .source.keys import load_manifest, verify_keys, publish_verified_manifest
            cfg = load_config(args.state_dir)
            manifest_path = args.manifest or cfg.state_dir / "keys.json"
            if not manifest_path.exists():
                raise BridgeError("setup_required")
            manifest = load_manifest(manifest_path)
            inventory = discover(cfg)
            coverage = verify_keys(inventory, manifest, cfg)
            if not coverage.ready:
                from .source.keys import load_master_secrets, verify_with_master_secrets
                manifest, coverage = verify_with_master_secrets(inventory, manifest, coverage, load_master_secrets(cfg), cfg)
            publish_verified_manifest(manifest, coverage, cfg)
            print(json.dumps(coverage.public_dict()))
            return 0 if coverage.ready else 2
        else:
            result = discover(create_config(args.state_dir)).public_dict()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BridgeError as error:
        print(json.dumps(error.public_dict()), file=sys.stderr if args.command == 'serve' else sys.stdout)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "operation_cancelled"}), file=sys.stderr if args.command == 'serve' else sys.stdout)
        return 130
    except Exception:
        print(json.dumps({"error": "internal_error"}), file=sys.stderr if args.command == 'serve' else sys.stdout)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
