"""Registers send_message on the MCP server. Imported only when the server starts with --enable-send."""
import inspect
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.errors import BridgeError
from .log import SendLog
from .sender import MAX_TEXT_CHARACTERS, Sender

ConversationId = Annotated[str, Field(strict=True, pattern=r'^conversation_[0-9a-f]{64}$')]
RecipientName = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
MessageText = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_TEXT_CHARACTERS)]


def register_send(server, allowed_arguments, service, config, invoke, driver=None):
    """Add `send_message`. Annotated destructive and open-world so a client's own permission prompt appears on every call."""
    if driver is None:
        from .macos import MacDriver
        driver = MacDriver(config)
    repository = service.queries
    sender = Sender(driver, SendLog(config.state_dir, repository.identity_key), repository.resolve_recipient)
    annotations = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

    def send_message(conversation_id: ConversationId, recipient_name: RecipientName, text: MessageText):
        """Send one text message to a chat through the open WeChat app. recipient_name must equal the conversation's full display name. Text only; after `unconfirmed` never resend."""
        return invoke(lambda: sender.send(conversation_id, recipient_name, text))

    allowed_arguments['send_message'] = set(inspect.signature(send_message).parameters)
    server.tool(annotations=annotations, structured_output=False)(send_message)
    return sender
