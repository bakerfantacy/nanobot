"""File-based relay backend for cross-process message distribution."""

import json
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir, get_nanobot_home


class ProcessedRelayStore:
    """In-memory store of processed relay message IDs to avoid duplicate consumption."""

    def __init__(self, max_size: int = 5000) -> None:
        self._store: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def add(self, relay_msg_id: str) -> None:
        self._store[relay_msg_id] = None
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def contains(self, relay_msg_id: str) -> bool:
        return relay_msg_id in self._store


class GroupMessageRelay:
    """
    Relay for distributing bot messages to other agent processes.

    Uses file-based backend (~/.nanobot/relay/outbound.jsonl) when Redis
    is not configured. Each line is a JSON payload.
    """

    CHANNEL = "nanobot:feishu:outbound"

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = ensure_dir(base_dir or (get_nanobot_home() / "relay"))
        self.outbound_path = self.base_dir / "outbound.jsonl"

    def publish(
        self,
        channel: str,
        chat_id: str,
        content: str,
        sender_bot_open_id: str,
        sender_agent_name: str,
        metadata: dict[str, Any],
    ) -> None:
        """Publish a bot message for other agents to receive."""
        relay_msg_id = f"{sender_bot_open_id}:{chat_id}:{int(time.time()*1000)}:{uuid.uuid4().hex[:12]}"
        payload = {
            "relay_msg_id": relay_msg_id,
            "channel": channel,
            "chat_id": chat_id,
            "content": content,
            "sender_bot_open_id": sender_bot_open_id,
            "sender_agent_name": sender_agent_name,
            "metadata": dict(metadata) if metadata else {},
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self.outbound_path, "a", encoding="utf-8") as f:
            f.write(line)

    def read_new_messages(self, agent_name: str) -> list[dict[str, Any]]:
        """
        Read new messages from the outbound file since last read.
        Tracks position per agent in ~/.nanobot/relay/offsets/
        """
        offset_file = self.base_dir / "offsets" / f"{agent_name}.txt"
        ensure_dir(offset_file.parent)
        last_offset = 0
        if offset_file.exists():
            try:
                last_offset = int(offset_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass

        result: list[dict[str, Any]] = []
        try:
            if not self.outbound_path.exists():
                return result
            with open(self.outbound_path, "r", encoding="utf-8") as f:
                f.seek(last_offset)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            result.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                last_offset = f.tell()
            offset_file.write_text(str(last_offset), encoding="utf-8")
        except OSError:
            pass
        return result
