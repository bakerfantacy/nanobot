"""Base classes for message routing and scenario-specific prompt injection.

See :mod:`nanobot.agent.routing` package docstring for the overall
architecture.
"""

from __future__ import annotations

import abc

from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session


# -----------------------------------------------------------------------
# ResponseFilter
# -----------------------------------------------------------------------

class ResponseFilter(abc.ABC):
    """Base class for scenario-specific message filters.

    Subclasses implement three hooks:

    * :meth:`should_respond` – gate (respond / skip / defer).
    * :meth:`build_prompt_extras` – contribute extra text to the **system
      prompt** (contextual info like member lists).
    * :meth:`build_user_reminder` – inject a short reminder **right before
      the user message** for maximum salience.
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

    def build_user_reminder(
        self, msg: InboundMessage, session: Session | None
    ) -> str | None:
        """Return a short reminder to prepend to the user message.

        This text appears **immediately before** the user's message content,
        making it the most salient instruction for the LLM.  Keep it brief
        and focused on the single most critical behavioral constraint.

        Return ``None`` (the default) if there is nothing to add.
        """
        return None


# -----------------------------------------------------------------------
# MessageRouter
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

    def collect_user_reminders(
        self, msg: InboundMessage, session: Session | None = None
    ) -> list[str]:
        """Gather user-message reminders from all filters.

        These short texts are prepended to the user message so they sit in
        the highest-attention position for the LLM.
        """
        reminders: list[str] = []
        for f in self._filters:
            reminder = f.build_user_reminder(msg, session)
            if reminder:
                reminders.append(reminder)
        return reminders
