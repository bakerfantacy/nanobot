"""Configuration module for nanobot."""

from nanobot.config.loader import (
    load_config,
    load_groups,
    get_config_path,
    get_groups_path,
    get_agent_dir,
    get_nanobot_home,
    list_agents,
    DEFAULT_AGENT_NAME,
)
from nanobot.config.schema import Config, GroupMember

__all__ = [
    "Config",
    "GroupMember",
    "load_config",
    "load_groups",
    "get_config_path",
    "get_groups_path",
    "get_agent_dir",
    "get_nanobot_home",
    "list_agents",
    "DEFAULT_AGENT_NAME",
]
