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


class GoogleWorkspaceConfig(BaseModel):
    """Non-secret Google resource identifiers for one environment."""

    model_config = ConfigDict(extra="forbid")

    drive_folder_id: str | None = None


class CourtOrderIntakeConfig(BaseModel):
    """The non-secret Google Form response source for the Case Management workflow."""

    model_config = ConfigDict(extra="forbid")

    form_url: str
    response_spreadsheet_id: str
    response_sheet_name: str


class CourtOrderWarrantConfig(BaseModel):
    """Google Docs resources used to create the player-facing Arrest Warrant."""

    model_config = ConfigDict(extra="forbid")

    template_document_id: str
    output_drive_folder_id: str


class CourtOrderSearchSeizureWarrantConfig(BaseModel):
    """Google Docs resources used for Search / Seizure Warrant generation."""

    model_config = ConfigDict(extra="forbid")

    template_document_id: str
    output_drive_folder_id: str


class CourtOrderSubpoenaConfig(BaseModel):
    """Google Docs resources used for Subpoena generation."""

    model_config = ConfigDict(extra="forbid")

    template_document_id: str
    output_drive_folder_id: str


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    guild: GuildConfig
    channels: dict[str, int | None] = Field(default_factory=dict)
    categories: dict[str, int | None] = Field(default_factory=dict)
    roles: dict[str, list[int]] = Field(default_factory=dict)
    workflows: dict[str, WorkflowConfig] = Field(default_factory=dict)
    google_workspace: GoogleWorkspaceConfig = Field(default_factory=GoogleWorkspaceConfig)
    court_order_intake: CourtOrderIntakeConfig | None = None
    court_order_warrant: CourtOrderWarrantConfig | None = None
    court_order_search_seizure_warrant: CourtOrderSearchSeizureWarrantConfig | None = None
    court_order_subpoena: CourtOrderSubpoenaConfig | None = None


class MasterDeploymentConfig(BaseModel):
    """Private one-file deployment configuration used by the live bot source repository."""

    model_config = ConfigDict(extra="forbid")

    runtime: dict[str, object]
    environment: EnvironmentConfig


def load_environment(name: str, config_root: Path | None = None) -> EnvironmentConfig:
    """Load one environment profile and reject malformed settings early."""
    root = config_root or Path(__file__).resolve().parents[2] / "config" / "environments"
    path = root / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Environment configuration was not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return EnvironmentConfig.model_validate(raw)


def load_master_deployment(path: Path | None = None) -> MasterDeploymentConfig | None:
    """Load the private all-in-one deployment file when a deployment repository provides it."""
    master_path = path or Path.cwd() / "config" / "master.yaml"
    if not master_path.is_file():
        return None
    with master_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return MasterDeploymentConfig.model_validate(raw)
