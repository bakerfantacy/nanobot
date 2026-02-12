"""Message routing and scenario-specific prompt injection.

This module isolates scenario-specific logic (group chat, bot-to-bot, etc.)
away from the core agent loop and context builder.  Each scenario is
encapsulated in a :class:`ResponseFilter` subclass that can independently:

1. **Gate** messages — decide whether the agent should respond.
2. **Enrich** the system prompt — inject scenario-specific instructions
   (e.g. group member list, @mention rules) that the core modules don't
   need to know about.

Architecture
------------
MessageRouter
  └── ResponseFilter (chain)
        ├── GroupChatFilter   – group chat @mention / policy / LLM relevance
        └── (future filters)  – e.g. rate-limit, DND, content-type …
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session
from nanobot.providers.base import LLMProvider

# -----------------------------------------------------------------------
# Prompt templates – group chat
#
# All group-chat-specific prompt text is maintained here so that the core
# context builder and agent loop remain scenario-agnostic.
# -----------------------------------------------------------------------

# -- system-prompt additions (injected into the main LLM call) ----------

_GROUP_MEMBERS_HEADER = "## Group Chat Members"

_GROUP_MENTION_INSTRUCTIONS = (
    "To @mention someone, write @name in your response{mention_hint}. "
    "Don't mention other bots if you are responding to a message from user, "
    "unless user allows you to do so. "
    "The system will convert it to a proper @mention automatically."
)

# -- routing LLM (lightweight yes/no call) ------------------------------

_GROUP_ROUTING_RULES = (
    "If from another BOT: NO for acknowledgments (OK/thanks), redundant, done. "
    "YES for substantive question, task needing you. "
    "If from a USER not @you: NO unless you were recently involved "
    "(follow-up like 继续) or it clearly targets your expertise. "
    "YES if recent follow-up or clear new request for you."
)

_GROUP_ROUTING_PROMPT = (
    "You are: {self_desc}\n"
    "{peers_desc}\n\n"
    "{sender_hint} said: \"{msg_preview}\"\n\n"
    "{history_blurb}"
    "Rules: {rules}\n\n"
    "Reply with ONLY 'YES' or 'NO'."
)


# -----------------------------------------------------------------------
# Base class
# -----------------------------------------------------------------------

class ResponseFilter(abc.ABC):
    """Base class for scenario-specific message filters.

    Subclasses implement two hooks:

    * :meth:`should_respond` – gate (respond / skip / defer).
    * :meth:`build_prompt_extras` – contribute extra text to the system
      prompt so the LLM is aware of scenario-specific context.
    """

    @abc.abstractmethod
    async def should_respond(
        self, msg: InboundMessage, session: Session | None
    ) -> bool | None:
        """Decide whether the agent should respond.

        Returns
        -------
        bool | None
            * ``True``  – the agent **should** respond.
            * ``False`` – the agent should **skip** this message.
            * ``None``  – this filter has no opinion; defer to the next one.
        """
        ...

    def build_prompt_extras(
        self, msg: InboundMessage, session: Session | None
    ) -> str | None:
        """Return extra text to append to the system prompt.

        Called **after** routing succeeds (i.e. the agent will respond).
        Return ``None`` (the default) if there is nothing to add.
        """
        return None


# -----------------------------------------------------------------------
# Group chat filter
# -----------------------------------------------------------------------

class GroupChatFilter(ResponseFilter):
    """Routing filter for **group chat** scenarios.

    Handles:
    * ``group_policy`` (``mention`` / ``auto`` / ``open``)
    * Bot-to-bot depth limiting
    * LLM-based relevance judgment when needed

    Non-group messages are ignored (returns ``None`` so the next filter or
    default behaviour applies).
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        workspace: Path,
        *,
        max_bot_reply_depth: int = 8,
        bot_reply_llm_threshold: int = 3,
        bot_reply_llm_check: bool = True,
    ) -> None:
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.max_bot_reply_depth = max_bot_reply_depth
        self.bot_reply_llm_threshold = bot_reply_llm_threshold
        self.bot_reply_llm_check = bot_reply_llm_check

    # -- prompt extras ---------------------------------------------------

    def build_prompt_extras(
        self, msg: InboundMessage, session: Session | None
    ) -> str | None:
        """Inject group member list and @mention instructions into the system prompt."""
        meta = msg.metadata or {}
        if meta.get("chat_type") != "group":
            return None

        group_members: list[dict[str, Any]] = meta.get("group_members", [])
        if not group_members:
            return None

        member_lines: list[str] = []
        first_bot_name: str | None = None
        for m in group_members:
            name = m.get("name", "")
            mtype = m.get("type", "bot")
            desc = m.get("description", "")
            label = f"@{name}"
            if mtype == "bot":
                label += " (bot)"
                if not first_bot_name:
                    first_bot_name = name
            if desc:
                label += f" - {desc}"
            member_lines.append(f"- {label}")

        members_text = "\n".join(member_lines)
        mention_hint = f" (e.g. @{first_bot_name})" if first_bot_name else ""

        return (
            f"\n\n{_GROUP_MEMBERS_HEADER}\n"
            f"Other members in this group chat:\n{members_text}\n\n"
            + _GROUP_MENTION_INSTRUCTIONS.format(mention_hint=mention_hint)
        )

    # -- routing ---------------------------------------------------------

    async def should_respond(
        self, msg: InboundMessage, session: Session | None
    ) -> bool | None:
        meta = msg.metadata or {}

        # Only applicable to group messages
        if meta.get("chat_type") != "group":
            return None  # not our concern – defer

        from_bot = meta.get("from_bot", False)
        policy = meta.get("group_policy", "open")

        logger.debug("GroupChatFilter: evaluating rules …")
        logger.debug(f"  from_bot={from_bot}, policy={policy}")

        if from_bot:
            depth = session.count_trailing_bots() + 1 if session else 1
            logger.debug(f"  bot depth={depth}")
            if depth >= self.max_bot_reply_depth:
                logger.debug(
                    f"  Skipping: depth {depth} >= max {self.max_bot_reply_depth}"
                )
                return False
            if not meta.get("is_mentioned", False):
                return False
            if (
                meta.get("is_mentioned", True)
                and depth <= self.bot_reply_llm_threshold
                or not self.bot_reply_llm_check
            ):
                return True  # within threshold – no LLM needed
        else:
            if policy == "open" or meta.get("is_mentioned", True):
                return True

        # Fall through → LLM judgment
        logger.debug("GroupChatFilter: deferring to LLM …")
        return await self._llm_should_respond(msg, session, from_bot=from_bot)

    # -- LLM relevance check ---------------------------------------------

    async def _llm_should_respond(
        self,
        msg: InboundMessage,
        session: Session | None,
        from_bot: bool,
    ) -> bool:
        """Single LLM call for bot-to-bot control + relevance judgment."""
        meta = msg.metadata or {}
        group_members: list[dict[str, Any]] = meta.get("group_members", [])

        self_desc = self._build_self_description(group_members)
        peers_desc = self._build_peers_description(group_members)
        history_blurb = self._build_history_blurb(session)

        msg_preview = msg.content[:300]
        sender_hint = (
            "Another bot" if from_bot else "A user (did NOT @mention you)"
        )
        default_respond = not from_bot  # bot → conservative no; user → yes

        prompt = _GROUP_ROUTING_PROMPT.format(
            self_desc=self_desc,
            peers_desc=peers_desc,
            sender_hint=sender_hint,
            msg_preview=msg_preview,
            history_blurb=history_blurb,
            rules=_GROUP_ROUTING_RULES,
        )

        try:
            response = await self.provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=self.model,
                max_tokens=64,
                temperature=0.0,
            )
            content = (response.content or "").strip()
            reasoning = (
                getattr(response, "reasoning_content", None) or ""
            ).strip()
            combined = f"{reasoning}\n{content}".strip() or content or reasoning
            answer = combined.upper()
            if not answer:
                return default_respond
            should = "YES" in answer and (
                "NO" not in answer or answer.rfind("YES") > answer.rfind("NO")
            )
            logger.debug(
                f"LLM should_respond (from_bot={from_bot}): "
                f"→ {answer[:60]} → {should}"
            )
            return should
        except Exception as e:
            logger.warning(
                f"LLM should_respond failed: {e}, default={default_respond}"
            )
            return default_respond

    # -- helpers ----------------------------------------------------------

    def _build_self_description(
        self, group_members: list[dict[str, Any]]
    ) -> str:
        """Build a description of *this* agent for the LLM prompt."""
        from nanobot.config.loader import load_groups

        all_members = load_groups()
        other_names = {m.get("name", "") for m in group_members}
        for m in all_members:
            if m.name not in other_names and m.type == "bot":
                return (
                    f"{m.name}: {m.description}" if m.description else m.name
                )

        # Fallback: read workspace agent files
        parts: list[str] = []
        for filename in ("AGENTS.md", "SOUL.md"):
            path = self.workspace / filename
            if path.exists():
                try:
                    parts.append(path.read_text(encoding="utf-8")[:300])
                except Exception:
                    pass
        return "\n".join(parts) if parts else "a helpful AI assistant"

    @staticmethod
    def _build_peers_description(
        group_members: list[dict[str, Any]],
    ) -> str:
        """Build a description of other group members for the LLM prompt."""
        if not group_members:
            return ""
        lines: list[str] = []
        for m in group_members:
            name = m.get("name", "")
            mtype = m.get("type", "bot")
            desc = m.get("description", "")
            entry = f"- {name} ({mtype})"
            if desc:
                entry += f": {desc}"
            lines.append(entry)
        return "\nOther members in this group:\n" + "\n".join(lines)

    @staticmethod
    def _build_history_blurb(session: Session | None) -> str:
        """Build a recent-history snippet for the LLM prompt."""
        if not session:
            return ""
        recent = session.get_recent_for_prompt(20)
        if not recent:
            return ""
        lines: list[str] = []
        for x in recent[-8:]:
            role = x.get("role", "?")
            content = (x.get("content") or "")[:100]
            sender = x.get("sender", "")
            label = f"{role}" + (f" ({sender})" if sender else "")
            lines.append(f"  {label}: {content}")
        return "\nRecent:\n" + "\n".join(lines) + "\n\n"


# -----------------------------------------------------------------------
# Router
# -----------------------------------------------------------------------

class MessageRouter:
    """Chains :class:`ResponseFilter` instances to reach a respond/skip decision.

    Filters are evaluated **in order**.  The first filter that returns a
    definitive ``True`` or ``False`` wins.  If every filter returns ``None``
    the router defaults to **respond** (``True``).
    """

    def __init__(self) -> None:
        self._filters: list[ResponseFilter] = []

    def add_filter(self, f: ResponseFilter) -> None:
        """Append a filter to the chain."""
        self._filters.append(f)

    async def should_respond(
        self, msg: InboundMessage, session: Session | None = None
    ) -> bool:
        for f in self._filters:
            result = await f.should_respond(msg, session)
            if result is not None:
                return result
        return True  # default: respond

    def collect_prompt_extras(
        self, msg: InboundMessage, session: Session | None = None
    ) -> list[str]:
        """Gather system-prompt additions from all filters.

        Called after :meth:`should_respond` returns ``True`` so each filter
        can inject scenario-specific instructions into the main LLM call.
        """
        extras: list[str] = []
        for f in self._filters:
            extra = f.build_prompt_extras(msg, session)
            if extra:
                extras.append(extra)
        return extras
