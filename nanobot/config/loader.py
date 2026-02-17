"""Configuration loading utilities."""

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config, GroupMember

DEFAULT_AGENT_NAME = "default"


def get_nanobot_home() -> Path:
    """Get the nanobot home directory (~/.nanobot)."""
    return Path.home() / ".nanobot"


def get_agent_dir(agent_name: str = DEFAULT_AGENT_NAME) -> Path:
    """
    Get the directory for a specific agent.

    Args:
        agent_name: Name of the agent. Defaults to "default".

    Returns:
        Path to ~/.nanobot/<agent_name>/
    """
    return get_nanobot_home() / agent_name


def get_config_path(agent_name: str = DEFAULT_AGENT_NAME) -> Path:
    """Get the configuration file path for a specific agent."""
    _maybe_migrate_legacy_config(agent_name)
    return get_agent_dir(agent_name) / "config.json"


def get_data_dir(agent_name: str = DEFAULT_AGENT_NAME) -> Path:
    """Get the data directory for a specific agent (same as agent dir)."""
    path = get_agent_dir(agent_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_agents() -> list[str]:
    """
    List all available agent names.

    Returns:
        Sorted list of agent names that have a config.json.
    """
    home = get_nanobot_home()
    if not home.exists():
        return []

    agents = []
    for child in sorted(home.iterdir()):
        if child.is_dir() and (child / "config.json").exists():
            agents.append(child.name)
    return agents


def get_groups_path() -> Path:
    """Get the shared groups configuration file path (~/.nanobot/groups.json)."""
    return get_nanobot_home() / "groups.json"


def load_groups() -> list[GroupMember]:
    """
    Load group member definitions from ~/.nanobot/groups.json.

    The file is a flat JSON array of member objects, each with
    name, open_id, type, and description.

    Returns:
        List of GroupMember (empty list if file missing or invalid).
    """
    path = get_groups_path()
    if not path.exists():
        return []

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: Failed to load groups from {path}: {e}")
        return []

    if not isinstance(raw, list):
        print(f"Warning: groups.json should be a JSON array, got {type(raw).__name__}")
        return []

    members: list[GroupMember] = []
    for m in raw:
        if isinstance(m, dict):
            members.append(GroupMember.model_validate(convert_keys(m)))
    return members


def load_config(
    config_path: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional explicit path to config file.
        agent_name: Agent name used when config_path is not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path(agent_name)

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            data = _migrate_config(data)

            # Inject agent_name so downstream code can reference it
            config = Config.model_validate(convert_keys(data))
            config._agent_name = agent_name
            return config
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")

    config = Config()
    config._agent_name = agent_name
    return config


def save_config(
    config: Config,
    config_path: Path | None = None,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional explicit path. Uses agent default if not provided.
        agent_name: Agent name used when config_path is not provided.
    """
    path = config_path or get_config_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert to camelCase format
    data = config.model_dump()
    data = convert_to_camel(data)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Legacy migration: ~/.nanobot/config.json → ~/.nanobot/default/config.json
# ---------------------------------------------------------------------------

_MIGRATION_DONE: set[str] = set()


def _maybe_migrate_legacy_config(agent_name: str) -> None:
    """
    Migrate the legacy single-agent config layout to multi-agent layout.

    Old layout:
        ~/.nanobot/config.json
        ~/.nanobot/workspace/
        ~/.nanobot/sessions/
        ~/.nanobot/cron/

    New layout:
        ~/.nanobot/<agent_name>/config.json
        ~/.nanobot/<agent_name>/workspace/
        ~/.nanobot/<agent_name>/sessions/
        ~/.nanobot/<agent_name>/cron/

    Only runs once per agent_name per process. Only migrates to "default".
    """
    if agent_name in _MIGRATION_DONE:
        return
    _MIGRATION_DONE.add(agent_name)

    if agent_name != DEFAULT_AGENT_NAME:
        return

    home = get_nanobot_home()
    legacy_config = home / "config.json"
    new_dir = home / DEFAULT_AGENT_NAME

    if not legacy_config.exists():
        return
    if (new_dir / "config.json").exists():
        # Already migrated; leave the legacy file as-is
        return

    import shutil

    new_dir.mkdir(parents=True, exist_ok=True)

    # Move config.json
    shutil.move(str(legacy_config), str(new_dir / "config.json"))

    # Move per-agent directories
    for dirname in ("workspace", "sessions", "cron"):
        src = home / dirname
        dst = new_dir / dirname
        if src.exists() and src.is_dir() and not dst.exists():
            shutil.move(str(src), str(dst))

    print(f"Migrated legacy config to ~/.nanobot/{DEFAULT_AGENT_NAME}/")


# ---------------------------------------------------------------------------
# Config data migration (schema changes)
# ---------------------------------------------------------------------------


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data


# ---------------------------------------------------------------------------
# Key conversion helpers
# ---------------------------------------------------------------------------


def convert_keys(data: Any) -> Any:
    """Convert camelCase keys to snake_case for Pydantic."""
    if isinstance(data, dict):
        return {camel_to_snake(k): convert_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_keys(item) for item in data]
    return data


def convert_to_camel(data: Any) -> Any:
    """Convert snake_case keys to camelCase."""
    if isinstance(data, dict):
        return {snake_to_camel(k): convert_to_camel(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_to_camel(item) for item in data]
    return data


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    result = []
    for i, char in enumerate(name):
        if char.isupper() and i > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])
