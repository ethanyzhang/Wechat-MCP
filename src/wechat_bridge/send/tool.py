"""Registers send_message on the MCP server. Imported only when the server starts with --enable-send."""
import inspect
import threading
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.errors import BridgeError
from .log import DEFAULT_DIR, SendLog, send_lock
from .sender import MAX_TEXT_CHARACTERS, Sender

ConversationId = Annotated[str, Field(strict=True, pattern=r'^conversation_[0-9a-f]{64}$')]
RecipientName = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
MessageText = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_TEXT_CHARACTERS)]


log_directory = DEFAULT_DIR   # tests point this at a temporary directory


def register_send(server, allowed_arguments, service, config, invoke, driver=None):
    """Add `send_message`. Annotated destructive and open-world so a client's own permission prompt appears on every call.

    The query repository is opened on the first call, inside the error boundary, so an unreadable source never stops the
    server (and its `status` tool) from starting.
    """
    if driver is None:
        from .macos import MacDriver
        driver = MacDriver(config)
        threading.Thread(target=driver.prepare, name='wechat-ui-helper-build', daemon=True).start()
    holder = {}

    def sender():
        if 'sender' not in holder:
            repository = service.queries
            holder['sender'] = Sender(driver, SendLog(log_directory, repository.identity_key), repository.resolve_recipient,
                                      lock=lambda: send_lock(log_directory))
        return holder['sender']

    annotations = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

    def send_message(conversation_id: ConversationId, recipient_name: RecipientName, text: MessageText):
        """Send one text message to a chat through the open WeChat app. recipient_name must equal the conversation's full display name. Text only; after `unconfirmed` never resend."""
        return invoke(lambda: sender().send(conversation_id, recipient_name, text))

    allowed_arguments['send_message'] = set(inspect.signature(send_message).parameters)
    server.tool(annotations=annotations, structured_output=False)(send_message)
