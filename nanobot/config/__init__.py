"""Configuration module for nanobot."""

from nanobot.config.loader import (
    load_config,
    get_config_path,
    get_agent_dir,
    get_nanobot_home,
    list_agents,
    DEFAULT_AGENT_NAME,
)
from nanobot.config.schema import Config

__all__ = [
    "Config",
    "load_config",
    "get_config_path",
    "get_agent_dir",
    "get_nanobot_home",
    "list_agents",
    "DEFAULT_AGENT_NAME",
]
