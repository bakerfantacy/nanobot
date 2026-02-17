"""Message routing and scenario-specific prompt injection.

This package isolates scenario-specific logic (group chat, bot-to-bot, etc.)
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

from nanobot.agent.routing.base import MessageRouter, ResponseFilter
from nanobot.agent.routing.group_chat import GroupChatFilter

__all__ = ["MessageRouter", "ResponseFilter", "GroupChatFilter"]
