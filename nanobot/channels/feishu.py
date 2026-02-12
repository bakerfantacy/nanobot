"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import re
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanobot.transcript.store import GroupTranscriptStore

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig

try:
    import lark_oapi as lark  # pyright: ignore[reportMissingImports]
    from lark_oapi.api.im.v1 import (  # pyright: ignore[reportMissingImports]
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.
    
    Uses WebSocket to receive events - no public IP or webhook required.
    
    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(
        self,
        config: FeishuConfig,
        bus: MessageBus,
        transcript_store: "GroupTranscriptStore | None" = None,
    ):
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._transcript_store = transcript_store
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot_open_id: str | None = None  # This bot's own open_id (fetched at startup)
        self._started_at_ms: float = 0  # Agent start time (ms), used to ignore replayed historical events
        self._group_members: list = []  # GroupMember list from shared groups.json
        self._name_to_open_id: dict[str, str] = {}  # display_name → open_id (for outbound @mention)
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # Fetch this bot's own open_id for self-message detection and @mention matching
        self._bot_open_id = await self._fetch_bot_open_id()
        if self._bot_open_id:
            logger.info(f"Feishu bot open_id: {self._bot_open_id}")
        else:
            logger.warning("Could not fetch bot open_id; self-skip and @mention detection disabled")

        # Load shared group member registry from ~/.nanobot/groups.json
        self._load_group_members()

        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread
        def run_ws():
            try:
                self._ws_client.start()
            except Exception as e:
                logger.error(f"Feishu WebSocket error: {e}")
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        # Record start time to ignore replayed historical events (Feishu may push old messages on connect)
        self._started_at_ms = time.time() * 1000
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        while self._running:
            await asyncio.sleep(1)

    @property
    def bot_open_id(self) -> str | None:
        """This bot's Feishu open_id (for relay publish)."""
        return self._bot_open_id

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")

    async def _fetch_bot_open_id(self, retries: int = 3, delay: float = 2.0) -> str | None:
        """
        Fetch this bot's own open_id via GET /open-apis/bot/v3/info.

        Obtains a tenant_access_token first, then queries the bot info API.
        Retries on failure.

        Returns:
            The bot's open_id, or None on failure.
        """
        import httpx

        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    # Step 1: get tenant_access_token
                    token_resp = await http.post(
                        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                    )
                    token_data = token_resp.json()
                    token = token_data.get("tenant_access_token")
                    if not token:
                        logger.warning(
                            f"[attempt {attempt}/{retries}] Could not obtain tenant_access_token: "
                            f"{token_data}"
                        )
                        if attempt < retries:
                            await asyncio.sleep(delay)
                        continue

                    # Step 2: get bot info
                    resp = await http.get(
                        "https://open.feishu.cn/open-apis/bot/v3/info",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        open_id = data.get("bot", {}).get("open_id")
                        if open_id:
                            return open_id
                        logger.warning(f"[attempt {attempt}/{retries}] Bot info response missing open_id: {data}")
                    else:
                        logger.warning(
                            f"[attempt {attempt}/{retries}] Fetch bot info failed: "
                            f"status={resp.status_code}, body={resp.text[:200]}"
                        )
            except Exception as e:
                logger.warning(f"[attempt {attempt}/{retries}] Error fetching bot info: {e}")

            if attempt < retries:
                await asyncio.sleep(delay)

        return None

    def _load_group_members(self) -> None:
        """Load group members from ~/.nanobot/groups.json."""
        try:
            from nanobot.config.loader import load_groups
            members = load_groups()
            self._group_members = members
            # Build name → open_id mapping, excluding self
            self._name_to_open_id = {
                m.name: m.feishu_open_id
                for m in members
                if m.feishu_open_id and m.feishu_open_id != self._bot_open_id
            }
            if members:
                logger.info(
                    f"Loaded {len(members)} group members from groups.json: "
                    f"{', '.join(m.name for m in members)}"
                )
        except Exception as e:
            logger.warning(f"Failed to load group members: {e}")

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})
        return elements or [{"tag": "markdown", "content": content}]

    def _resolve_outbound_mentions(self, text: str) -> str:
        """
        Convert @Name patterns in outgoing text to Feishu's interactive card
        @mention syntax: ``<at id=ou_xxx></at>``.

        Resolves names from the shared groups.json registry.
        """
        if not self._name_to_open_id:
            return text
        for display_name, open_id in self._name_to_open_id.items():
            pattern = re.compile(rf"@{re.escape(display_name)}", re.IGNORECASE)
            text = pattern.sub(f"<at id={open_id}></at>", text)
        return text

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return
        
        try:
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            if msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            # Resolve @BotName → Feishu <at> syntax before building card
            resolved_content = self._resolve_outbound_mentions(msg.content)

            # Build card with markdown + table support
            elements = self._build_card_elements(resolved_content)
            card = {
                "config": {"wide_screen_mode": True},
                "elements": elements,
            }
            content = json.dumps(card, ensure_ascii=False)
            
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(msg.chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .build()
                ).build()
            
            response = self._client.im.v1.message.create(request)
            
            if not response.success():
                logger.error(
                    f"Failed to send Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            else:
                logger.debug(f"Feishu message sent to {msg.chat_id}")
                
        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    # Regex to strip @mention placeholders like @_user_1 from text
    _MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu with multi-agent routing."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"

            # ----------------------------------------------------------
            # Self-sent check: skip messages from this bot itself
            # ----------------------------------------------------------
            if self._bot_open_id and sender_id == self._bot_open_id:
                logger.debug(f"Skipping self-sent message {message_id}")
                return

            # ----------------------------------------------------------
            # Ignore replayed historical events (Feishu may push old messages when WebSocket connects)
            # ----------------------------------------------------------
            create_time = getattr(message, "create_time", None) or getattr(event, "create_time", None)
            if create_time is not None and self._started_at_ms > 0:
                try:
                    msg_ts = int(create_time) if isinstance(create_time, str) else create_time
                    # Allow 60s buffer for clock skew; only process messages created after agent start
                    if msg_ts < self._started_at_ms - 60_000:
                        logger.debug(
                            f"Skipping replayed historical message {message_id} "
                            f"(create_time={msg_ts}, started_at={self._started_at_ms:.0f})"
                        )
                        return
                except (ValueError, TypeError):
                    pass

            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type

            # ----------------------------------------------------------
            # Parse @mentions
            # ----------------------------------------------------------
            mentions = getattr(message, "mentions", None) or []
            mentioned_ids: set[str] = set()
            mention_names: dict[str, str] = {}  # placeholder → display name
            for m in mentions:
                # m.id is typically a UserId object with .open_id; fall back to str
                m_id_obj = getattr(m, "id", None)
                open_id = getattr(m_id_obj, "open_id", None) if m_id_obj else None
                if not open_id and isinstance(m_id_obj, str):
                    open_id = m_id_obj
                if open_id:
                    mentioned_ids.add(open_id)
                key = getattr(m, "key", "")
                name = getattr(m, "name", "")
                if key and name:
                    mention_names[key] = name

            is_mentioned = bool(self._bot_open_id and self._bot_open_id in mentioned_ids)

            # ----------------------------------------------------------
            # Group routing (applies to all senders: user or other bot)
            # ----------------------------------------------------------
            if chat_type == "group" and not is_mentioned:
                policy = self.config.group_policy
                if policy == "mention":
                    # Strict mention-only mode: skip non-mentioned messages
                    logger.debug(
                        f"Skipping non-mentioned group message {message_id} (policy=mention)"
                    )
                    return
                # "auto" and "open" pass through; "auto" will be checked by AgentLoop

            # Add reaction to indicate "seen" (only for messages we will process)
            await self._add_reaction(message_id, "THUMBSUP")

            # ----------------------------------------------------------
            # Parse message content
            # ----------------------------------------------------------
            if msg_type == "text":
                try:
                    content = json.loads(message.content).get("text", "")
                except json.JSONDecodeError:
                    content = message.content or ""
            else:
                content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

            if not content:
                return

            # Clean @mention placeholders from text, replace with display names
            for placeholder, display_name in mention_names.items():
                content = content.replace(placeholder, f"@{display_name}")
            # Remove any remaining unresolved placeholders
            content = self._MENTION_PLACEHOLDER_RE.sub("", content).strip()

            if not content:
                return

            # Append to shared transcript (for multi-agent context)
            if chat_type == "group" and self._transcript_store:
                session_key = f"feishu:{chat_id}"
                try:
                    self._transcript_store.append(
                        session_key,
                        role="user",
                        content=content,
                        sender=sender_id,
                        message_id=message_id,
                    )
                except Exception as e:
                    logger.debug(f"Failed to append inbound to transcript: {e}")

            # ----------------------------------------------------------
            # Forward to message bus with routing metadata
            # ----------------------------------------------------------
            reply_to = chat_id if chat_type == "group" else sender_id
            metadata: dict[str, Any] = {
                "message_id": message_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
                "is_mentioned": is_mentioned,
                "group_policy": self.config.group_policy,
            }
            # Pass group member registry so the agent knows its peers
            if self._group_members:
                metadata["group_members"] = [
                    {"name": m.name, "type": m.type, "description": m.description}
                    for m in self._group_members
                    if m.feishu_open_id != self._bot_open_id  # exclude self
                ]

            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                metadata=metadata,
            )

        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")
