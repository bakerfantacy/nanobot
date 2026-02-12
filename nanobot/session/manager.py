"""Session management for conversation history."""

import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.
    
    Stores messages in JSONL format for easy reading and persistence.
    """
    
    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def add_message(
        self,
        role: str,
        content: str,
        sender_type: str | None = None,
        sender: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Add a message to the session.

        Args:
            role: "user" or "assistant".
            content: Message content.
            sender_type: "human" | "bot" | None (None treated as human for backward compat).
            sender: When sender_type="bot", the agent name or open_id.
        """
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        if sender_type is not None:
            msg["sender_type"] = sender_type
        if sender is not None:
            msg["sender"] = sender
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def count_trailing_bots(self, max_scan: int = 30) -> int:
        """
        Count consecutive bot messages at the end of the session.
        Used for bot-to-bot depth calculation.
        Counts: assistant (self) + user with sender_type=="bot".
        Stops at user with sender_type != "bot" (or missing, treated as human).
        """
        recent = self.messages[-max_scan:] if len(self.messages) > max_scan else self.messages
        count = 0
        for m in reversed(recent):
            role = m.get("role", "")
            sender_type = m.get("sender_type", "human")
            if role == "assistant":
                count += 1
            elif role == "user" and sender_type == "bot":
                count += 1
            else:
                break
        return count

    def get_recent_for_prompt(self, max_messages: int = 20) -> list[dict[str, Any]]:
        """
        Get recent messages in transcript-like format for prompts.
        Returns [{role, content, sender}] compatible with transcript.get_recent.
        assistant (self) -> role=assistant, sender="self"
        user+sender_type=bot -> role=assistant, sender=agent_name
        user+human/system -> role=user, sender=""
        """
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        result: list[dict[str, Any]] = []
        for m in recent:
            role = m.get("role", "")
            content = m.get("content", "")
            sender_type = m.get("sender_type", "human")
            sender = m.get("sender", "")
            if role == "assistant":
                result.append({"role": "assistant", "content": content, "sender": "self"})
            elif role == "user" and sender_type == "bot":
                result.append({"role": "assistant", "content": content, "sender": sender})
            else:
                result.append({"role": "user", "content": content, "sender": ""})
        return result

    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.
        
        Args:
            max_messages: Maximum messages to return.
        
        Returns:
            List of messages in LLM format.
        """
        # Get recent messages
        recent = self.messages[-max_messages:] if len(self.messages) > max_messages else self.messages
        
        # Convert to LLM format (just role and content)
        return [{"role": m["role"], "content": m["content"]} for m in recent]
    
    def clear(self) -> None:
        """Clear all messages in the session."""
        self.messages = []
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.
    
    Sessions are stored as JSONL files in the sessions directory.
    """
    
    def __init__(self, workspace: Path, sessions_dir: Path | None = None):
        self.workspace = workspace
        # Use explicit sessions_dir when provided (per-agent);
        # otherwise fall back to a 'sessions' folder next to the workspace.
        self.sessions_dir = ensure_dir(
            sessions_dir or (workspace.parent / "sessions")
        )
        self._cache: dict[str, Session] = {}
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        # Check cache
        if key in self._cache:
            return self._cache[key]
        
        # Try to load from disk
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        
        self._cache[key] = session
        return session
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        
        if not path.exists():
            return None
        
        try:
            messages = []
            metadata = {}
            created_at = None
            
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    data = json.loads(line)
                    
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                    else:
                        messages.append(data)
            
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None
    
    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)
        
        with open(path, "w") as f:
            # Write metadata first
            metadata_line = {
                "_type": "metadata",
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata
            }
            f.write(json.dumps(metadata_line) + "\n")
            
            # Write messages
            for msg in session.messages:
                f.write(json.dumps(msg) + "\n")
        
        self._cache[session.key] = session
    
    def delete(self, key: str) -> bool:
        """
        Delete a session.
        
        Args:
            key: Session key.
        
        Returns:
            True if deleted, False if not found.
        """
        # Remove from cache
        self._cache.pop(key, None)
        
        # Remove file
        path = self._get_session_path(key)
        if path.exists():
            path.unlink()
            return True
        return False
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
