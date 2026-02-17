"""Relay subscriber: receives relay messages and injects into local bus."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.relay.backend import ProcessedRelayStore

if TYPE_CHECKING:
    from nanobot.relay.backend import GroupMessageRelay
    from nanobot.transcript.store import GroupTranscriptStore


class RelaySubscriber:
    """
    Subscribes to relay and injects received messages into the local bus.
    Skips self-sent messages; appends to transcript before inject; deduplicates.
    """

    def __init__(
        self,
        relay: "GroupMessageRelay",
        bus: MessageBus,
        transcript_store: "GroupTranscriptStore",
        bot_open_id: str | None,
        agent_name: str,
        get_bot_open_id: Callable[[], str | None] | None = None,
    ):
        self.relay = relay
        self.bus = bus
        self.transcript_store = transcript_store
        self._bot_open_id = bot_open_id or ""
        self.get_bot_open_id = get_bot_open_id
        self.agent_name = agent_name
        self.processed = ProcessedRelayStore()
        self._running = False

    def _current_bot_open_id(self) -> str:
        """Get current bot open_id (static or from getter)."""
        if self.get_bot_open_id:
            v = self.get_bot_open_id()
            return v or ""
        return self._bot_open_id or ""

    def _compute_is_mentioned(self, content: str) -> bool:
        """Check if this agent is @mentioned in content."""
        try:
            from nanobot.config.loader import load_groups
            members = load_groups()
            bot_id = self._current_bot_open_id()
            for m in members:
                if m.feishu_open_id == bot_id:
                    name = m.name
                    if name and f"@{name}" in content:
                        return True
                    if bot_id and f"<at id={bot_id}" in content:
                        return True
                    break
        except Exception:
            pass
        return False

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        """Process one relay message: dedup, append transcript, inject."""
        relay_msg_id = payload.get("relay_msg_id") or ""
        if not relay_msg_id:
            return
        if self.processed.contains(relay_msg_id):
            return
        sender_bot_open_id = payload.get("sender_bot_open_id") or ""
        if sender_bot_open_id and sender_bot_open_id == self._current_bot_open_id():
            return  # skip self

        self.processed.add(relay_msg_id)

        channel = payload.get("channel") or "feishu"
        chat_id = payload.get("chat_id") or ""
        content = payload.get("content") or ""
        sender_agent_name = payload.get("sender_agent_name") or "unknown"
        metadata = dict(payload.get("metadata") or {})

        session_key = f"{channel}:{chat_id}"

        try:
            self.transcript_store.append(
                session_key,
                role="assistant",
                content=content,
                sender=sender_agent_name,
            )
        except Exception as e:
            logger.debug(f"Relay: failed to append transcript: {e}")

        metadata["from_bot"] = True
        metadata["sender_agent_name"] = sender_agent_name
        metadata["chat_type"] = metadata.get("chat_type") or "group"
        # Always derive is_mentioned from this message's content only. Do not trust
        # payload metadata (it may be copied from the original user message when
        # the sending bot replied, so e.g. BotB would see is_mentioned=True from
        # the user's @ even though the sending bot's reply did not @ BotB).
        metadata["is_mentioned"] = self._compute_is_mentioned(content)
        if "group_policy" not in metadata:
            metadata["group_policy"] = "auto"

        try:
            from nanobot.config.loader import load_groups
            members = load_groups()
            metadata["group_members"] = [
                {"name": m.name, "type": m.type, "description": m.description}
                for m in members
                if m.feishu_open_id and m.feishu_open_id != self._current_bot_open_id()
            ]
        except Exception:
            pass

        reply_to = chat_id if metadata.get("chat_type") == "group" else sender_bot_open_id
        msg = InboundMessage(
            channel=channel,
            sender_id=sender_bot_open_id,
            chat_id=reply_to,
            content=content,
            metadata=metadata,
        )
        await self.bus.publish_inbound(msg)
        logger.debug(f"Relay: injected message from {sender_agent_name} to {session_key}")

    async def run(self, poll_interval: float = 0.5) -> None:
        """Poll for new relay messages and inject. Runs until stop()."""
        self._running = True
        while self._running:
            try:
                messages = self.relay.read_new_messages(self.agent_name)
                for payload in messages:
                    await self._handle_message(payload)
            except Exception as e:
                logger.debug(f"Relay subscriber error: {e}")
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        """Stop the subscriber loop."""
        self._running = False
