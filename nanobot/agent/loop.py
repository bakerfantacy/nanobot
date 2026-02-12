"""Agent loop: the core processing engine."""

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.subagent import SubagentManager
from nanobot.session.manager import Session, SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        max_bot_reply_depth: int = 8,
        bot_reply_llm_threshold: int = 3,
        bot_reply_llm_check: bool = True,
    ):
        from nanobot.config.schema import ExecToolConfig
        from nanobot.cron.service import CronService
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.max_bot_reply_depth = max_bot_reply_depth
        self.bot_reply_llm_threshold = bot_reply_llm_threshold
        self.bot_reply_llm_check = bot_reply_llm_check
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    # ------------------------------------------------------------------
    # Multi-agent group routing
    # ------------------------------------------------------------------

    async def _should_respond(
        self, msg: InboundMessage, session: Session | None = None
    ) -> bool:
        """
        Determine whether this agent should respond to the given message.

        Uses metadata set by the channel layer (e.g. Feishu):
        - is_mentioned: True if this bot was explicitly @mentioned.
        - group_policy: "mention" | "auto" | "open".
        - chat_type: "p2p" | "group".

        For non-group messages or messages from channels that don't set
        these metadata keys, this always returns True.

        When group_policy is "auto", session history is used to improve
        relevance detection for follow-up messages (e.g. "继续", "结果呢").
        """
        meta = msg.metadata or {}

         # Non-group messages (P2P, CLI, etc.) are always processed
        if meta.get("chat_type") != "group":
            return True


        # policy=auto, or from_bot (relay-injected): need LLM judgment
        # Bot-to-bot: depth hard limit first
        from_bot = meta.get("from_bot", False)
        logger.debug(f"is bot？: {from_bot}")

        policy = meta.get("group_policy", "open")
        if from_bot:
            depth = session.count_trailing_bots() + 1 if session else 1
            logger.debug(f"depth？: {depth}")
            if depth >= self.max_bot_reply_depth:
                logger.debug(f"Skipping bot message: depth {depth} >= max {self.max_bot_reply_depth}")
                return False
            if (not meta.get("is_mentioned", False)):
                return False
            if meta.get("is_mentioned", True) and depth <= self.bot_reply_llm_threshold or not self.bot_reply_llm_check:
                return True # Within threshold, no LLM needed
        else:
            # If this bot is @mentioned, always respond
            if policy == "open" or (meta.get("is_mentioned", True)):  # default True for channels without this key
                return True

    
        # Single LLM call for both: bot-to-bot control + relevance (user not @mentioned)
        logger.debug(f"judge by llm...")
        return await self._llm_should_respond(msg, session, from_bot=from_bot)

    async def _llm_should_respond(
        self,
        msg: InboundMessage,
        session: Session | None,
        from_bot: bool,
    ) -> bool:
        """
        Single LLM call: bot-to-bot control + relevance judgment.
        Handles: (1) another bot's message - should I reply? (2) user message not @me - is it relevant?
        Returns True = respond, False = skip.
        """
        meta = msg.metadata or {}
        group_members = meta.get("group_members", [])

        # Find this agent's description
        self_desc = ""
        from nanobot.config.loader import load_groups
        all_members = load_groups()
        other_names = {m.get("name", "") for m in group_members}
        for m in all_members:
            if m.name not in other_names and m.type == "bot":
                self_desc = f"{m.name}: {m.description}" if m.description else m.name
                break
        if not self_desc:
            parts = []
            for filename in ("AGENTS.md", "SOUL.md"):
                path = self.workspace / filename
                if path.exists():
                    try:
                        parts.append(path.read_text(encoding="utf-8")[:300])
                    except Exception:
                        pass
            self_desc = "\n".join(parts) if parts else "a helpful AI assistant"

        # Peers description
        peers_desc = ""
        if group_members:
            lines = []
            for m in group_members:
                name = m.get("name", "")
                mtype = m.get("type", "bot")
                desc = m.get("description", "")
                entry = f"- {name} ({mtype})"
                if desc:
                    entry += f": {desc}"
                lines.append(entry)
            peers_desc = "\nOther members in this group:\n" + "\n".join(lines)

        # Recent history
        history_blurb = ""
        if session:
            recent = session.get_recent_for_prompt(20)
            if recent:
                lines_h = []
                for x in recent[-8:]:
                    role = x.get("role", "?")
                    content = (x.get("content") or "")[:100]
                    sender = x.get("sender", "")
                    label = f"{role}" + (f" ({sender})" if sender else "")
                    lines_h.append(f"  {label}: {content}")
                history_blurb = "\nRecent:\n" + "\n".join(lines_h) + "\n\n"

        msg_preview = msg.content[:300]
        sender_hint = "Another bot" if from_bot else "A user (did NOT @mention you)"
        # Combined rules: bot-to-bot control + relevance
        rules = (
            "If from another BOT: NO for acknowledgments (OK/thanks), redundant, done. "
            "YES for substantive question, task needing you. "
            "If from a USER not @you: NO unless you were recently involved (follow-up like 继续) or it clearly targets your expertise. "
            "YES if recent follow-up or clear new request for you."
        )
        default_respond = not from_bot  # bot: conservative no; user: conservative yes

        prompt = (
            f"You are: {self_desc}\n"
            f"{peers_desc}\n\n"
            f"{sender_hint} said: \"{msg_preview}\"\n\n"
            f"{history_blurb}"
            f"Rules: {rules}\n\n"
            "Reply with ONLY 'YES' or 'NO'."
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
            reasoning = (getattr(response, "reasoning_content", None) or "").strip()
            combined = f"{reasoning}\n{content}".strip() or content or reasoning
            answer = combined.upper()
            if not answer:
                return default_respond
            should = "YES" in answer and (
                "NO" not in answer or answer.rfind("YES") > answer.rfind("NO")
            )
            logger.debug(f"LLM should_respond (from_bot={from_bot}): → {answer[:60]} → {should}")
            return should
        except Exception as e:
            logger.warning(f"LLM should_respond failed: {e}, default={default_respond}")
            return default_respond

    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        # Get or create session early (needed for relevance check when using group_policy=auto)
        session = self.sessions.get_or_create(msg.session_key)

        # Multi-agent group routing: check if this agent should respond
        if not await self._should_respond(msg, session):
            logger.info(
                f"Skipping message from {msg.channel}:{msg.sender_id} "
                f"(not relevant / not mentioned)"
            )
            return None

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
                
                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        # Log response preview
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")
        
        # Save to session (distinguish human vs other bot for depth calculation)
        meta = msg.metadata or {}
        if meta.get("from_bot"):
            session.add_message(
                "user",
                msg.content,
                sender_type="bot",
                sender=meta.get("sender_agent_name") or msg.sender_id,
            )
        else:
            session.add_message("user", msg.content, sender_type="human")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        
        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
                
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "Background task completed."
        
        # Save to session (mark as system message in history)
        session.add_message(
            "user",
            f"[System: {msg.sender_id}] {msg.content}",
            sender_type="system",
        )
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""
