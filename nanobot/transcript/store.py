"""Shared group transcript store for multi-agent scenarios."""

import json
import time
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir, get_nanobot_home, safe_filename


class GroupTranscriptStore:
    """
    Store group chat transcripts for multi-agent message sharing.

    Enables agents in different processes to read the full conversation
    (including other agents' replies) for relevance checks and context.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = ensure_dir(base_dir or (get_nanobot_home() / "transcripts"))

    def _get_path(self, session_key: str) -> Path:
        """Get file path for a session key."""
        safe_key = safe_filename(session_key.replace(":", "_"))
        return self.base_dir / f"{safe_key}.jsonl"

    def append(
        self,
        session_key: str,
        role: str,
        content: str,
        sender: str,
        message_id: str | None = None,
        timestamp_ms: float | None = None,
    ) -> None:
        """
        Append a message to the group transcript.

        Args:
            session_key: Channel:chat_id (e.g. feishu:oc_xxx).
            role: "user" or "assistant".
            content: Message content.
            sender: Sender identifier (user_id or agent_name).
            message_id: Optional message ID for deduplication (inbound only).
            timestamp_ms: Optional timestamp in milliseconds.
        """
        ts = timestamp_ms if timestamp_ms is not None else time.time() * 1000
        entry: dict[str, Any] = {
            "role": role,
            "content": content,
            "sender": sender,
            "ts": ts,
        }
        if message_id is not None:
            entry["message_id"] = message_id

        path = self._get_path(session_key)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_recent(self, session_key: str, max_messages: int = 20) -> list[dict[str, Any]]:
        """
        Get recent messages from the group transcript.

        Deduplicates by message_id (for user messages that may be appended
        by multiple processes). Returns format compatible with session history:
        [{role, content, sender?}].

        Args:
            session_key: Channel:chat_id.
            max_messages: Maximum number of messages to return.

        Returns:
            List of message dicts, newest last.
        """
        path = self._get_path(session_key)
        if not path.exists():
            return []

        lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, json.JSONDecodeError):
            return []

        parsed: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = entry.get("message_id")
            if msg_id and msg_id in seen_message_ids:
                continue
            if msg_id:
                seen_message_ids.add(msg_id)
            parsed.append(entry)

        recent = sorted(parsed, key=lambda x: x.get("ts", 0))[-max_messages:]
        return [
            {"role": m["role"], "content": m["content"], "sender": m.get("sender", "")}
            for m in recent
        ]

    def count_trailing_assistants(self, session_key: str, max_scan: int = 30) -> int:
        """
        Count consecutive assistant messages at the end of the transcript.
        Used for bot-to-bot depth calculation.
        """
        recent = self.get_recent(session_key, max_scan)
        count = 0
        for m in reversed(recent):
            if m.get("role") == "assistant":
                count += 1
            else:
                break
        return count
