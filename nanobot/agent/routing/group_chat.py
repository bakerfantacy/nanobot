"""Group-chat routing filter and prompt injection.

All group-chat-specific prompt text and routing logic lives in this file
so that the core context builder and agent loop remain scenario-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session

from nanobot.agent.routing.base import ResponseFilter

# -----------------------------------------------------------------------
# Prompt templates – group chat
# -----------------------------------------------------------------------

# -- system-prompt additions (injected into the main LLM call) ----------

_GROUP_MEMBERS_HEADER = "## Group Chat Members"

# Mention rules injected into the system prompt.
# Keyed by message source so the restriction strength can vary.

_MENTION_RULES_FROM_USER = (
    "**When the message @mentions multiple bots (including you), "
    "ONLY respond to the part directed at YOU.** "
    "Ignore instructions and questions meant for other bots entirely — "
    "do not answer them, summarize them, or reference them in your response.\n\n"
    "**Do NOT @mention other bots in your response** unless ALL of the following are true:\n"
    "1. You need another bot to **execute a task** that you cannot do yourself.\n"
    "2. Your **next step depends on** the result of that task.\n"
    "3. There is no other way to obtain the result.\n\n"
    "If you are unsure, do NOT @mention. Specifically:\n"
    "- Do not @mention a bot just to ask its opinion or for general help.\n"
    "- Do not answer on behalf of another bot, even if you know the answer.\n"
    "- If the user's question involves another bot's expertise, "
    "let the user decide whether to ask them.\n\n"
    "Mention syntax: write @name in your response{mention_hint}. "
    "The system will convert it to a proper @mention automatically."
)

_MENTION_RULES_FROM_BOT = (
    "You are replying to another bot. Keep your response focused on the task.\n"
    "- Do NOT @mention additional bots unless the requesting bot explicitly "
    "asked you to relay results to a specific bot by name.\n"
    "- Avoid chain-summoning: if you can answer directly, just answer.\n\n"
    "Mention syntax: write @name in your response{mention_hint}. "
    "The system will convert it to a proper @mention automatically."
)

# -- user-message reminder (highest-attention position) -----------------

_USER_REMINDER_GROUP = (
    "[System] This is a group chat. "
    "ONLY answer the part directed at you. "
    "Do NOT answer for other bots. "
    "Do NOT @mention other bots unless you need one to execute a task "
    "and your next step depends on its result."
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
# GroupChatFilter
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

    # -- user-message reminder -------------------------------------------

    def build_user_reminder(
        self, msg: InboundMessage, session: Session | None
    ) -> str | None:
        """Short reminder prepended to user message for maximum salience."""
        meta = msg.metadata or {}
        if meta.get("chat_type") != "group":
            return None
        return _USER_REMINDER_GROUP

    # -- prompt extras ---------------------------------------------------

    def build_prompt_extras(
        self, msg: InboundMessage, session: Session | None
    ) -> str | None:
        """Inject group member list and @mention rules into the system prompt.

        The mention rules are tailored to the message source:
        - From a human user → strict prohibition on @mentioning bots.
        - From another bot  → limited, task-focused mention policy.
        """
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

        # Pick mention rules based on message source
        from_bot = meta.get("from_bot", False)
        rules_template = _MENTION_RULES_FROM_BOT if from_bot else _MENTION_RULES_FROM_USER
        mention_rules = rules_template.format(mention_hint=mention_hint)

        return (
            f"\n\n{_GROUP_MEMBERS_HEADER}\n"
            f"Other members in this group chat:\n{members_text}\n\n"
            f"{mention_rules}"
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
        # Set by channel (e.g. Feishu: only true when @ appears in message text)
        # or by relay (from relayed message content). Default False.
        is_mentioned = meta.get("is_mentioned", False)

        logger.debug("GroupChatFilter: evaluating rules …")
        logger.debug(f"from_bot={from_bot}, policy={policy}")

        if from_bot:
            depth = session.count_trailing_bots() + 1 if session else 1
            logger.debug(f"bot depth={depth}")
            logger.debug(f"is mentioned={is_mentioned}")
            if depth >= self.max_bot_reply_depth:
                logger.debug(
                    f"  Skipping: depth {depth} >= max {self.max_bot_reply_depth}"
                )
                return False
            if not is_mentioned:
                return False
            if is_mentioned and depth <= self.bot_reply_llm_threshold:
                return True  # within threshold – no LLM needed
        else:
            if policy == "open" or is_mentioned:
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
