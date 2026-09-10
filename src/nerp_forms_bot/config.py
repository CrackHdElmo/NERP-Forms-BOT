"""Typed configuration loading for Discord resource mappings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str
    mode: Literal["forum_post", "existing_forum_post", "private_ticket_channel"]
    initial_status: str


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    guild: GuildConfig
    channels: dict[str, int | None] = Field(default_factory=dict)
    categories: dict[str, int | None] = Field(default_factory=dict)
    roles: dict[str, list[int]] = Field(default_factory=dict)
    workflows: dict[str, WorkflowConfig] = Field(default_factory=dict)
    test_requester_user_id: int | None = None


def load_environment(name: str, config_root: Path | None = None) -> EnvironmentConfig:
    """Load one environment profile and reject malformed settings early."""
    root = config_root or Path(__file__).resolve().parents[2] / "config" / "environments"
    path = root / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Environment configuration was not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return EnvironmentConfig.model_validate(raw)
