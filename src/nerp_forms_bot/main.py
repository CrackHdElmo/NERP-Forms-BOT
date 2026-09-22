"""Discord process entry point for NERP - Case Management."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO

import discord
from discord import app_commands
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import EnvironmentConfig, load_environment, load_master_deployment
from .fivemanage import FiveManageService
from .google_workspace import (
    FORM_RESPONSE_ARCHIVE_STATUS_HEADER,
    GeneratedWarrant,
    GoogleWorkspaceService,
    TrackingRow,
)
from .submission_store import Submission, SubmissionStore

LOGGER = logging.getLogger(__name__)

CASE_ASSIGNMENT_TYPES = ("prosecutor", "judge", "defense_attorney", "pd_officer")
CASE_ASSIGNMENT_SLOTS = (1, 2)
SERVICE_DESK_CONFIG_KEY = "service_desk_embed"
SERVICE_DESK_MESSAGE_KEY = "service_desk_embed_message_id"


class CourtOrderMergeError(Exception):
    """Raised when a requested Court Order-to-Docket merge cannot safely proceed."""


class Settings(BaseSettings):
    """Runtime settings loaded from protected environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    nerp_environment: str = "production"
    google_service_account_file: str | None = None
    database_url: str = "sqlite+aiosqlite:///./data/nerp_forms_bot.db"
    google_forms_poll_interval_seconds: int = 60
    fivemanage_api_token: str | None = None
    fivemanage_storage_path: str = "nerp-doj/court-orders"


@dataclass(frozen=True)
class SetupPlan:
    """One administrator-reviewed Discord resource setup plan."""

    preset: str
    names: dict[str, str]


class SetupPresetView(discord.ui.View):
    """First, private setup-wizard step: choose the resource preset."""

    def __init__(self, bot: NerpFormsBot) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        selector = discord.ui.Select(
            placeholder="Choose a workflow to set up…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Court Administration",
                    value="court_administration",
                    description="Service desk, Docket Forum, and staff case records.",
                ),
                discord.SelectOption(
                    label="Off-Docket Court Orders",
                    value="off_docket_court_orders",
                    description="Private category for bot-created Court Order tickets.",
                ),
                discord.SelectOption(
                    label="Attorney Requests",
                    value="attorney_requests",
                    description="Private category prepared for Attorney Request tickets.",
                ),
                discord.SelectOption(
                    label="Business Licensing",
                    value="business_licensing",
                    description="Corporate Office category and Business Licensing Forum.",
                ),
            ],
        )
        selector.callback = self._choose_preset
        self.add_item(selector)

    async def _choose_preset(self, interaction: discord.Interaction) -> None:
        if not self.bot._is_administrator(interaction):
            await interaction.response.send_message(
                "Only a configured bot administrator can run setup.", ephemeral=True
            )
            return
        selector = self.children[0]
        assert isinstance(selector, discord.ui.Select)
        plan = self.bot._default_setup_plan(selector.values[0])
        await interaction.response.edit_message(
            embed=self.bot._setup_preview_embed(plan),
            view=SetupPlanView(self.bot, plan),
        )


class SetupPlanView(discord.ui.View):
    """Second wizard step: customize or confirm an explicit setup preview."""

    def __init__(self, bot: NerpFormsBot, plan: SetupPlan) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.plan = plan

    @discord.ui.button(label="Customize names", style=discord.ButtonStyle.secondary)
    async def customize(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.bot._is_administrator(interaction):
            await interaction.response.send_message(
                "Only a configured bot administrator can run setup.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SetupNamesModal(self.bot, self.plan))

    @discord.ui.button(label="Confirm setup", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.bot._is_administrator(interaction):
            await interaction.response.send_message(
                "Only a configured bot administrator can run setup.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.bot._apply_setup_plan(self.plan)
        except (discord.Forbidden, PermissionError):
            await interaction.followup.send(
                "The bot lacks a required Discord permission. Grant its role **Manage Channels**, "
                "**Manage Roles**, and **Manage Threads**, then run `/setup-workflow` again.",
                ephemeral=True,
            )
            return
        except Exception:
            LOGGER.exception("Guided setup failed for preset %s", self.plan.preset)
            await interaction.followup.send(
                "Setup could not be completed. Check the Wispbyte console for the safe diagnostic message.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Setup cancelled. No resources were created.", embed=None, view=None)


class SetupNamesModal(discord.ui.Modal):
    """Optional names step for a guided setup plan."""

    def __init__(self, bot: NerpFormsBot, plan: SetupPlan) -> None:
        super().__init__(title="Customize setup names")
        self.bot = bot
        self.plan = plan
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for resource_key, label in bot._setup_name_fields(plan.preset):
            text_input = discord.ui.TextInput(
                label=label,
                default=plan.names[resource_key],
                required=True,
                max_length=100,
            )
            self.inputs[resource_key] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = {resource_key: text_input.value for resource_key, text_input in self.inputs.items()}
        plan = self.bot._default_setup_plan(self.plan.preset, names)
        await interaction.response.send_message(
            embed=self.bot._setup_preview_embed(plan),
            view=SetupPlanView(self.bot, plan),
            ephemeral=True,
        )


class NerpFormsBot(discord.Client):
    """Minimal Discord client; workflow handlers are added in later milestones."""

    def __init__(self, environment: EnvironmentConfig, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.environment = environment
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = SubmissionStore(settings.database_url)
        self._direct_bot_administrator_ids: set[int] = set()
        self._sync_lock = asyncio.Lock()
        self._court_order_poll_task: asyncio.Task[None] | None = None

    def _is_administrator(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if interaction.guild and interaction.guild.owner_id == member.id:
            return True
        if member.guild_permissions.administrator:
            return True
        if member.id in self._direct_bot_administrator_ids:
            return True
        administrator_role_ids = set(self.environment.roles.get("administrators", []))
        return any(role.id in administrator_role_ids for role in member.roles)

    def _can_approve_warrant(self, interaction: discord.Interaction) -> bool:
        """Allow the configured judicial role; administrators remain an authorized override."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        judge_role_ids = set(self.environment.roles.get("judges", []))
        return self._is_administrator(interaction) or any(
            role.id in judge_role_ids for role in member.roles
        )

    def _can_deny_warrant(self, interaction: discord.Interaction) -> bool:
        """A judicial denial has the same authority boundary as a judicial approval."""
        return self._can_approve_warrant(interaction)

    def _can_refresh_court_order(self, interaction: discord.Interaction) -> bool:
        """Allow administrators, judicial staff, and the Attorney General to repair an active intake."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        refresh_role_ids = self._role_ids(
            "judges",
            "judges_office",
            "senior_judges",
            "attorney_general",
        )
        return self._is_administrator(interaction) or any(
            role.id in refresh_role_ids for role in member.roles
        )

    def _can_merge_court_order(self, interaction: discord.Interaction) -> bool:
        """Use the same judicial/AG authority boundary for moving an investigative Court Order."""
        return self._can_refresh_court_order(interaction)

    def _can_manage_case_assignments(self, interaction: discord.Interaction) -> bool:
        """Allow the configured DOJ/PD leadership groups to make a direct reassignment."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        management_role_ids = self._role_ids(
            "high_command_doj",
            "attorney_general",
            "district_attorneys_office",
            "lspd_high_command",
            "lspd_chief",
        )
        return self._is_administrator(interaction) or any(
            role.id in management_role_ids for role in member.roles
        )

    def _assignment_role_ids(self, assignment_type: str) -> set[int]:
        """Resolve the server roles eligible for each optional case assignment."""
        groups = {
            "prosecutor": (
                "prosecutors",
                "district_attorneys_office",
                "assistant_district_attorneys",
                "attorney_general",
            ),
            "judge": ("judges", "judges_office", "senior_judges"),
            "defense_attorney": ("defense_attorneys", "chief_of_defense"),
            "pd_officer": ("lspd", "lspd_high_command", "lspd_chief"),
        }
        return self._role_ids(*groups.get(assignment_type, ()))

    def _member_can_fill_assignment(
        self, member: discord.Member, assignment_type: str
    ) -> bool:
        """Avoid assigning a user to a case role they do not hold on this Discord server."""
        eligible_role_ids = self._assignment_role_ids(assignment_type)
        return any(role.id in eligible_role_ids for role in member.roles)

    @staticmethod
    def _assignment_label(assignment_type: str) -> str:
        return {
            "prosecutor": "Prosecutor",
            "judge": "Judge",
            "defense_attorney": "Defense Attorney",
            "pd_officer": "PD Officer",
        }[assignment_type]

    async def _case_assignment_embed(self, submission: Submission) -> discord.Embed:
        """Show all optional case roles, explicitly distinguishing an unassigned role."""
        current = {
            (assignment.assignment_type, assignment.assignment_slot): assignment
            for assignment in await self.store.get_case_assignments(submission.id)
        }
        embed = discord.Embed(
            title=f"Case assignments — {submission.request_id}",
            description="Current staff assignments for this ticket. Roles remain optional until assigned.",
            color=discord.Color.blurple(),
        )
        for assignment_type in CASE_ASSIGNMENT_TYPES:
            value = "\n".join(
                f"{slot}. <@{current[assignment_type, slot].user_id}>"
                if (assignment_type, slot) in current
                else f"{slot}. Unassigned"
                for slot in CASE_ASSIGNMENT_SLOTS
            )
            embed.add_field(
                name=f"{self._assignment_label(assignment_type)}s", value=value, inline=True
            )
        return embed

    def _role_ids(self, *role_groups: str) -> set[int]:
        """Resolve configured role groups while allowing configured role aliases."""
        return {
            role_id
            for role_group in role_groups
            for role_id in self.environment.roles.get(role_group, [])
        }

    def _can_close_ticket(self, interaction: discord.Interaction, submission: Submission) -> bool:
        """Allow the requester, judicial/AG staff, or a server administrator to close a request."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if (
            self._is_administrator(interaction)
            or member.id == submission.claimed_user_id
            or (
                submission.workflow == "new_docket"
                and self._normalize_username(member.name) == submission.requester_username
            )
        ):
            return True
        closer_role_ids = self._role_ids(
            "judges",
            "judges_office",
            "senior_judges",
            "attorney_general",
        )
        return any(role.id in closer_role_ids for role in member.roles)

    def _case_record_viewer_role_ids(self) -> set[int]:
        """Return DOJ and PD staff roles that may read the permanent records channel."""
        return self._role_ids(
            "administrators",
            "district_attorneys_office",
            "defense_attorneys",
            "chief_of_defense",
            "assistant_district_attorneys",
            "prosecutors",
            "lspd",
            "lspd_high_command",
            "lspd_chief",
            "judges",
            "judges_office",
            "senior_judges",
            "high_command_doj",
            "paralegals",
            "attorney_general",
        )

    def _existing_docket_reference(self, payload: dict[str, str]) -> str:
        """Read either version of the Form's Docket / Off-Docket reference question."""
        return self._answer_prefix(payload, "Please indicate the Docket or Off-Docket Name/ID") or self._answer(
            payload,
            "Please indicate the Docket Name/ID.",
        )

    @staticmethod
    def _setup_name_fields(preset: str) -> list[tuple[str, str]]:
        fields = {
            "court_administration": [
                ("court_administration_category", "Category name"),
                ("service_desk_channel", "Service Desk channel name"),
                ("docket_forum_channel", "Docket Forum name"),
                ("doj_case_records", "DOJ Case Records channel name"),
            ],
            "off_docket_court_orders": [
                ("off_docket_tickets_category", "Category name"),
            ],
            "attorney_requests": [
                ("attorney_request_tickets_category", "Category name"),
            ],
            "business_licensing": [
                ("corporate_office_category", "Category name"),
                ("business_licensing_forum_channel", "Business Licensing Forum name"),
            ],
        }
        return fields[preset]

    def _default_setup_plan(
        self, preset: str, custom_names: dict[str, str] | None = None
    ) -> SetupPlan:
        defaults = {
            "court_administration": {
                "court_administration_category": "Court Administration",
                "service_desk_channel": "service-desk",
                "docket_forum_channel": "docket",
                "doj_case_records": "doj-case-records",
            },
            "off_docket_court_orders": {
                "off_docket_tickets_category": "Off-Docket Requests",
            },
            "attorney_requests": {
                "attorney_request_tickets_category": "Attorney Requests",
            },
            "business_licensing": {
                "corporate_office_category": "Corporate Office",
                "business_licensing_forum_channel": "business-licensing",
            },
        }
        if preset not in defaults:
            raise ValueError("Unknown guided setup preset.")
        names = defaults[preset] | (custom_names or {})
        for resource_key, label in self._setup_name_fields(preset):
            value = names.get(resource_key, "").strip()
            if not value:
                raise ValueError(f"{label} cannot be blank.")
            names[resource_key] = (
                self._normalize_discord_name(value) if "channel" in resource_key or resource_key == "doj_case_records" else value[:100]
            )
        return SetupPlan(preset=preset, names=names)

    @staticmethod
    def _normalize_discord_name(value: str) -> str:
        """Turn an administrator-entered channel name into a valid readable Discord name."""
        normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return normalized[:100] or "nerp-resource"

    def _setup_preview_embed(self, plan: SetupPlan) -> discord.Embed:
        labels = {
            "court_administration": "Court Administration",
            "off_docket_court_orders": "Off-Docket Court Orders",
            "attorney_requests": "Attorney Requests",
            "business_licensing": "Business Licensing",
        }
        details = {
            "court_administration": "Creates a service directory, Docket Forum, and DOJ/PD-only case-records channel.",
            "off_docket_court_orders": "Creates the private category used for bot-created Court Order tickets.",
            "attorney_requests": "Creates the private category reserved for future Attorney Request tickets.",
            "business_licensing": "Creates the Corporate Office category and its staff-operated licensing Forum.",
        }
        lines = []
        for resource_key, label in self._setup_name_fields(plan.preset):
            resource_type = "category" if resource_key.endswith("category") else "channel / Forum"
            lines.append(f"**{label}** ({resource_type}): `{plan.names[resource_key]}`")
        embed = discord.Embed(
            title=f"Setup preview — {labels[plan.preset]}",
            description=(
                f"{details[plan.preset]}\n\nNo resource will be created until you select **Confirm setup**. "
                "Existing matching bot resources will be reused rather than duplicated."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Resources", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Permissions applied to new resources",
            value=(
                "Public directory only: read-only. Staff resources: DOJ/PD staff roles only. "
                "Private ticket categories: hidden from the public."
            ),
            inline=False,
        )
        return embed

    async def _registered_or_configured_channel(
        self, guild: discord.Guild, resource_key: str, configured_id: int | None
    ) -> discord.abc.GuildChannel | None:
        stored_id = await self.store.get_resource_channel_id(resource_key)
        for channel_id in (stored_id, configured_id):
            if channel_id:
                channel = guild.get_channel(channel_id)
                if channel:
                    return channel
        return None

    @staticmethod
    def _default_service_desk_config() -> dict[str, object]:
        return {
            "title": "NERP - Case Management Service Desk",
            "description": (
                "Your central directory for case-management forms, court services, "
                "standard operating procedures, and related community resources."
            ),
            "image_url": None,
            "form_links": [None, None, None, None],
            "external_links": [None, None, None, None],
        }

    async def _load_service_desk_config(self) -> dict[str, object]:
        """Load the editable Service Desk directory, repairing malformed saved values safely."""
        config = self._default_service_desk_config()
        raw = await self.store.get_bot_setting(SERVICE_DESK_CONFIG_KEY)
        if not raw:
            return config
        try:
            saved = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring malformed saved Service Desk configuration")
            return config
        if not isinstance(saved, dict):
            return config
        for key, maximum in (("title", 256), ("description", 4096), ("image_url", 2048)):
            value = saved.get(key)
            if value is None and key in {"description", "image_url"}:
                config[key] = None
                continue
            if isinstance(value, str) and len(value.strip()) <= maximum:
                config[key] = value.strip() or None
        for key in ("form_links", "external_links"):
            saved_links = saved.get(key)
            if not isinstance(saved_links, list):
                continue
            links: list[dict[str, str] | None] = [None, None, None, None]
            for index, item in enumerate(saved_links[:4]):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("label"), str)
                    and isinstance(item.get("url"), str)
                    and item["label"].strip()
                    and item["url"].strip()
                ):
                    links[index] = {"label": item["label"].strip()[:80], "url": item["url"].strip()[:2048]}
            config[key] = links
        return config

    async def _save_service_desk_config(self, config: dict[str, object]) -> None:
        await self.store.set_bot_setting(SERVICE_DESK_CONFIG_KEY, json.dumps(config, sort_keys=True))

    async def _get_service_desk_channel(self) -> discord.TextChannel:
        guild = self.get_guild(self.environment.guild.id)
        if not guild:
            raise RuntimeError("The configured Discord server is not currently available.")
        channel = await self._registered_or_configured_channel(
            guild, "service_desk_channel", self.environment.channels.get("service_desk")
        )
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("The configured #service-desk channel could not be found.")
        return channel

    @staticmethod
    def _service_desk_embed(config: dict[str, object]) -> discord.Embed:
        title = config.get("title") or "NERP - Case Management Service Desk"
        description = config.get("description") or "Case-management resources and information."
        embed = discord.Embed(title=str(title), description=str(description), color=discord.Color.blurple())
        for key, heading, empty_text in (
            ("form_links", "Case & Court Forms", "No form links have been added yet."),
            ("external_links", "Policies & External Resources", "No external links have been added yet."),
        ):
            links = config.get(key)
            if not isinstance(links, list):
                links = []
            values = [
                f"{index}. [{item['label']}]({item['url']})"
                for index, item in enumerate(links, start=1)
                if isinstance(item, dict) and item.get("label") and item.get("url")
            ]
            embed.add_field(name=heading, value="\n".join(values) or empty_text, inline=False)
        image_url = config.get("image_url")
        if isinstance(image_url, str) and image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text="Managed by NERP - Case Management")
        return embed

    async def _publish_service_desk(self, config: dict[str, object]) -> tuple[discord.TextChannel, bool]:
        """Create or update the one bot-owned Service Desk embed without duplicate posts."""
        channel = await self._get_service_desk_channel()
        embed = self._service_desk_embed(config)
        raw_message_id = await self.store.get_bot_setting(SERVICE_DESK_MESSAGE_KEY)
        if raw_message_id and raw_message_id.isdigit():
            try:
                message = await channel.fetch_message(int(raw_message_id))
                if self.user and message.author.id == self.user.id:
                    await message.edit(embed=embed)
                    await self._save_service_desk_config(config)
                    return channel, False
            except (discord.NotFound, discord.Forbidden):
                pass
        message = await channel.send(embed=embed)
        await self.store.set_bot_setting(SERVICE_DESK_MESSAGE_KEY, str(message.id))
        await self._save_service_desk_config(config)
        return channel, True

    async def _adopt_service_desk_message(self, message_link: str) -> None:
        """Select one existing bot-authored Service Desk message for future in-place updates."""
        match = re.fullmatch(
            r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)/?",
            message_link.strip(),
        )
        if not match:
            raise ValueError("Provide the full Discord message link for the existing Service Desk post.")
        channel = await self._get_service_desk_channel()
        channel_id, message_id = (int(value) for value in match.groups())
        if channel.id != channel_id:
            raise ValueError("That message is not located in the configured #service-desk channel.")
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound as error:
            raise ValueError("That Service Desk message could not be found.") from error
        if not self.user or message.author.id != self.user.id:
            raise ValueError(
                "The bot can only adopt a message it authored. It will create and maintain its own directory post instead."
            )
        await self.store.set_bot_setting(SERVICE_DESK_MESSAGE_KEY, str(message.id))

    async def _set_service_desk_link(
        self,
        interaction: discord.Interaction,
        *,
        key: str,
        slot: int,
        label: str,
        url: str,
    ) -> None:
        """Validate one directory link, persist it, and republish the managed embed."""
        if not self._is_administrator(interaction):
            await interaction.response.send_message(
                "Only a configured bot administrator can update the Service Desk.", ephemeral=True
            )
            return
        if not label.strip() or len(label.strip()) > 80:
            await interaction.response.send_message(
                "The visible link label must contain between 1 and 80 characters.", ephemeral=True
            )
            return
        if not re.fullmatch(r"https://\S{1,2040}", url.strip()):
            await interaction.response.send_message(
                "Provide a direct HTTPS URL for the Service Desk link.", ephemeral=True
            )
            return
        config = await self._load_service_desk_config()
        links = config[key]
        assert isinstance(links, list)
        links[slot - 1] = {"label": label.strip(), "url": url.strip()}
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel, created = await self._publish_service_desk(config)
        except discord.Forbidden:
            await interaction.followup.send(
                "The bot needs permission to view #service-desk, read its message history, and send/edit messages there.",
                ephemeral=True,
            )
            return
        except (RuntimeError, TypeError) as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            LOGGER.exception("Service Desk link update failed")
            await interaction.followup.send(
                "The Service Desk embed could not be updated. Check the Wispbyte console for the safe diagnostic message.",
                ephemeral=True,
            )
            return
        action = "Created" if created else "Updated"
        await interaction.followup.send(
            f"{action} the managed Service Desk embed in {channel.mention}.", ephemeral=True
        )

    async def _resolve_category(self, resource_key: str, configured_key: str) -> discord.CategoryChannel:
        guild = self.get_guild(self.environment.guild.id)
        configured_id = self.environment.categories.get(configured_key)
        category = (
            await self._registered_or_configured_channel(guild, resource_key, configured_id)
            if guild
            else None
        )
        if not isinstance(category, discord.CategoryChannel):
            raise TypeError(f"The required category '{configured_key}' is not configured.")
        return category

    def _staff_overwrites(
        self, guild: discord.Guild, *, allow_messages: bool
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        """Use the same DOJ/PD staff visibility model for newly-created staff resources."""
        me = guild.me
        if not me:
            raise RuntimeError("The bot member was not available in the configured guild.")
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                manage_channels=True,
                manage_messages=True,
                manage_threads=True,
            ),
        }
        for role_id in self._case_record_viewer_role_ids():
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=allow_messages,
                    read_message_history=True,
                    attach_files=allow_messages,
                    embed_links=allow_messages,
                    create_public_threads=allow_messages,
                    send_messages_in_threads=allow_messages,
                )
        return overwrites

    async def _ensure_category(
        self,
        guild: discord.Guild,
        *,
        resource_key: str,
        configured_key: str,
        name: str,
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite],
    ) -> tuple[discord.CategoryChannel, bool]:
        category = await self._registered_or_configured_channel(
            guild, resource_key, self.environment.categories.get(configured_key)
        )
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name=name)
        created = False
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category(
                name=name,
                overwrites=overwrites,
                reason="NERP - Case Management guided setup",
            )
            created = True
        await self.store.set_resource_channel_id(resource_key, category.id)
        return category, created

    async def _ensure_text_channel(
        self,
        guild: discord.Guild,
        *,
        resource_key: str,
        configured_key: str | None,
        category: discord.CategoryChannel,
        name: str,
        topic: str,
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite],
    ) -> tuple[discord.TextChannel, bool]:
        configured_id = self.environment.channels.get(configured_key) if configured_key else None
        channel = await self._registered_or_configured_channel(guild, resource_key, configured_id)
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(category.text_channels, name=name)
        created = False
        if not isinstance(channel, discord.TextChannel):
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                topic=topic,
                overwrites=overwrites,
                reason="NERP - Case Management guided setup",
            )
            created = True
        await self.store.set_resource_channel_id(resource_key, channel.id)
        return channel, created

    async def _ensure_forum_channel(
        self,
        guild: discord.Guild,
        *,
        resource_key: str,
        configured_key: str | None,
        category: discord.CategoryChannel,
        name: str,
        topic: str,
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite],
    ) -> tuple[discord.ForumChannel, bool]:
        configured_id = self.environment.channels.get(configured_key) if configured_key else None
        channel = await self._registered_or_configured_channel(guild, resource_key, configured_id)
        if not isinstance(channel, discord.ForumChannel):
            channel = discord.utils.get(category.forums, name=name)
        created = False
        if not isinstance(channel, discord.ForumChannel):
            channel = await guild.create_forum(
                name=name,
                category=category,
                topic=topic,
                overwrites=overwrites,
                reason="NERP - Case Management guided setup",
            )
            created = True
        await self.store.set_resource_channel_id(resource_key, channel.id)
        return channel, created

    async def _apply_setup_plan(self, plan: SetupPlan) -> str:
        """Create or safely register all resources in one confirmed administrator plan."""
        guild = self.get_guild(self.environment.guild.id)
        if not guild or not guild.me:
            raise RuntimeError("The configured Discord server or bot member was not available.")
        required_permissions = guild.me.guild_permissions
        if not (
            required_permissions.manage_channels
            and required_permissions.manage_roles
            and required_permissions.manage_threads
        ):
            raise PermissionError("Missing Manage Channels, Manage Roles, or Manage Threads.")

        results: list[tuple[str, discord.abc.GuildChannel, bool]] = []
        if plan.preset == "court_administration":
            category, created = await self._ensure_category(
                guild,
                resource_key="court_administration_category",
                configured_key="court_administration",
                name=plan.names["court_administration_category"],
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Category", category, created))
            service_desk_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
                ),
            }
            service_desk, created = await self._ensure_text_channel(
                guild,
                resource_key="service_desk_channel",
                configured_key="service_desk",
                category=category,
                name=plan.names["service_desk_channel"],
                topic="NERP - Case Management request directory and workflow information.",
                overwrites=service_desk_overwrites,
            )
            results.append(("Read-only directory", service_desk, created))
            docket, created = await self._ensure_forum_channel(
                guild,
                resource_key="docket_forum_channel",
                configured_key="docket_forum",
                category=category,
                name=plan.names["docket_forum_channel"],
                topic="Active criminal and civil Docket records.",
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Docket Forum", docket, created))
            records, created = await self._ensure_text_channel(
                guild,
                resource_key="doj_case_records",
                configured_key=None,
                category=category,
                name=plan.names["doj_case_records"],
                topic="NERP - Case Management permanent DOJ/PD case and request closure records.",
                overwrites=self._staff_overwrites(guild, allow_messages=False),
            )
            results.append(("Staff records", records, created))
        elif plan.preset == "off_docket_court_orders":
            category, created = await self._ensure_category(
                guild,
                resource_key="off_docket_tickets_category",
                configured_key="off_docket_tickets",
                name=plan.names["off_docket_tickets_category"],
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Private ticket category", category, created))
        elif plan.preset == "attorney_requests":
            category, created = await self._ensure_category(
                guild,
                resource_key="attorney_request_tickets_category",
                configured_key="attorney_request_tickets",
                name=plan.names["attorney_request_tickets_category"],
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Private ticket category", category, created))
        elif plan.preset == "business_licensing":
            category, created = await self._ensure_category(
                guild,
                resource_key="corporate_office_category",
                configured_key="corporate_office",
                name=plan.names["corporate_office_category"],
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Category", category, created))
            forum, created = await self._ensure_forum_channel(
                guild,
                resource_key="business_licensing_forum_channel",
                configured_key="business_licensing_forum",
                category=category,
                name=plan.names["business_licensing_forum_channel"],
                topic="Business Licensing and Corporate Office workflow records.",
                overwrites=self._staff_overwrites(guild, allow_messages=True),
            )
            results.append(("Business Licensing Forum", forum, created))
        else:
            raise ValueError("Unknown guided setup preset.")

        summary = "\n".join(
            f"• {'Created' if created else 'Reused'} {label}: {resource.mention}"
            for label, resource, created in results
        )
        return f"**Setup complete — {plan.preset.replace('_', ' ').title()}**\n{summary}"

    async def setup_hook(self) -> None:
        await self.store.initialize()
        self._direct_bot_administrator_ids = await self.store.get_bot_admin_user_ids()
        guild = discord.Object(id=self.environment.guild.id)
        # Guild commands update immediately during development.  Clearing the scoped
        # tree first also replaces any stale command schema that Discord retained
        # from an earlier intake run.
        self.tree.clear_commands(guild=guild)

        @self.tree.command(
            name="bot-status",
            description="Show the current NERP - Case Management environment.",
            guild=guild,
        )
        async def bot_status(interaction: discord.Interaction) -> None:
            if interaction.guild_id != self.environment.guild.id:
                await interaction.response.send_message(
                    "This command is only configured for the selected NERP environment.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"NERP - Case Management is connected to **{self.environment.guild.name}** "
                f"using the **{self.environment.environment}** configuration profile.",
                ephemeral=True,
            )

        @self.tree.command(
            name="setup-workflow",
            description="Guided administrator setup for NERP categories, channels, and Forums.",
            guild=guild,
        )
        async def setup_workflow(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can run setup.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                "Choose the workflow resources you want the bot to set up. This preview is private; "
                "nothing will be created until you confirm the plan.",
                view=SetupPresetView(self),
                ephemeral=True,
            )

        bot_admin = app_commands.Group(
            name="bot-admin",
            description="Manage direct NERP bot-administrator access.",
        )
        self.tree.add_command(bot_admin, guild=guild)

        @bot_admin.command(
            name="add",
            description="Grant a server member direct NERP bot-administrator access.",
        )
        @app_commands.describe(member="The server member who should receive direct bot-admin access.")
        async def bot_admin_add(
            interaction: discord.Interaction, member: discord.Member
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only the server owner or an existing bot administrator can manage bot administrators.",
                    ephemeral=True,
                )
                return
            if member.bot:
                await interaction.response.send_message(
                    "Bot accounts cannot receive a direct bot-administrator grant.", ephemeral=True
                )
                return
            added = await self.store.add_bot_administrator(
                user_id=member.id,
                user_name=member.display_name,
                added_by_user_id=interaction.user.id,
                added_by_name=interaction.user.display_name,
                added_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
            if not added:
                await interaction.response.send_message(
                    f"{member.mention} already has a direct bot-administrator grant.", ephemeral=True
                )
                return
            self._direct_bot_administrator_ids.add(member.id)
            await interaction.response.send_message(
                f"Granted {member.mention} direct NERP bot-administrator access. "
                "This private confirmation is also retained in the bot database.",
                ephemeral=True,
            )

        @bot_admin.command(
            name="remove",
            description="Remove a server member's direct NERP bot-administrator access.",
        )
        @app_commands.describe(member="The server member whose direct bot-admin access should be removed.")
        async def bot_admin_remove(
            interaction: discord.Interaction, member: discord.Member
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only the server owner or an existing bot administrator can manage bot administrators.",
                    ephemeral=True,
                )
                return
            removed = await self.store.remove_bot_administrator(member.id)
            if not removed:
                await interaction.response.send_message(
                    f"{member.mention} does not have a direct bot-administrator grant to remove.",
                    ephemeral=True,
                )
                return
            self._direct_bot_administrator_ids.discard(member.id)
            await interaction.response.send_message(
                f"Removed {member.mention}'s direct NERP bot-administrator grant. This does not remove "
                "Discord server ownership, Discord Administrator permission, or access inherited from a configured administrator role.",
                ephemeral=True,
            )

        @bot_admin.command(
            name="list",
            description="Privately list direct NERP bot-administrator grants.",
        )
        async def bot_admin_list(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only the server owner or an existing bot administrator can view bot administrators.",
                    ephemeral=True,
                )
                return
            grants = await self.store.list_bot_administrators()
            if not grants:
                await interaction.response.send_message(
                    "There are no direct bot-administrator grants. Server owners, Discord Administrators, and configured administrator roles may still administer the bot.",
                    ephemeral=True,
                )
                return
            lines = [f"• <@{grant.user_id}> — added by **{grant.added_by_name}**" for grant in grants]
            await interaction.response.send_message(
                "**Direct NERP bot administrators**\n" + "\n".join(lines), ephemeral=True
            )

        service_desk = app_commands.Group(
            name="service-desk",
            description="Build and maintain the #service-desk directory embed.",
        )
        self.tree.add_command(service_desk, guild=guild)

        async def publish_service_desk_update(
            interaction: discord.Interaction, config: dict[str, object]
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                channel, created = await self._publish_service_desk(config)
            except discord.Forbidden:
                await interaction.followup.send(
                    "The bot needs permission to view #service-desk, read its message history, and send/edit messages there.",
                    ephemeral=True,
                )
                return
            except (RuntimeError, TypeError) as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Service Desk update failed")
                await interaction.followup.send(
                    "The Service Desk embed could not be updated. Check the Wispbyte console for the safe diagnostic message.",
                    ephemeral=True,
                )
                return
            action = "Created" if created else "Updated"
            await interaction.followup.send(
                f"{action} the managed Service Desk embed in {channel.mention}.", ephemeral=True
            )

        @service_desk.command(
            name="edit",
            description="Set the Service Desk header, optional directory text, or image.",
        )
        @app_commands.describe(
            title="New embed header title; leave blank to keep the current title.",
            description="Optional directory introduction; leave blank to keep the current text.",
            image_url="Direct HTTPS image URL; leave blank to keep the current image.",
        )
        async def service_desk_edit(
            interaction: discord.Interaction,
            title: str | None = None,
            description: str | None = None,
            image_url: str | None = None,
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can update the Service Desk.", ephemeral=True
                )
                return
            if not any((title, description, image_url)):
                await interaction.response.send_message(
                    "Provide at least one item to update: title, description, or image URL.", ephemeral=True
                )
                return
            if title is not None and (not title.strip() or len(title.strip()) > 256):
                await interaction.response.send_message(
                    "The Service Desk title must contain between 1 and 256 characters.", ephemeral=True
                )
                return
            if description is not None and (not description.strip() or len(description.strip()) > 4096):
                await interaction.response.send_message(
                    "The directory introduction must contain between 1 and 4,096 characters.", ephemeral=True
                )
                return
            if image_url is not None and not re.fullmatch(r"https://\S{1,2040}", image_url.strip()):
                await interaction.response.send_message(
                    "Provide a direct HTTPS image URL, or use `/service-desk clear target:image` to remove the image.",
                    ephemeral=True,
                )
                return
            config = await self._load_service_desk_config()
            if title is not None:
                config["title"] = title.strip()
            if description is not None:
                config["description"] = description.strip()
            if image_url is not None:
                config["image_url"] = image_url.strip()
            await publish_service_desk_update(interaction, config)

        @service_desk.command(
            name="form-link",
            description="Add or replace one of four case-management form links.",
        )
        @app_commands.describe(slot="Which form-link position to change.", label="Visible link label.", url="Direct HTTPS form URL.")
        @app_commands.choices(slot=[app_commands.Choice(name=str(index), value=index) for index in range(1, 5)])
        async def service_desk_form_link(
            interaction: discord.Interaction,
            slot: app_commands.Choice[int],
            label: str,
            url: str,
        ) -> None:
            await self._set_service_desk_link(
                interaction, key="form_links", slot=slot.value, label=label, url=url
            )

        @service_desk.command(
            name="external-link",
            description="Add or replace one of four SOP or external-resource links.",
        )
        @app_commands.describe(slot="Which external-link position to change.", label="Visible link label.", url="Direct HTTPS URL.")
        @app_commands.choices(slot=[app_commands.Choice(name=str(index), value=index) for index in range(1, 5)])
        async def service_desk_external_link(
            interaction: discord.Interaction,
            slot: app_commands.Choice[int],
            label: str,
            url: str,
        ) -> None:
            await self._set_service_desk_link(
                interaction, key="external_links", slot=slot.value, label=label, url=url
            )

        @service_desk.command(
            name="remove-link",
            description="Remove one configured Service Desk link.",
        )
        @app_commands.describe(link_type="Whether this is a form or external resource.", slot="Which link position to remove.")
        @app_commands.choices(
            link_type=[
                app_commands.Choice(name="Case-management form", value="form_links"),
                app_commands.Choice(name="SOP or external resource", value="external_links"),
            ],
            slot=[app_commands.Choice(name=str(index), value=index) for index in range(1, 5)],
        )
        async def service_desk_remove_link(
            interaction: discord.Interaction,
            link_type: app_commands.Choice[str],
            slot: app_commands.Choice[int],
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can update the Service Desk.", ephemeral=True
                )
                return
            config = await self._load_service_desk_config()
            links = config[link_type.value]
            assert isinstance(links, list)
            links[slot.value - 1] = None
            await publish_service_desk_update(interaction, config)

        @service_desk.command(name="clear", description="Remove the Service Desk image or directory introduction.")
        @app_commands.describe(target="The portion of the Service Desk embed to remove.")
        @app_commands.choices(
            target=[
                app_commands.Choice(name="Image", value="image_url"),
                app_commands.Choice(name="Directory introduction", value="description"),
            ]
        )
        async def service_desk_clear(
            interaction: discord.Interaction, target: app_commands.Choice[str]
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can update the Service Desk.", ephemeral=True
                )
                return
            config = await self._load_service_desk_config()
            config[target.value] = None
            await publish_service_desk_update(interaction, config)

        @service_desk.command(name="preview", description="View the current Service Desk embed privately.")
        async def service_desk_preview(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can view the Service Desk configuration.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                embed=self._service_desk_embed(await self._load_service_desk_config()), ephemeral=True
            )

        @service_desk.command(
            name="adopt",
            description="Use an existing bot-authored Service Desk post for future in-place updates.",
        )
        @app_commands.describe(message_link="Full Discord message link for the bot-authored Service Desk embed.")
        async def service_desk_adopt(interaction: discord.Interaction, message_link: str) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can update the Service Desk.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await self._adopt_service_desk_message(message_link)
                channel, _ = await self._publish_service_desk(await self._load_service_desk_config())
            except discord.Forbidden:
                await interaction.followup.send(
                    "The bot needs permission to view #service-desk, read its message history, and send/edit messages there.",
                    ephemeral=True,
                )
                return
            except (RuntimeError, TypeError, ValueError) as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            await interaction.followup.send(
                f"The existing managed Service Desk post in {channel.mention} is now configured for in-place updates.",
                ephemeral=True,
            )

        @self.tree.command(
            name="google-workspace-status",
            description="Verify Google access and create the request tracker if needed.",
            guild=guild,
        )
        async def google_workspace_status(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can run this command.", ephemeral=True
                )
                return
            if not self.settings.google_service_account_file:
                await interaction.response.send_message(
                    "The protected Google service-account file path is not configured on the host.",
                    ephemeral=True,
                )
                return
            folder_id = self.environment.google_workspace.drive_folder_id
            if not folder_id:
                await interaction.response.send_message(
                    "The Google Drive folder ID is not configured for this environment.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                tracker = await asyncio.to_thread(
                    GoogleWorkspaceService(
                        self.settings.google_service_account_file, folder_id
                    ).ensure_tracking_sheet
                )
            except Exception:
                LOGGER.exception("Google Workspace verification failed")
                await interaction.followup.send(
                    "Google Workspace setup could not be verified. Check the Wispbyte console "
                    "for the safe diagnostic message.",
                    ephemeral=True,
                )
                return

            action = "Created" if tracker.created else "Found"
            await interaction.followup.send(
                f"{action} the request tracker: {tracker.url}", ephemeral=True
            )

        @self.tree.command(
            name="court-order-sync",
            description="Import new Court Order Form submissions into private DOJ tickets.",
            guild=guild,
        )
        async def court_order_sync(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can run this command.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                created = await self._sync_court_order_responses()
            except Exception:
                LOGGER.exception("Court Order Form synchronization failed")
                await interaction.followup.send(
                    "Court Order Form synchronization failed. Check the Wispbyte console for details.",
                    ephemeral=True,
                )
                return
            if created:
                await interaction.followup.send(
                    f"Created {created} private Court Order request ticket(s).", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "No new Court Order Form submissions were found.", ephemeral=True
                )

        @self.tree.command(
            name="refresh-court-order",
            description="Refresh this active Court Order ticket from its original Google Form response.",
            guild=guild,
        )
        async def refresh_court_order(interaction: discord.Interaction) -> None:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Run this command inside the active private Court Order ticket to refresh.",
                    ephemeral=True,
                )
                return
            if not self._can_refresh_court_order(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator, Judge, or Attorney General can refresh a Court Order ticket.",
                    ephemeral=True,
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.workflow != "court_order":
                await interaction.response.send_message(
                    "This channel is not an active Court Order request ticket.", ephemeral=True
                )
                return
            if submission.status == "closed":
                await interaction.response.send_message(
                    "Closed requests cannot be refreshed because their case record is already finalized.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                refreshed = await self._refresh_court_order_submission(submission)
            except Exception:
                LOGGER.exception("Court Order ticket refresh failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The original Form response could not be refreshed. Check the Wispbyte console for the safe diagnostic message.",
                    ephemeral=True,
                )
                return
            if not refreshed:
                await interaction.followup.send(
                    "The original Form response for this ticket could not be found. No ticket data was changed.",
                    ephemeral=True,
                )
                return

            await self._post_court_order_intake(
                interaction.channel,
                refreshed,
                refreshed=True,
            )
            await interaction.followup.send(
                f"Refreshed **{refreshed.request_id}** from its original Form response and posted a new intake record in this ticket.",
                ephemeral=True,
            )

        @self.tree.command(
            name="court-order-baseline",
            description="Mark current Court Order Form responses as historical without creating tickets.",
            guild=guild,
        )
        async def court_order_baseline(interaction: discord.Interaction) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can run this command.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                marked = await self._baseline_court_order_responses()
            except Exception:
                LOGGER.exception("Court Order Form baseline failed")
                await interaction.followup.send(
                    "The Court Order Form baseline failed. Check the Wispbyte console for details.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Marked {marked} existing Court Order response(s) as historical. "
                "Future submissions can now be imported safely.",
                ephemeral=True,
            )

        @self.tree.command(
            name="claim-court-order",
            description="Claim your latest Court Order request using your Discord username.",
            guild=guild,
        )
        async def claim_court_order(interaction: discord.Interaction) -> None:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Run this command inside the private Court Order ticket you want to claim.",
                    ephemeral=True,
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.workflow != "court_order":
                await interaction.response.send_message(
                    "This channel is not a Court Order request ticket.",
                    ephemeral=True,
                )
                return
            if submission.claimed_user_id:
                await interaction.response.send_message(
                    "This Court Order request has already been claimed.", ephemeral=True
                )
                return
            username = self._normalize_username(interaction.user.name)
            if username != self._normalize_username(submission.requester_username):
                await interaction.response.send_message(
                    "Your Discord username does not match the username submitted with this request.",
                    ephemeral=True,
                )
                return
            channel = interaction.channel
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await channel.set_permissions(
                    interaction.user,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )
                await channel.edit(
                    name=self._court_order_ticket_name(submission, interaction.user),
                    reason=f"NERP - Case Management verified claimant for {submission.request_id}",
                )
                await self.store.mark_claimed(submission.id, interaction.user.id)
                if submission.tracker_spreadsheet_id and submission.tracker_row:
                    await asyncio.to_thread(
                        GoogleWorkspaceService(
                            self.settings.google_service_account_file or "",
                            self.environment.google_workspace.drive_folder_id or "",
                        ).mark_tracking_row_claimed,
                        TrackingRow(submission.tracker_spreadsheet_id, submission.tracker_row),
                        interaction.user.id,
                    )
                await channel.send(
                    f"{interaction.user.mention} has verified ownership of this Court Order request. "
                    "Please upload any documents, screenshots, media, or evidence directly in this ticket."
                )
            except discord.Forbidden:
                LOGGER.warning("The bot lacks permission to grant claimant access to %s", channel.id)
                await interaction.followup.send(
                    "The bot cannot yet grant access to this private ticket. A DOJ administrator "
                    "must give the bot role the Discord **Manage Roles** permission, then retry.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Your private Court Order request is ready: {channel.mention}", ephemeral=True
            )

        @self.tree.command(
            name="add-ticket-member",
            description="Give a server member access to this private NERP request ticket.",
            guild=guild,
        )
        @app_commands.describe(member="The server member who should be allowed into this ticket.")
        async def add_ticket_member(
            interaction: discord.Interaction,
            member: discord.Member,
        ) -> None:
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Run this command inside a private NERP request ticket.", ephemeral=True
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission:
                await interaction.response.send_message(
                    "This channel is not a bot-managed private NERP request ticket.", ephemeral=True
                )
                return
            if not (
                self._is_administrator(interaction)
                or interaction.user.id == submission.claimed_user_id
            ):
                await interaction.response.send_message(
                    "Only the verified requester or a configured bot administrator can add a member.",
                    ephemeral=True,
                )
                return
            if member.bot:
                await interaction.response.send_message(
                    "Bot accounts cannot be added through this command.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await interaction.channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )
            except discord.Forbidden:
                LOGGER.warning("The bot lacks permission to add %s to ticket %s", member.id, interaction.channel.id)
                await interaction.followup.send(
                    "The bot cannot change access for this ticket. A DOJ administrator must give the "
                    "bot role Discord **Manage Roles** permission, then retry.",
                    ephemeral=True,
                )
                return

            await interaction.channel.send(
                f"{interaction.user.mention} granted {member.mention} access to "
                f"NERP request **{submission.request_id}**."
            )
            await interaction.followup.send(
                f"{member.mention} can now view, message, and attach files in {interaction.channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="assign-case",
            description="Assign or reassign one of two Prosecutor, Judge, defense, or PD officer slots.",
            guild=guild,
        )
        @app_commands.describe(
            assignment_type="The optional role being assigned.",
            assignment_slot="The first or second record slot for that role.",
            member="The eligible server member who will hold that role.",
        )
        @app_commands.choices(
            assignment_type=[
                app_commands.Choice(name="Prosecutor", value="prosecutor"),
                app_commands.Choice(name="Judge", value="judge"),
                app_commands.Choice(name="Defense Attorney", value="defense_attorney"),
                app_commands.Choice(name="PD Officer", value="pd_officer"),
            ]
        )
        @app_commands.choices(
            assignment_slot=[
                app_commands.Choice(name="Slot 1", value=1),
                app_commands.Choice(name="Slot 2", value=2),
            ]
        )
        async def assign_case(
            interaction: discord.Interaction,
            assignment_type: app_commands.Choice[str],
            assignment_slot: app_commands.Choice[int],
            member: discord.Member,
        ) -> None:
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "Run this command inside a bot-managed case, Docket entry, or Court Order ticket.",
                    ephemeral=True,
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.status == "closed":
                await interaction.response.send_message(
                    "This is not an active bot-managed case or request.", ephemeral=True
                )
                return
            if member.bot or not self._member_can_fill_assignment(member, assignment_type.value):
                await interaction.response.send_message(
                    f"{member.mention} does not hold an eligible {self._assignment_label(assignment_type.value)} role.",
                    ephemeral=True,
                )
                return
            self_assignment = interaction.user.id == member.id
            if not self_assignment and not self._can_manage_case_assignments(interaction):
                await interaction.response.send_message(
                    "Only DOJ/PD Command or a configured bot administrator can assign another member. "
                    "Eligible staff may assign themselves.",
                    ephemeral=True,
                )
                return
            if self_assignment and not self._member_can_fill_assignment(
                interaction.user, assignment_type.value
            ):
                await interaction.response.send_message(
                    "You do not hold the role you are trying to self-assign.", ephemeral=True
                )
                return
            current_assignments = await self.store.get_case_assignments(submission.id)
            selected_slot = next(
                (
                    item
                    for item in current_assignments
                    if item.assignment_type == assignment_type.value
                    and item.assignment_slot == assignment_slot.value
                ),
                None,
            )
            if self_assignment and selected_slot and selected_slot.user_id != interaction.user.id:
                await interaction.response.send_message(
                    f"{self._assignment_label(assignment_type.value)} {assignment_slot.value} is already "
                    "assigned. Choose the other slot or ask DOJ/PD Command to reassign it.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            assigned_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            actor_name = interaction.user.display_name.strip() or interaction.user.name
            await self.store.set_case_assignment(
                submission_id=submission.id,
                assignment_type=assignment_type.value,
                assignment_slot=assignment_slot.value,
                user_id=member.id,
                user_name=member.display_name,
                assigned_by_user_id=interaction.user.id,
                assigned_by_name=actor_name,
                assigned_at=assigned_at,
            )
            await interaction.channel.send(
                f"{interaction.user.mention} assigned {member.mention} as **{self._assignment_label(assignment_type.value)} "
                f"{assignment_slot.value}** "
                f"for **{submission.request_id}**.",
                embed=await self._case_assignment_embed(submission),
            )
            await interaction.followup.send("Assignment recorded.", ephemeral=True)

        @self.tree.command(
            name="transfer-case-assignment",
            description="Offer one of your assigned case-role slots to another eligible staff member.",
            guild=guild,
        )
        @app_commands.describe(
            assignment_type="The assigned role being handed off.",
            assignment_slot="The first or second role slot being handed off.",
            member="The eligible staff member who must accept this transfer.",
        )
        @app_commands.choices(
            assignment_type=[
                app_commands.Choice(name="Prosecutor", value="prosecutor"),
                app_commands.Choice(name="Judge", value="judge"),
                app_commands.Choice(name="Defense Attorney", value="defense_attorney"),
                app_commands.Choice(name="PD Officer", value="pd_officer"),
            ]
        )
        @app_commands.choices(
            assignment_slot=[
                app_commands.Choice(name="Slot 1", value=1),
                app_commands.Choice(name="Slot 2", value=2),
            ]
        )
        async def transfer_case_assignment(
            interaction: discord.Interaction,
            assignment_type: app_commands.Choice[str],
            assignment_slot: app_commands.Choice[int],
            member: discord.Member,
        ) -> None:
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "Run this command inside the active case or request being transferred.", ephemeral=True
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.status == "closed":
                await interaction.response.send_message(
                    "This is not an active bot-managed case or request.", ephemeral=True
                )
                return
            if member.bot or member.id == interaction.user.id:
                await interaction.response.send_message(
                    "Choose another eligible server member to receive the transfer.", ephemeral=True
                )
                return
            if not self._member_can_fill_assignment(member, assignment_type.value):
                await interaction.response.send_message(
                    f"{member.mention} does not hold an eligible {self._assignment_label(assignment_type.value)} role.",
                    ephemeral=True,
                )
                return
            assignments = await self.store.get_case_assignments(submission.id)
            current = next(
                (
                    item
                    for item in assignments
                    if item.assignment_type == assignment_type.value
                    and item.assignment_slot == assignment_slot.value
                ),
                None,
            )
            if not current:
                await interaction.response.send_message(
                    f"There is no assigned {self._assignment_label(assignment_type.value)} {assignment_slot.value} to transfer yet.",
                    ephemeral=True,
                )
                return
            if current.user_id != interaction.user.id and not self._can_manage_case_assignments(interaction):
                await interaction.response.send_message(
                    "Only the currently assigned staff member, DOJ/PD Command, or a configured bot administrator can offer this transfer.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            actor_name = interaction.user.display_name.strip() or interaction.user.name
            await self.store.propose_assignment_transfer(
                submission_id=submission.id,
                assignment_type=assignment_type.value,
                assignment_slot=assignment_slot.value,
                from_user_id=current.user_id,
                from_user_name=current.user_name,
                to_user_id=member.id,
                to_user_name=member.display_name,
                proposed_by_user_id=interaction.user.id,
                proposed_by_name=actor_name,
                proposed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
            await interaction.channel.send(
                f"{member.mention}, {interaction.user.mention} offered you the **{self._assignment_label(assignment_type.value)} "
                f"{assignment_slot.value}** "
                f"assignment for **{submission.request_id}**. Run `/accept-case-transfer` in this ticket to accept it.",
            )
            await interaction.followup.send(
                "Transfer offer recorded. The assignment will not change until the recipient accepts it.",
                ephemeral=True,
            )

        @self.tree.command(
            name="accept-case-transfer",
            description="Accept a pending Prosecutor, Judge, defense, or PD officer transfer in this ticket.",
            guild=guild,
        )
        @app_commands.describe(
            assignment_type="The pending role transfer you are accepting.",
            assignment_slot="The first or second role slot in the pending transfer.",
        )
        @app_commands.choices(
            assignment_type=[
                app_commands.Choice(name="Prosecutor", value="prosecutor"),
                app_commands.Choice(name="Judge", value="judge"),
                app_commands.Choice(name="Defense Attorney", value="defense_attorney"),
                app_commands.Choice(name="PD Officer", value="pd_officer"),
            ]
        )
        @app_commands.choices(
            assignment_slot=[
                app_commands.Choice(name="Slot 1", value=1),
                app_commands.Choice(name="Slot 2", value=2),
            ]
        )
        async def accept_case_transfer(
            interaction: discord.Interaction,
            assignment_type: app_commands.Choice[str],
            assignment_slot: app_commands.Choice[int],
        ) -> None:
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "Run this command inside the ticket containing your transfer offer.", ephemeral=True
                )
                return
            if not isinstance(interaction.user, discord.Member) or not self._member_can_fill_assignment(
                interaction.user, assignment_type.value
            ):
                await interaction.response.send_message(
                    "You do not hold the role required to accept that transfer.", ephemeral=True
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.status == "closed":
                await interaction.response.send_message(
                    "This is not an active bot-managed case or request.", ephemeral=True
                )
                return
            transfer = await self.store.get_pending_assignment_transfer(
                submission_id=submission.id,
                assignment_type=assignment_type.value,
                assignment_slot=assignment_slot.value,
                recipient_user_id=interaction.user.id,
            )
            if not transfer:
                await interaction.response.send_message(
                    "You do not have a pending transfer for that role in this ticket.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            accepted = await self.store.accept_assignment_transfer(
                transfer,
                accepted_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            )
            if not accepted:
                await interaction.followup.send(
                    "That transfer is no longer pending. No assignment was changed.", ephemeral=True
                )
                return
            await interaction.channel.send(
                f"{interaction.user.mention} accepted the **{self._assignment_label(assignment_type.value)} "
                f"{assignment_slot.value}** "
                f"transfer for **{submission.request_id}**.",
                embed=await self._case_assignment_embed(submission),
            )
            await interaction.followup.send("Transfer accepted and assignment updated.", ephemeral=True)

        @self.tree.command(
            name="rename-docket",
            description="Change the title of this active Docket Forum post.",
            guild=guild,
        )
        @app_commands.describe(title="The new Docket title, up to 100 characters.")
        async def rename_docket(interaction: discord.Interaction, title: str) -> None:
            if not isinstance(interaction.channel, discord.Thread):
                await interaction.response.send_message(
                    "Run this command inside the Docket Forum post you want to rename.", ephemeral=True
                )
                return
            if not self._can_merge_court_order(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator, Judge, or Attorney General can rename a Docket.",
                    ephemeral=True,
                )
                return
            try:
                docket_thread = await self._resolve_docket_thread(str(interaction.channel.id))
            except ValueError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            new_title = title.strip()
            if not new_title or len(new_title) > 100:
                await interaction.response.send_message(
                    "The Docket title must contain between 1 and 100 characters.", ephemeral=True
                )
                return
            old_title = docket_thread.name
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await docket_thread.edit(
                    name=new_title,
                    reason=f"NERP - Case Management Docket rename by {interaction.user}",
                )
                await docket_thread.send(
                    f"{interaction.user.mention} renamed this Docket from **{old_title}** to **{new_title}**."
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "The bot needs access to this Docket and **Manage Threads** permission to rename it.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(f"Renamed this Docket to **{new_title}**.", ephemeral=True)

        @self.tree.command(
            name="import-court-order",
            description="Import an off-docket Court Order into this Docket Forum post.",
            guild=guild,
        )
        @app_commands.describe(
            court_order_id="The Court Order ID, for example COR-000007.",
            disposition="Keep the original Court Order open or close it after a successful import.",
        )
        @app_commands.choices(
            disposition=[
                app_commands.Choice(name="Keep original Court Order open", value="keep_open"),
                app_commands.Choice(name="Import and close original Court Order", value="close"),
            ]
        )
        async def import_court_order(
            interaction: discord.Interaction,
            court_order_id: str,
            disposition: app_commands.Choice[str],
        ) -> None:
            if not isinstance(interaction.channel, discord.Thread):
                await interaction.response.send_message(
                    "Run this command inside the destination Docket Forum post.", ephemeral=True
                )
                return
            if not self._can_merge_court_order(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator, Judge, or Attorney General can import a Court Order.",
                    ephemeral=True,
                )
                return
            try:
                docket_thread = await self._resolve_docket_thread(str(interaction.channel.id))
            except ValueError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            match = re.fullmatch(r"\s*(COR-\d{6})\s*", court_order_id, flags=re.IGNORECASE)
            if not match:
                await interaction.response.send_message(
                    "Provide the bot-issued Court Order ID in the format `COR-000007`.", ephemeral=True
                )
                return
            submission = await self.store.get_by_request_id(match.group(1))
            if not submission or submission.workflow != "court_order" or submission.status == "closed":
                await interaction.response.send_message(
                    "That is not an active bot-managed Court Order available for import.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self._merge_court_order_into_docket(
                    submission_id=submission.id,
                    docket_thread_id=docket_thread.id,
                    close_source=disposition.value == "close",
                    actor=interaction.user,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "The bot needs permission to read the Court Order and send messages in this Docket post. "
                    "When closing, it also needs permission to manage the original private ticket.",
                    ephemeral=True,
                )
                return
            except (CourtOrderMergeError, RuntimeError, ValueError) as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Manual Court Order import failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The Court Order could not be imported. Check the Wispbyte console for the safe diagnostic message.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(result, ephemeral=True)

        @self.tree.command(
            name="merge-court-order-to-docket",
            description="Merge a Court Order into a Docket using its case record ID.",
            guild=guild,
        )
        @app_commands.describe(
            court_order_id="Court Order ID when this is run in the destination Docket, e.g. COR-000007.",
            docket_id="Docket ID when this is run in the source Court Order, e.g. DCK-000010.",
        )
        async def merge_court_order_to_docket(
            interaction: discord.Interaction,
            court_order_id: str | None = None,
            docket_id: str | None = None,
        ) -> None:
            if not self._can_merge_court_order(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator, Judge, or Attorney General can merge a Court Order.",
                    ephemeral=True,
                )
                return
            channel = interaction.channel
            if isinstance(channel, discord.TextChannel):
                submission = await self.store.get_by_channel_id(channel.id)
                if not submission or submission.workflow != "court_order" or submission.status == "closed":
                    await interaction.response.send_message(
                        "This is not an active Court Order ticket.", ephemeral=True
                    )
                    return
                off_docket_category = await self._resolve_category(
                    "off_docket_tickets_category", "off_docket_tickets"
                )
                if channel.category_id != off_docket_category.id:
                    await interaction.response.send_message(
                        "Only an off-docket Court Order ticket can be merged into a Docket entry.",
                        ephemeral=True,
                    )
                    return
                if not docket_id:
                    await interaction.response.send_message(
                        "Run this in a Court Order ticket with the destination Docket ID, for example "
                        "`/merge-court-order-to-docket docket_id:DCK-000010`.",
                        ephemeral=True,
                    )
                    return
                try:
                    docket_thread = await self._resolve_docket_thread_by_request_id(docket_id)
                except ValueError as error:
                    await interaction.response.send_message(str(error), ephemeral=True)
                    return
            elif isinstance(channel, discord.Thread):
                try:
                    docket_thread = await self._resolve_docket_thread(str(channel.id))
                except ValueError:
                    await interaction.response.send_message(
                        "Run this command inside an active Docket Forum post or an off-docket Court Order ticket.",
                        ephemeral=True,
                    )
                    return
                if not court_order_id:
                    await interaction.response.send_message(
                        "Run this in a Docket post with the Court Order ID, for example "
                        "`/merge-court-order-to-docket court_order_id:COR-000007`.",
                        ephemeral=True,
                    )
                    return
                try:
                    submission = await self._resolve_active_court_order(court_order_id)
                except ValueError as error:
                    await interaction.response.send_message(str(error), ephemeral=True)
                    return
            else:
                await interaction.response.send_message(
                    "Run this command inside the source Court Order ticket or the destination Docket Forum post.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self._merge_court_order_into_docket(
                    submission_id=submission.id,
                    docket_thread_id=docket_thread.id,
                    close_source=False,
                    actor=interaction.user,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "The bot needs permission to read the Court Order and send messages in the Docket post. "
                    "Confirm its role permissions and retry.",
                    ephemeral=True,
                )
                return
            except (CourtOrderMergeError, RuntimeError, ValueError) as error:
                await interaction.followup.send(str(error), ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Court Order merge failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The Court Order could not be merged. Check the Wispbyte console for the safe diagnostic message.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(result, ephemeral=True)

        @self.tree.command(
            name="close-ticket",
            description="Close this NERP request and add a staff-only case record.",
            guild=guild,
        )
        @app_commands.describe(
            closure_note="Optional short disposition or closing note for the staff case record."
        )
        async def close_ticket(
            interaction: discord.Interaction,
            closure_note: str | None = None,
        ) -> None:
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message(
                    "Run this command inside a bot-managed NERP request ticket or Forum post.",
                    ephemeral=True,
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission:
                await interaction.response.send_message(
                    "This channel is not a bot-managed NERP request ticket or Forum post.",
                    ephemeral=True,
                )
                return
            if submission.status == "closed":
                await interaction.response.send_message(
                    "This request is already closed.", ephemeral=True
                )
                return
            if not self._can_close_ticket(interaction, submission):
                await interaction.response.send_message(
                    "Only the verified requester, a configured Judge, the Attorney General, or a "
                    "server administrator can close this request.",
                    ephemeral=True,
                )
                return
            if closure_note and len(closure_note) > 1000:
                await interaction.response.send_message(
                    "The optional closure note must be 1,000 characters or fewer.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            closer_name = interaction.user.display_name.strip() or interaction.user.name
            closed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            try:
                records_channel = await self._get_or_create_case_records_channel()
                for record_embed in await self._case_record_embeds(
                    submission=submission,
                    closed_by=interaction.user,
                    closed_at=closed_at,
                    closure_note=closure_note,
                    source_deleted=isinstance(interaction.channel, discord.TextChannel),
                ):
                    await records_channel.send(embed=record_embed)
                await self._archive_closed_form_response(
                    submission=submission,
                    closed_by=closer_name,
                    closed_at=closed_at,
                    closure_note=closure_note,
                )
                recorded = await self.store.mark_closed(
                    submission_id=submission.id,
                    closer_user_id=interaction.user.id,
                    closer_name=closer_name,
                    closed_at=closed_at,
                    closed_note=closure_note,
                )
                await self._finalize_request_channel(interaction.channel, submission)
            except discord.Forbidden:
                LOGGER.warning("The bot lacks permission to close request %s", submission.request_id)
                await interaction.followup.send(
                    "The bot lacks a required Discord permission to create the staff record or close "
                    "this request. Confirm its role has **Manage Channels**, **Manage Roles**, and "
                    "**Manage Threads** permissions, then retry.",
                    ephemeral=True,
                )
                return
            except Exception:
                LOGGER.exception("Ticket closure failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The request could not be closed. Check the Wispbyte console for the safe "
                    "diagnostic message.",
                    ephemeral=True,
                )
                return

            if not recorded:
                LOGGER.warning("Closure record already exists for %s", submission.request_id)
                await interaction.followup.send(
                    "A case record was posted, but this request had already been marked closed. "
                    "Please have DOJ staff review the records channel.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"Closed **{submission.request_id}** and posted its staff record in "
                f"{records_channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="generate-arrest-warrant",
            description="Create a one-page Arrest Warrant from a claimed Court Order request.",
            guild=guild,
        )
        @app_commands.describe(
            request_id="Court Order request ID, for example COR-000001.",
        )
        async def generate_arrest_warrant(
            interaction: discord.Interaction,
            request_id: str,
        ) -> None:
            warrant_config = self.environment.court_order_warrant
            if not warrant_config or not self.settings.google_service_account_file:
                await interaction.response.send_message(
                    "The Arrest Warrant template is not configured on this host yet.", ephemeral=True
                )
                return
            submission = await self.store.get_by_request_id(request_id)
            if not submission or submission.workflow != "court_order":
                await interaction.response.send_message(
                    "That Court Order request could not be found.", ephemeral=True
                )
                return
            if not self._is_arrest_warrant(submission):
                await interaction.response.send_message(
                    "This request is not an Arrest Warrant. Its selected Court Order type is "
                    f"**{self._request_type(submission) or 'not recognized'}**; player-facing "
                    "document automation for that type has not been configured yet.",
                    ephemeral=True,
                )
                return
            if interaction.channel_id != submission.discord_channel_id:
                await interaction.response.send_message(
                    "Run this command inside the private ticket for that Court Order request.",
                    ephemeral=True,
                )
                return
            if not submission.claimed_user_id:
                await interaction.response.send_message(
                    "The requester must first verify ownership with `/claim-court-order`.",
                    ephemeral=True,
                )
                return
            if not (
                self._is_administrator(interaction) or interaction.user.id == submission.claimed_user_id
            ):
                await interaction.response.send_message(
                    "Only the verified requester or a configured bot administrator can generate this warrant.",
                    ephemeral=True,
                )
                return
            channel = self.get_channel(submission.discord_channel_id or 0)
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    "The private request ticket could not be located.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            replacements, validation_error = self._arrest_warrant_replacements(submission=submission)
            if validation_error:
                await self._notify_warrant_fit_failure(channel, submission, validation_error)
                await interaction.followup.send(
                    "No warrant was issued. The requester was notified in their private ticket.",
                    ephemeral=True,
                )
                return

            try:
                warrant = await asyncio.to_thread(
                    GoogleWorkspaceService(
                        self.settings.google_service_account_file,
                        self.environment.google_workspace.drive_folder_id or "",
                    ).generate_arrest_warrant,
                    template_document_id=warrant_config.template_document_id,
                    output_drive_folder_id=warrant_config.output_drive_folder_id,
                    request_id=submission.request_id,
                    replacements=replacements,
                )
            except Exception:
                LOGGER.exception("Arrest Warrant generation failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The warrant could not be generated. Check the Wispbyte console for the safe "
                    "diagnostic message.",
                    ephemeral=True,
                )
                return

            if warrant.page_count != 1:
                await self._notify_warrant_fit_failure(
                    channel,
                    submission,
                    "the completed warrant would exceed the required one-page limit",
                )
                await interaction.followup.send(
                    "No player-facing warrant was issued because the completed document is not "
                    "exactly one page. The requester was notified in their private ticket.",
                    ephemeral=True,
                )
                return

            filename = f"arrest-warrant-{submission.request_id.lower()}.png"
            fivemanage_url: str | None = None
            if self.settings.fivemanage_api_token:
                try:
                    upload = await asyncio.to_thread(
                        FiveManageService(
                            self.settings.fivemanage_api_token,
                            self.settings.fivemanage_storage_path,
                        ).upload_png,
                        filename=filename,
                        content=warrant.png_bytes,
                        request_id=submission.request_id,
                    )
                    fivemanage_url = upload.url
                except Exception:
                    LOGGER.exception("FiveManage upload failed for %s", submission.request_id)

            message = (
                f"One-page Arrest Warrant generated for **{submission.request_id}**. "
                f"Internal document record: {warrant.document_url}"
            )
            if fivemanage_url:
                message += f"\nFiveManage PNG URL: {fivemanage_url}"
            elif self.settings.fivemanage_api_token:
                message += "\nFiveManage upload was unavailable; the PNG is attached here."
            await channel.send(
                message,
                file=discord.File(BytesIO(warrant.png_bytes), filename=filename),
            )
            await interaction.followup.send(
                f"Issued the one-page warrant in {channel.mention}.", ephemeral=True
            )

        @self.tree.command(
            name="approve-arrest-warrant",
            description="Judicially approve the Arrest Warrant in this private ticket.",
            guild=guild,
        )
        async def approve_arrest_warrant(interaction: discord.Interaction) -> None:
            if not self._can_approve_warrant(interaction):
                await interaction.response.send_message(
                    "Only a configured Judge or bot administrator can approve an Arrest Warrant.",
                    ephemeral=True,
                )
                return
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Run this command inside the private Court Order ticket.", ephemeral=True
                )
                return
            warrant_config = self.environment.court_order_warrant
            if not warrant_config or not self.settings.google_service_account_file:
                await interaction.response.send_message(
                    "The Arrest Warrant template is not configured on this host yet.", ephemeral=True
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.workflow != "court_order":
                await interaction.response.send_message(
                    "This channel is not a Court Order request ticket.", ephemeral=True
                )
                return
            if not self._is_arrest_warrant(submission):
                await interaction.response.send_message(
                    "This request is not an Arrest Warrant, so it cannot use the Arrest Warrant "
                    "approval command. Its document workflow has not been configured yet.",
                    ephemeral=True,
                )
                return
            if not submission.claimed_user_id:
                await interaction.response.send_message(
                    "The requester must verify ownership before judicial approval.", ephemeral=True
                )
                return
            if submission.status == "approved":
                await interaction.response.send_message(
                    "This Arrest Warrant has already been approved and cannot be approved twice.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            replacements, validation_error = self._arrest_warrant_replacements(submission=submission)
            if validation_error:
                await self._notify_warrant_fit_failure(interaction.channel, submission, validation_error)
                await interaction.followup.send(
                    "No warrant was approved. The requester was notified in their private ticket.",
                    ephemeral=True,
                )
                return
            approver_name = interaction.user.display_name.strip() or interaction.user.name
            approved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            issue_date = datetime.now(UTC).strftime("%B %d, %Y").replace(" 0", " ")
            replacements.update(
                {
                    # The native template owns the literal "/s/" marker on its signature line
                    # and uses this same placeholder again for the printed judicial name below it.
                    "{{APPROVER_NAME}}": approver_name,
                    "{{ISSUE_DATE}}": issue_date,
                }
            )

            try:
                warrant = await asyncio.to_thread(
                    GoogleWorkspaceService(
                        self.settings.google_service_account_file,
                        self.environment.google_workspace.drive_folder_id or "",
                    ).generate_arrest_warrant,
                    template_document_id=warrant_config.template_document_id,
                    output_drive_folder_id=warrant_config.output_drive_folder_id,
                    request_id=submission.request_id,
                    replacements=replacements,
                    document_title=f"Approved Arrest Warrant {submission.request_id}",
                )
            except Exception:
                LOGGER.exception("Arrest Warrant approval generation failed for %s", submission.request_id)
                await interaction.followup.send(
                    "The approved warrant could not be generated. Check the Wispbyte console for the "
                    "safe diagnostic message.",
                    ephemeral=True,
                )
                return

            if warrant.page_count != 1:
                await self._notify_warrant_fit_failure(
                    interaction.channel,
                    submission,
                    "the approved warrant would exceed the required one-page limit",
                )
                await interaction.followup.send(
                    "No approved player-facing warrant was issued because the completed document "
                    "is not exactly one page.",
                    ephemeral=True,
                )
                return

            filename = f"approved-arrest-warrant-{submission.request_id.lower()}.png"
            fivemanage_url: str | None = None
            if self.settings.fivemanage_api_token:
                try:
                    upload = await asyncio.to_thread(
                        FiveManageService(
                            self.settings.fivemanage_api_token,
                            self.settings.fivemanage_storage_path,
                        ).upload_png,
                        filename=filename,
                        content=warrant.png_bytes,
                        request_id=submission.request_id,
                    )
                    fivemanage_url = upload.url
                except Exception:
                    LOGGER.exception("FiveManage upload failed for approved %s", submission.request_id)

            await interaction.channel.send(
                f"**Arrest Warrant approved** by {interaction.user.mention} on **{issue_date}**.\n"
                f"Internal approved document: {warrant.document_url}"
                + (f"\nFiveManage PNG URL: {fivemanage_url}" if fivemanage_url else ""),
                file=discord.File(BytesIO(warrant.png_bytes), filename=filename),
            )
            recorded = await self.store.mark_warrant_approved(
                submission_id=submission.id,
                approver_user_id=interaction.user.id,
                approver_name=approver_name,
                approved_at=approved_at,
                approved_document_id=warrant.document_id,
            )
            if not recorded:
                LOGGER.warning("Approval was posted but audit record was already approved for %s", submission.request_id)
            await interaction.followup.send(
                f"Approved and posted the signed one-page warrant in {interaction.channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="deny-arrest-warrant",
            description="Judicially deny this Arrest Warrant and record the required reason.",
            guild=guild,
        )
        @app_commands.describe(denial_notes="Required reason or corrective guidance for the denial.")
        async def deny_arrest_warrant(
            interaction: discord.Interaction,
            denial_notes: str,
        ) -> None:
            if not self._can_deny_warrant(interaction):
                await interaction.response.send_message(
                    "Only a configured Judge or bot administrator can deny an Arrest Warrant.",
                    ephemeral=True,
                )
                return
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Run this command inside the private Court Order ticket.", ephemeral=True
                )
                return
            denial_notes = denial_notes.strip()
            if not denial_notes or len(denial_notes) > 1000:
                await interaction.response.send_message(
                    "Denial notes are required and must be 1,000 characters or fewer.", ephemeral=True
                )
                return
            submission = await self.store.get_by_channel_id(interaction.channel.id)
            if not submission or submission.workflow != "court_order":
                await interaction.response.send_message(
                    "This channel is not a Court Order request ticket.", ephemeral=True
                )
                return
            if not self._is_arrest_warrant(submission):
                await interaction.response.send_message(
                    "This request is not an Arrest Warrant, so it cannot use the Arrest Warrant "
                    "denial command. Its document workflow has not been configured yet.",
                    ephemeral=True,
                )
                return
            if submission.status == "closed":
                await interaction.response.send_message(
                    "This request is already closed and cannot be denied.", ephemeral=True
                )
                return
            if submission.status == "approved":
                await interaction.response.send_message(
                    "This Arrest Warrant was already approved and cannot be denied.", ephemeral=True
                )
                return
            if submission.status == "denied":
                await interaction.response.send_message(
                    "This Arrest Warrant has already been denied. A corrected Form submission can "
                    "reference this request ID to continue here.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            denied_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            denier_name = interaction.user.display_name.strip() or interaction.user.name
            recorded = await self.store.mark_warrant_denied(
                submission_id=submission.id,
                denier_user_id=interaction.user.id,
                denier_name=denier_name,
                denied_at=denied_at,
                denial_note=denial_notes,
            )
            if not recorded:
                await interaction.followup.send(
                    "The denial could not be recorded because this request changed state. Please retry "
                    "only after staff review the ticket.",
                    ephemeral=True,
                )
                return
            await interaction.channel.send(
                f"**Arrest Warrant denied** by {interaction.user.mention}.\n"
                f"**Denial notes:** {denial_notes}\n\n"
                "This ticket remains open. To submit a correction, complete a new Court Order Form and "
                f"enter **{submission.request_id}** in the Docket / Off-Docket Name/ID field; the bot "
                "will post the new submission in this same ticket."
            )
            await interaction.followup.send(
                f"Denied **{submission.request_id}** and recorded the denial notes in this ticket.",
                ephemeral=True,
            )

        @self.tree.command(
            name="generate-search-seizure-warrant",
            description="Create a one-page Search / Seizure Warrant from a claimed Court Order request.",
            guild=guild,
        )
        @app_commands.describe(request_id="Court Order request ID, for example COR-000001.")
        async def generate_search_seizure_warrant(
            interaction: discord.Interaction, request_id: str
        ) -> None:
            await self._generate_search_seizure_warrant(interaction, request_id)

        @self.tree.command(
            name="approve-search-seizure-warrant",
            description="Judicially approve the Search / Seizure Warrant in this private ticket.",
            guild=guild,
        )
        async def approve_search_seizure_warrant(interaction: discord.Interaction) -> None:
            await self._approve_search_seizure_warrant(interaction)

        @self.tree.command(
            name="deny-search-seizure-warrant",
            description="Judicially deny this Search / Seizure Warrant and record the required reason.",
            guild=guild,
        )
        @app_commands.describe(denial_notes="Required reason or corrective guidance for the denial.")
        async def deny_search_seizure_warrant(
            interaction: discord.Interaction, denial_notes: str
        ) -> None:
            await self._deny_search_seizure_warrant(interaction, denial_notes)

        @self.tree.command(
            name="generate-subpoena",
            description="Create a one-page Subpoena from a claimed Court Order request.",
            guild=guild,
        )
        @app_commands.describe(request_id="Court Order request ID, for example COR-000001.")
        async def generate_subpoena(interaction: discord.Interaction, request_id: str) -> None:
            await self._generate_search_seizure_warrant(
                interaction, request_id, document_kind="subpoena"
            )

        @self.tree.command(
            name="approve-subpoena",
            description="Judicially approve the Subpoena in this private ticket.",
            guild=guild,
        )
        async def approve_subpoena(interaction: discord.Interaction) -> None:
            await self._approve_search_seizure_warrant(interaction, document_kind="subpoena")

        @self.tree.command(
            name="deny-subpoena",
            description="Judicially deny this Subpoena and record the required reason.",
            guild=guild,
        )
        @app_commands.describe(denial_notes="Required reason or corrective guidance for the denial.")
        async def deny_subpoena(interaction: discord.Interaction, denial_notes: str) -> None:
            await self._deny_search_seizure_warrant(
                interaction, denial_notes, document_kind="subpoena"
            )

        await self.tree.sync(guild=guild)
        self._court_order_poll_task = asyncio.create_task(self._court_order_poll_loop())

    async def _generate_search_seizure_warrant(
        self, interaction: discord.Interaction, request_id: str, *, document_kind: str = "search_seizure"
    ) -> None:
        config = self._court_order_document_config(document_kind)
        document_label = self._court_order_document_label(document_kind)
        if not config or not self.settings.google_service_account_file:
            await interaction.response.send_message(
                f"The {document_label} template is not configured on this host yet.",
                ephemeral=True,
            )
            return
        submission = await self.store.get_by_request_id(request_id)
        if not submission or submission.workflow != "court_order":
            await interaction.response.send_message(
                "That Court Order request could not be found.", ephemeral=True
            )
            return
        if not self._is_court_order_document_type(submission, document_kind):
            await interaction.response.send_message(
                f"This request is not a {document_label}. Its selected Court Order type is "
                f"**{self._request_type(submission) or 'not recognized'}**.",
                ephemeral=True,
            )
            return
        if interaction.channel_id != submission.discord_channel_id:
            await interaction.response.send_message(
                "Run this command inside the private ticket for that Court Order request.",
                ephemeral=True,
            )
            return
        if not submission.claimed_user_id:
            await interaction.response.send_message(
                "The requester must first verify ownership with `/claim-court-order`.",
                ephemeral=True,
            )
            return
        if not (self._is_administrator(interaction) or interaction.user.id == submission.claimed_user_id):
            await interaction.response.send_message(
                "Only the verified requester or a configured bot administrator can generate this warrant.",
                ephemeral=True,
            )
            return
        channel = self.get_channel(submission.discord_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "The private request ticket could not be located.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        replacements, validation_error = self._court_order_document_replacements(
            submission=submission, document_kind=document_kind
        )
        if validation_error:
            await self._notify_warrant_fit_failure(channel, submission, validation_error)
            await interaction.followup.send(
                "No warrant was issued. The requester was notified in their private ticket.",
                ephemeral=True,
            )
            return
        try:
            warrant = await asyncio.to_thread(
                GoogleWorkspaceService(
                    self.settings.google_service_account_file,
                    self.environment.google_workspace.drive_folder_id or "",
                ).generate_arrest_warrant,
                template_document_id=config.template_document_id,
                output_drive_folder_id=config.output_drive_folder_id,
                request_id=submission.request_id,
                replacements=replacements,
                document_title=f"{document_label} {submission.request_id}",
                required_placeholders=set(replacements),
            )
        except Exception:
            LOGGER.exception("%s generation failed for %s", document_label, submission.request_id)
            await interaction.followup.send(
                "The warrant could not be generated. Check the Wispbyte console for the safe "
                "diagnostic message.",
                ephemeral=True,
            )
            return
        if warrant.page_count != 1:
            await self._notify_warrant_fit_failure(
                channel, submission, "the completed warrant would exceed the required one-page limit"
            )
            await interaction.followup.send(
                "No player-facing warrant was issued because the completed document is not exactly one page.",
                ephemeral=True,
            )
            return
        await self._post_search_seizure_warrant(
            channel=channel, submission=submission, warrant=warrant, approved=False, document_kind=document_kind
        )
        await interaction.followup.send(
            f"Issued the one-page warrant in {channel.mention}.", ephemeral=True
        )

    async def _approve_search_seizure_warrant(
        self, interaction: discord.Interaction, *, document_kind: str = "search_seizure"
    ) -> None:
        document_label = self._court_order_document_label(document_kind)
        if not self._can_approve_warrant(interaction):
            await interaction.response.send_message(
                f"Only a configured Judge or bot administrator can approve a {document_label}.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run this command inside the private Court Order ticket.", ephemeral=True
            )
            return
        config = self._court_order_document_config(document_kind)
        if not config or not self.settings.google_service_account_file:
            await interaction.response.send_message(
                f"The {document_label} template is not configured on this host yet.",
                ephemeral=True,
            )
            return
        submission = await self.store.get_by_channel_id(interaction.channel.id)
        if not submission or submission.workflow != "court_order" or not self._is_court_order_document_type(submission, document_kind):
            await interaction.response.send_message(
                f"This channel does not contain a {document_label} request.", ephemeral=True
            )
            return
        if not submission.claimed_user_id:
            await interaction.response.send_message(
                "The requester must verify ownership before judicial approval.", ephemeral=True
            )
            return
        if submission.status == "approved":
            await interaction.response.send_message(
                f"This {document_label} has already been approved.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        replacements, validation_error = self._court_order_document_replacements(
            submission=submission, document_kind=document_kind
        )
        if validation_error:
            await self._notify_warrant_fit_failure(interaction.channel, submission, validation_error)
            await interaction.followup.send(
                "No warrant was approved. The requester was notified in their private ticket.",
                ephemeral=True,
            )
            return
        approver_name = interaction.user.display_name.strip() or interaction.user.name
        approved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        issue_date = datetime.now(UTC).strftime("%B %d, %Y").replace(" 0", " ")
        replacements.update({"{{APPROVER_NAME}}": approver_name, "{{ISSUE_DATE}}": issue_date})
        try:
            warrant = await asyncio.to_thread(
                GoogleWorkspaceService(
                    self.settings.google_service_account_file,
                    self.environment.google_workspace.drive_folder_id or "",
                ).generate_arrest_warrant,
                template_document_id=config.template_document_id,
                output_drive_folder_id=config.output_drive_folder_id,
                request_id=submission.request_id,
                replacements=replacements,
                document_title=f"Approved {document_label} {submission.request_id}",
                required_placeholders=set(replacements),
            )
        except Exception:
            LOGGER.exception("%s approval generation failed for %s", document_label, submission.request_id)
            await interaction.followup.send(
                "The approved warrant could not be generated. Check the Wispbyte console for the safe "
                "diagnostic message.",
                ephemeral=True,
            )
            return
        if warrant.page_count != 1:
            await self._notify_warrant_fit_failure(
                interaction.channel, submission, "the approved warrant would exceed the required one-page limit"
            )
            await interaction.followup.send(
                "No approved player-facing warrant was issued because the completed document is not "
                "exactly one page.",
                ephemeral=True,
            )
            return
        await self._post_search_seizure_warrant(
            channel=interaction.channel,
            submission=submission,
            warrant=warrant,
            approved=True,
            issue_date=issue_date,
            approver_mention=interaction.user.mention,
            document_kind=document_kind,
        )
        recorded = await self.store.mark_warrant_approved(
            submission_id=submission.id,
            approver_user_id=interaction.user.id,
            approver_name=approver_name,
            approved_at=approved_at,
            approved_document_id=warrant.document_id,
        )
        if not recorded:
            LOGGER.warning("Search / Seizure approval was posted but audit state changed for %s", submission.request_id)
        await interaction.followup.send(
            f"Approved and posted the signed one-page warrant in {interaction.channel.mention}.",
            ephemeral=True,
        )

    async def _deny_search_seizure_warrant(
        self, interaction: discord.Interaction, denial_notes: str, *, document_kind: str = "search_seizure"
    ) -> None:
        document_label = self._court_order_document_label(document_kind)
        if not self._can_deny_warrant(interaction):
            await interaction.response.send_message(
                f"Only a configured Judge or bot administrator can deny a {document_label}.",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run this command inside the private Court Order ticket.", ephemeral=True
            )
            return
        denial_notes = denial_notes.strip()
        if not denial_notes or len(denial_notes) > 1000:
            await interaction.response.send_message(
                "Denial notes are required and must be 1,000 characters or fewer.", ephemeral=True
            )
            return
        submission = await self.store.get_by_channel_id(interaction.channel.id)
        if not submission or submission.workflow != "court_order" or not self._is_court_order_document_type(submission, document_kind):
            await interaction.response.send_message(
                f"This channel does not contain a {document_label} request.", ephemeral=True
            )
            return
        if submission.status == "closed":
            await interaction.response.send_message(
                "This request is already closed and cannot be denied.", ephemeral=True
            )
            return
        if submission.status == "approved":
            await interaction.response.send_message(
                f"This {document_label} was already approved and cannot be denied.", ephemeral=True
            )
            return
        if submission.status == "denied":
            await interaction.response.send_message(
                f"This {document_label} has already been denied. A corrected Form submission can "
                "reference this request ID to continue here.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        denied_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        denier_name = interaction.user.display_name.strip() or interaction.user.name
        recorded = await self.store.mark_warrant_denied(
            submission_id=submission.id,
            denier_user_id=interaction.user.id,
            denier_name=denier_name,
            denied_at=denied_at,
            denial_note=denial_notes,
        )
        if not recorded:
            await interaction.followup.send(
                "The denial could not be recorded because this request changed state. Please retry "
                "only after staff review the ticket.",
                ephemeral=True,
            )
            return
        await interaction.channel.send(
            f"**{document_label} denied** by {interaction.user.mention}.\n"
            f"**Denial notes:** {denial_notes}\n\n"
            "This ticket remains open. To submit a correction, complete a new Court Order Form and "
            f"enter **{submission.request_id}** in the Docket / Off-Docket Name/ID field; the bot "
            "will post the new submission in this same ticket."
        )
        await interaction.followup.send(
            f"Denied **{submission.request_id}** and recorded the denial notes in this ticket.",
            ephemeral=True,
        )

    async def _post_search_seizure_warrant(
        self,
        *,
        channel: discord.TextChannel,
        submission: Submission,
        warrant: GeneratedWarrant,
        approved: bool,
        issue_date: str | None = None,
        approver_mention: str | None = None,
        document_kind: str = "search_seizure",
    ) -> None:
        """Post the verified PNG and optional FiveManage URL without blocking the record."""
        document_label = self._court_order_document_label(document_kind)
        document_slug = "subpoena" if document_kind == "subpoena" else "search-seizure-warrant"
        filename = (
            f"approved-{document_slug}-{submission.request_id.lower()}.png"
            if approved
            else f"{document_slug}-{submission.request_id.lower()}.png"
        )
        fivemanage_url: str | None = None
        if self.settings.fivemanage_api_token:
            try:
                upload = await asyncio.to_thread(
                    FiveManageService(
                        self.settings.fivemanage_api_token, self.settings.fivemanage_storage_path
                    ).upload_png,
                    filename=filename,
                    content=warrant.png_bytes,
                    request_id=submission.request_id,
                )
                fivemanage_url = upload.url
            except Exception:
                LOGGER.exception("FiveManage upload failed for %s %s", document_label, submission.request_id)
        heading = f"**{document_label} approved**" if approved else f"One-page {document_label} generated"
        message = (
            f"{heading}"
            + (f" by {approver_mention} on **{issue_date}**." if approved else f" for **{submission.request_id}**.")
            + f"\nInternal document record: {warrant.document_url}"
        )
        if fivemanage_url:
            message += f"\nFiveManage PNG URL: {fivemanage_url}"
        elif self.settings.fivemanage_api_token:
            message += "\nFiveManage upload was unavailable; the PNG is attached here."
        await channel.send(message, file=discord.File(BytesIO(warrant.png_bytes), filename=filename))

    async def _sync_court_order_responses(self) -> int:
        """Read new source rows once and route each one to a private ticket."""
        async with self._sync_lock:
            return await self._sync_court_order_responses_locked()

    async def _sync_court_order_responses_locked(self) -> int:
        """Perform the source scan while excluding concurrent manual or automatic scans."""
        intake = self.environment.court_order_intake
        if not intake or not self.settings.google_service_account_file:
            raise RuntimeError("Court Order Form intake is not configured on this host.")
        workspace, rows = await self._get_court_order_rows()
        created = 0
        for row in rows:
            if self._is_archived_form_response(row):
                continue
            source_key = f"{intake.response_spreadsheet_id}:{row['_source_row']}"
            username = self._normalize_username(
                self._answer(row, "Discord Username", "Your Discord Name")
            )
            if not username:
                LOGGER.warning("Skipping Court Order response %s without a Discord username", source_key)
                continue
            workflow = "new_docket" if self._is_new_docket_case_row(row) else "court_order"
            submission = await self.store.begin_submission(
                source_key=source_key,
                workflow=workflow,
                requester_username=username,
                payload=row,
                request_prefix="DCK" if workflow == "new_docket" else "COR",
            )
            if not submission:
                existing = await self.store.get_by_source_key(source_key)
                if existing and await self.store.requeue_failed_uncreated_submission(
                    submission_id=existing.id,
                    requester_username=username,
                    payload=row,
                ):
                    submission = await self.store.get_by_id(existing.id)
                    if not submission:
                        raise RuntimeError("The failed Form response could not be reloaded for retry.")
                else:
                    await self.store.refresh_active_submission_payload(
                        source_key=source_key,
                        workflow=workflow,
                        requester_username=username,
                        payload=row,
                    )
                    continue
            try:
                if workflow == "new_docket":
                    channel = await self._create_docket_case(submission)
                    tracking = await asyncio.to_thread(
                        workspace.append_tracking_row,
                        request_id=submission.request_id,
                        source_key=source_key,
                        payload=row,
                        channel_id=channel.id,
                        channel_url=channel.jump_url,
                        status="Pending Review",
                        destination="Docket Forum post",
                    )
                    await self.store.mark_ticket_created(
                        submission.id,
                        channel.id,
                        tracking.spreadsheet_id,
                        tracking.row_number,
                        status="pending_review",
                    )
                    created += 1
                    continue
                reference = self._existing_docket_reference(row)
                channel = await self._find_reusable_court_order_ticket(reference)
                if channel:
                    await self._post_court_order_intake(
                        channel,
                        submission,
                        existing_reference=reference,
                    )
                else:
                    channel = await self._create_court_order_ticket(submission)
                tracking = await asyncio.to_thread(
                    workspace.append_tracking_row,
                    request_id=submission.request_id,
                    source_key=source_key,
                    payload=row,
                    channel_id=channel.id,
                    channel_url=channel.jump_url,
                )
                await self.store.mark_ticket_created(
                    submission.id, channel.id, tracking.spreadsheet_id, tracking.row_number
                )
                created += 1
            except Exception:
                await self.store.mark_failed(submission.id)
                raise
        return created

    async def _court_order_poll_loop(self) -> None:
        """Import new Court Order responses at a bounded interval after the bot connects."""
        await self.wait_until_ready()
        interval = max(60, self.settings.google_forms_poll_interval_seconds)
        while not self.is_closed():
            try:
                created = await self._sync_court_order_responses()
                if created:
                    LOGGER.info("Automatically created %s Court Order ticket(s)", created)
            except Exception:
                LOGGER.exception("Automatic Court Order Form synchronization failed")
            await asyncio.sleep(interval)

    async def _baseline_court_order_responses(self) -> int:
        """Suppress historical rows before enabling active intake for a copied Form."""
        intake = self.environment.court_order_intake
        if not intake:
            raise RuntimeError("Court Order Form intake is not configured on this host.")
        _, rows = await self._get_court_order_rows()
        marked = 0
        for row in rows:
            source_key = f"{intake.response_spreadsheet_id}:{row['_source_row']}"
            username = self._normalize_username(
                self._answer(row, "Discord Username", "Your Discord Name")
            )
            if await self.store.record_baseline(
                source_key=source_key,
                workflow="court_order",
                requester_username=username or "unmatched",
                payload=row,
            ):
                marked += 1
        return marked

    async def _refresh_court_order_submission(self, submission: Submission) -> Submission | None:
        """Reload one active ticket from its original Form row without issuing a second request ID."""
        intake = self.environment.court_order_intake
        if not intake or not self.settings.google_service_account_file:
            raise RuntimeError("Court Order Form intake is not configured on this host.")
        _, rows = await self._get_court_order_rows()
        source_row = next(
            (
                row
                for row in rows
                if f"{intake.response_spreadsheet_id}:{row['_source_row']}" == submission.source_key
            ),
            None,
        )
        if not source_row:
            return None
        username = self._normalize_username(
            self._answer(source_row, "Discord Username", "Your Discord Name")
        )
        if not username:
            raise RuntimeError("The original Form response does not include a Discord username.")
        refreshed = await self.store.refresh_active_submission_payload(
            source_key=submission.source_key,
            workflow="court_order",
            requester_username=username,
            payload=source_row,
        )
        if not refreshed:
            return None
        return await self.store.get_by_request_id(submission.request_id)

    async def _get_court_order_rows(self) -> tuple[GoogleWorkspaceService, list[dict[str, str]]]:
        intake = self.environment.court_order_intake
        if not intake or not self.settings.google_service_account_file:
            raise RuntimeError("Court Order Form intake is not configured on this host.")
        workspace = GoogleWorkspaceService(
            self.settings.google_service_account_file,
            self.environment.google_workspace.drive_folder_id or "",
        )
        rows = await asyncio.to_thread(
            workspace.get_form_response_rows,
            intake.response_spreadsheet_id,
            intake.response_sheet_name,
        )
        return workspace, rows

    async def _archive_closed_form_response(
        self,
        *,
        submission: Submission,
        closed_by: str,
        closed_at: str,
        closure_note: str | None,
    ) -> None:
        """Archive a Form-originated ticket without destabilizing Google Forms row IDs."""
        intake = self.environment.court_order_intake
        if not intake or not self.settings.google_service_account_file:
            raise RuntimeError("The Form response archive is not configured on this host.")
        spreadsheet_id, separator, row_value = submission.source_key.rpartition(":")
        if not separator or spreadsheet_id != intake.response_spreadsheet_id:
            raise RuntimeError("The request does not have a valid Form response reference for archival.")
        try:
            source_row = int(row_value)
        except ValueError as error:
            raise RuntimeError("The request does not have a valid Form response row for archival.") from error
        workspace = GoogleWorkspaceService(
            self.settings.google_service_account_file,
            self.environment.google_workspace.drive_folder_id or "",
        )
        await asyncio.to_thread(
            workspace.archive_form_response_row,
            spreadsheet_id=intake.response_spreadsheet_id,
            sheet_name=intake.response_sheet_name,
            source_row=source_row,
            request_id=submission.request_id,
            closed_by=closed_by,
            closed_at=closed_at,
            closure_note=closure_note,
        )

    @staticmethod
    def _is_archived_form_response(row: dict[str, str]) -> bool:
        """Keep closed records out of intake even if the local database is recreated."""
        return row.get(FORM_RESPONSE_ARCHIVE_STATUS_HEADER, "").strip().casefold() == "archived"

    @classmethod
    def _is_new_docket_case_row(cls, payload: dict[str, str]) -> bool:
        """Keep the Form's new Docket route out of the private Court Order workflow."""
        return cls._answer(payload, "Request Type").casefold() == "file new case: creates new docket entry"

    async def _create_docket_case(self, submission: Submission) -> discord.Thread:
        """Create the staff Docket Forum post directly from a new-case Form response."""
        guild = self.get_guild(self.environment.guild.id)
        if not guild:
            raise RuntimeError("The configured Discord server was not available.")
        forum_id = self.environment.channels.get("docket_forum")
        forum = guild.get_channel(forum_id or 0)
        if not isinstance(forum, discord.ForumChannel):
            raise TypeError("The configured Docket Forum is not available.")
        thread_name = self._docket_thread_name(submission)
        thread_with_message = await forum.create_thread(
            name=thread_name,
            embed=self._docket_intake_embed(submission),
            reason=f"NERP - Case Management new Docket filing {submission.request_id}",
        )
        thread = thread_with_message.thread
        await thread.send(embed=await self._case_assignment_embed(submission))
        await self._automatically_import_referenced_court_orders(thread, submission)
        await thread.send(
            "This Docket was created from the Case Management System form. "
            "Authorized staff may use the normal assignment and Court Order merge commands here."
        )
        return thread

    async def _automatically_import_referenced_court_orders(
        self, docket_thread: discord.Thread, submission: Submission
    ) -> None:
        """Forward valid Form-referenced Court Orders while keeping the original investigations open."""
        bot_user = self.user
        if not bot_user:
            raise RuntimeError("The bot user is not available for the automatic import audit.")
        seen_request_ids: set[str] = set()
        for reference in self._answers_for_prefix(submission.payload, "Import Off-Docket Court Order"):
            match = re.search(r"\bCOR-\d{6}\b", reference, flags=re.IGNORECASE)
            if not match:
                await docket_thread.send(
                    f"Automatic Court Order import skipped for **{reference}** because it is not a bot-issued `COR-######` ID."
                )
                continue
            request_id = match.group(0).upper()
            if request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)
            court_order = await self.store.get_by_request_id(request_id)
            if not court_order or court_order.workflow != "court_order" or court_order.status == "closed":
                await docket_thread.send(
                    f"Automatic Court Order import skipped for **{request_id}** because no active bot-managed Court Order was found."
                )
                continue
            try:
                await self._merge_court_order_into_docket(
                    submission_id=court_order.id,
                    docket_thread_id=docket_thread.id,
                    close_source=False,
                    actor=bot_user,
                )
            except (CourtOrderMergeError, RuntimeError, ValueError, discord.Forbidden):
                LOGGER.exception(
                    "Automatic Court Order import failed for %s into Docket %s",
                    request_id,
                    docket_thread.id,
                )
                await docket_thread.send(
                    f"Automatic Court Order import could not complete for **{request_id}**. "
                    "Leadership can use `/import-court-order` here after reviewing the source ticket."
                )

    def _docket_thread_name(self, submission: Submission) -> str:
        """Produce a readable, stable forum-post title from the submitted case parties."""
        petitioner = self._answer_prefix(submission.payload, "Petitioner Subject 1 Name:") or "petitioner"
        respondent = self._answer_prefix(submission.payload, "Respondent Subject 1 Name:") or "respondent"
        return self._discord_channel_name_component(
            f"{submission.request_id}-{petitioner}-v-{respondent}",
            fallback=submission.request_id.lower(),
        )[:100]

    def _docket_intake_embed(self, submission: Submission) -> discord.Embed:
        """Render a compact, complete overview of a new Docket filing."""
        payload = submission.payload
        embed = discord.Embed(
            title=f"New Docket Filing — {submission.request_id}",
            description="Created from the Case Management System form; pending staff review.",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="Filing overview",
            value=(
                f"**Court:** {self._answer(payload, 'Select Court:') or 'Unspecified'}\n"
                f"**Requester:** {self._answer(payload, 'Requestors Name:') or 'Unspecified'}\n"
                f"**Requester role:** {self._answer(payload, 'Requestors Role:') or 'Unspecified'}\n"
                f"**Requester title:** {self._answer(payload, 'Requestors Title:') or 'Unspecified'}\n"
                f"**Requesting agency:** {self._answer(payload, 'Requesting Agency:') or 'Unspecified'}\n"
                f"**Primary case officer:** {self._answer_prefix(payload, 'Primary Case Officer') or 'Unspecified'}\n"
                f"**Case officer agency:** {self._answer_prefix(payload, 'Law Enforcement Agency Assigned to Case:') or 'Unspecified'}"
            )[:1024],
            inline=False,
        )
        petitioners = [
            value
            for value in self._answers_for_prefix(payload, "Petitioner Subject")
            if value.strip()
        ]
        respondents = [
            value
            for value in self._answers_for_prefix(payload, "Respondent Subject")
            if value.strip()
        ]
        embed.add_field(name="Petitioner(s)", value="\n".join(petitioners) or "Unspecified", inline=True)
        embed.add_field(name="Respondent(s)", value="\n".join(respondents) or "Unspecified", inline=True)
        embed.add_field(
            name="Proceeding",
            value=(
                f"**Trial type:** {self._answer(payload, 'Trial Type:') or 'Unspecified'}\n"
                f"**Initial hearing:** {self._answer(payload, 'Initial Hearing Type:') or 'Unspecified'}\n"
                f"**Proposed date:** {self._answer(payload, 'Proposed Initial Hearing Date:') or 'Unspecified'}\n"
                f"**Proposed time:** {self._answer(payload, 'Proposed Initial Hearing Time:') or 'Unspecified'}\n"
                f"**Party availability:** {self._answer_prefix(payload, 'Have All Parties Agreed') or 'Unspecified'}"
            )[:1024],
            inline=False,
        )
        orders = self._answers_for_prefix(payload, "Import Off-Docket Court Order")
        if orders:
            embed.add_field(name="Imported off-docket order reference(s)", value="\n".join(orders)[:1024], inline=False)
        embed.set_footer(text=f"Docket ID: {submission.request_id}")
        return embed

    async def _resolve_docket_thread(self, reference: str) -> discord.Thread:
        """Resolve only a Forum post in the configured Docket Forum from a pasted URL or ID."""
        match = re.search(r"(\d{17,20})/?$", reference.strip())
        if not match:
            raise ValueError(
                "Provide the destination Docket Forum post's full Discord link or its numeric Discord ID."
            )
        guild = self.get_guild(self.environment.guild.id)
        if not guild:
            raise ValueError("The configured Discord server is not currently available.")
        thread_id = int(match.group(1))
        target = guild.get_thread(thread_id)
        if target is None:
            try:
                fetched = await guild.fetch_channel(thread_id)
            except discord.NotFound as error:
                raise ValueError("That Docket Forum post could not be found.") from error
            target = fetched if isinstance(fetched, discord.Thread) else None
        expected_parent_id = self.environment.channels.get("docket_forum")
        if not isinstance(target, discord.Thread) or target.parent_id != expected_parent_id:
            raise ValueError("The selected destination must be an existing post in the configured Docket Forum.")
        if target.archived or target.locked:
            raise ValueError("The selected Docket post is archived or locked and cannot receive a Court Order.")
        return target

    async def _resolve_docket_thread_by_request_id(self, docket_id: str) -> discord.Thread:
        """Resolve an active Docket Forum post from its bot-issued DCK identifier."""
        match = re.fullmatch(r"\s*(DCK-\d{6})\s*", docket_id, flags=re.IGNORECASE)
        if not match:
            raise ValueError("Provide the bot-issued Docket ID in the format `DCK-000010`.")
        submission = await self.store.get_by_request_id(match.group(1))
        if not submission or submission.workflow != "new_docket" or submission.status == "closed":
            raise ValueError("That is not an active bot-managed Docket available to receive a Court Order.")
        if not submission.discord_channel_id:
            raise ValueError("That Docket does not have an available Discord Forum post.")
        return await self._resolve_docket_thread(str(submission.discord_channel_id))

    async def _resolve_active_court_order(self, court_order_id: str) -> Submission:
        """Resolve an active Court Order from its bot-issued COR identifier."""
        match = re.fullmatch(r"\s*(COR-\d{6})\s*", court_order_id, flags=re.IGNORECASE)
        if not match:
            raise ValueError("Provide the bot-issued Court Order ID in the format `COR-000007`.")
        submission = await self.store.get_by_request_id(match.group(1))
        if not submission or submission.workflow != "court_order" or submission.status == "closed":
            raise ValueError("That is not an active bot-managed Court Order available for import.")
        return submission

    async def _merge_court_order_into_docket(
        self,
        *,
        submission_id: int,
        docket_thread_id: int,
        close_source: bool,
        actor: discord.abc.User,
    ) -> str:
        """Forward an auditable Court Order snapshot into a Docket post, optionally closing the source."""
        submission = await self.store.get_by_id(submission_id)
        if not submission or submission.workflow != "court_order" or submission.status == "closed":
            raise RuntimeError("This Court Order is no longer an active request and cannot be merged.")
        guild = self.get_guild(self.environment.guild.id)
        source = guild.get_channel(submission.discord_channel_id or 0) if guild else None
        if not isinstance(source, discord.TextChannel):
            raise CourtOrderMergeError(
                "The original private Court Order ticket is no longer available to merge."
            )
        docket_thread = await self._resolve_docket_thread(str(docket_thread_id))
        previous_merge = await self.store.get_court_order_docket_merge(submission.id)
        if previous_merge and previous_merge.docket_thread_id != docket_thread.id:
            raise RuntimeError(
                "This Court Order was already merged into a different Docket post. "
                "Use that original Docket post to preserve one continuous case record."
            )

        actor_name = getattr(actor, "display_name", actor.name).strip() or actor.name
        merged_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        last_forwarded_id = previous_merge.last_forwarded_message_id if previous_merge else None
        if previous_merge:
            header = "Court Order update merged into Docket"
            description = (
                f"Additional content from **{submission.request_id}** was forwarded from its private "
                f"Court Order ticket by {actor.mention}."
            )
        else:
            header = "Court Order merged into Docket"
            description = (
                f"**{submission.request_id}** was copied from its private investigative Court Order ticket "
                f"by {actor.mention}. Forwarded messages retain the original Discord authorship "
                "and attachments."
            )
        await docket_thread.send(
            embed=discord.Embed(
                title=header,
                description=description,
                color=discord.Color.gold(),
                timestamp=datetime.fromisoformat(merged_at),
            )
        )

        forwarded_count = 0
        newest_message_id = last_forwarded_id
        history_kwargs: dict[str, object] = {"limit": None, "oldest_first": True}
        if last_forwarded_id:
            history_kwargs["after"] = discord.Object(id=last_forwarded_id)
        async for message in source.history(**history_kwargs):
            await message.forward(docket_thread, fail_if_not_exists=False)
            forwarded_count += 1
            newest_message_id = message.id

        await self.store.record_court_order_docket_merge(
            submission_id=submission.id,
            docket_thread_id=docket_thread.id,
            merged_by_user_id=actor.id,
            merged_by_name=actor_name,
            merged_at=merged_at,
            last_forwarded_message_id=newest_message_id,
            source_closed=False,
        )
        if not close_source:
            source_notice = await source.send(
                f"{actor.mention} merged **{submission.request_id}** into {docket_thread.mention}. "
                "This Court Order remains open; a later merge can forward additional ticket activity."
            )
            await self.store.record_court_order_docket_merge(
                submission_id=submission.id,
                docket_thread_id=docket_thread.id,
                merged_by_user_id=actor.id,
                merged_by_name=actor_name,
                merged_at=merged_at,
                last_forwarded_message_id=source_notice.id,
                source_closed=False,
            )
            return (
                f"Merged **{submission.request_id}** into {docket_thread.mention} and forwarded "
                f"{forwarded_count} message(s). The original Court Order remains open."
            )

        closure_note = f"Merged into Docket {docket_thread.mention} by {actor.mention}."
        records_channel = await self._get_or_create_case_records_channel()
        for record_embed in await self._case_record_embeds(
            submission=submission,
            closed_by=actor,
            closed_at=merged_at,
            closure_note=closure_note,
            source_deleted=True,
        ):
            await records_channel.send(embed=record_embed)
        recorded = await self.store.mark_closed(
            submission_id=submission.id,
            closer_user_id=actor.id,
            closer_name=actor_name,
            closed_at=merged_at,
            closed_note=closure_note,
        )
        if not recorded:
            raise RuntimeError("This Court Order was already closed before the merge could finish.")
        await self.store.record_court_order_docket_merge(
            submission_id=submission.id,
            docket_thread_id=docket_thread.id,
            merged_by_user_id=actor.id,
            merged_by_name=actor_name,
            merged_at=merged_at,
            last_forwarded_message_id=newest_message_id,
            source_closed=True,
        )
        await self._finalize_request_channel(source, submission)
        return (
            f"Merged **{submission.request_id}** into {docket_thread.mention}, forwarded "
            f"{forwarded_count} message(s), posted the permanent staff record in {records_channel.mention}, "
            "and closed the original Court Order ticket."
        )

    async def _get_or_create_case_records_channel(self) -> discord.TextChannel:
        """Lazily create the permanent DOJ/PD-only records channel on first closure."""
        guild = self.get_guild(self.environment.guild.id)
        if not guild:
            raise RuntimeError("The configured Discord server was not available.")
        category = await self._resolve_category(
            "court_administration_category", "court_administration"
        )

        stored_channel_id = await self.store.get_resource_channel_id("doj_case_records")
        stored_channel = guild.get_channel(stored_channel_id) if stored_channel_id else None
        if isinstance(stored_channel, discord.TextChannel):
            return stored_channel

        existing = discord.utils.get(category.text_channels, name="doj-case-records")
        if isinstance(existing, discord.TextChannel):
            await self.store.set_resource_channel_id("doj_case_records", existing.id)
            return existing

        me = guild.me
        if not me:
            raise RuntimeError("The bot member was not available in the configured guild.")
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                manage_messages=True,
            ),
        }
        for role_id in self._case_record_viewer_role_ids():
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    attach_files=False,
                    embed_links=True,
                )
        channel = await guild.create_text_channel(
            name="doj-case-records",
            category=category,
            topic="NERP - Case Management permanent DOJ/PD case and request closure records.",
            overwrites=overwrites,
            reason="NERP - Case Management first request closure",
        )
        await self.store.set_resource_channel_id("doj_case_records", channel.id)
        return channel

    async def _case_record_embeds(
        self,
        *,
        submission: Submission,
        closed_by: discord.abc.User,
        closed_at: str,
        closure_note: str | None,
        source_deleted: bool,
    ) -> list[discord.Embed]:
        """Create staff-only records, including Form evidence and assignment audit context."""
        classified_answer = self._answer_prefix(
            submission.payload,
            'Has the Case or it\'s evidence been designated "Classified"',
        )
        classified = "Classified" if classified_answer.lower() in {"yes", "y"} else "Unclassified"
        is_search_seizure = self._is_search_seizure_warrant(submission)
        is_subpoena = self._is_subpoena(submission)
        is_new_docket = submission.workflow == "new_docket"
        subjects = self._answers_for_prefix(
            submission.payload,
            "Subject Name of Subpoena:"
            if is_subpoena
            else "Subject Name of Search or Seizure:"
            if is_search_seizure
            else "Subject / Arrestee Name:",
        )
        citizen_ids = self._answers_for_prefix(submission.payload, "Subject Citizen ID:")
        subject_detail_values = self._answers_for_prefix(
            submission.payload,
            "Purpose of Subpoena:"
            if is_subpoena
            else "Request Type:"
            if is_search_seizure
            else "Initial Charges To Be Filed Against Subject:",
        )
        narrative_values = self._answers_for_prefix(
            submission.payload,
            "Subpoena Details as It Will Appear on The Order:"
            if is_subpoena
            else "Probable Cause For Search or Seizure:"
            if is_search_seizure
            else "Probable Cause For Arrest:",
        )
        evidence_links = self._answers_for_prefix(submission.payload, "Please include any evidence")
        if is_new_docket:
            petitioners = self._answers_for_prefix(submission.payload, "Petitioner Subject")
            respondents = self._answers_for_prefix(submission.payload, "Respondent Subject")
            subject_lines = [
                *(f"**Petitioner {number}:** {name}" for number, name in enumerate(petitioners, start=1)),
                *(f"**Respondent {number}:** {name}" for number, name in enumerate(respondents, start=1)),
            ]
        else:
            subject_lines = [
                f"{number}. {self._value_at(subjects, number - 1)} "
                f"({self._value_at(citizen_ids, number - 1)}) — "
                f"{self._value_at(subject_detail_values, number - 1)}"
                for number in range(1, max(len(subjects), len(citizen_ids), len(subject_detail_values)) + 1)
            ]
        embed = discord.Embed(
            title=f"Closed {submission.workflow.replace('_', ' ').title()} — {submission.request_id}",
            description=(
                "Permanent staff case record. Form-submitted evidence links are recorded below; "
                "uploaded attachments remain in the source request."
            ),
            color=discord.Color.dark_grey(),
            timestamp=datetime.fromisoformat(closed_at),
        )
        embed.add_field(
            name="Request",
            value=(
                f"**Type:** {self._answer(submission.payload, 'Request Type') or submission.workflow}\n"
                f"**Requester:** {self._answer(submission.payload, 'Requestors Name:') or 'N/A'}\n"
                f"**Requester role:** {self._answer(submission.payload, 'Requestors Role:') or 'Unspecified'}\n"
                f"**Agency:** {self._answer(submission.payload, 'Requesting Agency:') or 'N/A'}\n"
                f"**Primary Case Officer:** {self._answer_prefix(submission.payload, 'Primary Case Officer') or 'Unspecified'}\n"
                f"**Case Officer Agency:** {self._answer_prefix(submission.payload, 'Law Enforcement Agency Assigned to Case:') or 'Unspecified'}\n"
                f"**Docket / Off-Docket:** {submission.request_id if is_new_docket else self._existing_docket_reference(submission.payload) or 'N/A'}"
            ),
            inline=False,
        )
        embed.add_field(name="Classification", value=classified, inline=True)
        embed.add_field(
            name="Status at closure", value=submission.status.replace("_", " ").title(), inline=True
        )
        embed.add_field(name="Closed by", value=closed_by.mention, inline=True)
        judicial_history: list[str] = []
        if submission.denied_by_name:
            decision = f"Denied by <@{submission.denied_by_user_id}> on {submission.denied_at}"
            if submission.denial_note:
                decision += f"\n**Denial notes:** {submission.denial_note}"
            judicial_history.append(decision)
        if submission.approved_by_name:
            judicial_history.append(
                f"Approved by <@{submission.approved_by_user_id}> on {submission.approved_at}"
            )
        if judicial_history:
            embed.add_field(
                name="Judicial history",
                value="\n\n".join(judicial_history)[:1024],
                inline=False,
            )
        if subject_lines:
            embed.add_field(name="Subjects", value="\n".join(subject_lines)[:1024], inline=False)
        assignments = await self.store.get_case_assignments(submission.id)
        assignment_values = {
            (item.assignment_type, item.assignment_slot): item for item in assignments
        }
        assignment_lines = []
        for assignment_type in CASE_ASSIGNMENT_TYPES:
            for slot in CASE_ASSIGNMENT_SLOTS:
                assignment = assignment_values.get((assignment_type, slot))
                assignment_lines.append(
                    f"**{self._assignment_label(assignment_type)} {slot}:** "
                    f"<@{assignment.user_id}>"
                    if assignment
                    else f"**{self._assignment_label(assignment_type)} {slot}:** Unassigned"
                )
        embed.add_field(name="Case assignments", value="\n".join(assignment_lines), inline=False)
        transfers = await self.store.get_assignment_transfers(submission.id)
        accepted_transfers = [transfer for transfer in transfers if transfer.status == "accepted"]
        if accepted_transfers:
            embed.add_field(
                name="Accepted assignment transfers",
                value="\n".join(
                    f"{self._assignment_label(transfer.assignment_type)} {transfer.assignment_slot}: "
                    f"<@{transfer.from_user_id}> → <@{transfer.to_user_id}> "
                    f"(accepted {transfer.accepted_at})"
                    for transfer in accepted_transfers
                )[:1024],
                inline=False,
            )
        if closure_note:
            embed.add_field(name="Closure note", value=closure_note, inline=False)
        if source_deleted:
            embed.add_field(
                name="Source request",
                value="Original private ticket deleted after this staff record was posted.",
                inline=False,
            )
        elif submission.discord_channel_id:
            embed.add_field(
                name="Source request", value=f"<#{submission.discord_channel_id}>", inline=False
            )
        embed.set_footer(text=f"Closed at {closed_at}")
        records = [embed]
        narrative_label = "Submitted subpoena details" if is_subpoena else "Submitted probable cause"
        for number, narrative in enumerate(narrative_values, start=1):
            if narrative.strip().upper() in {"", "N/A", "NA", "NONE"}:
                continue
            subject_name = self._value_at(subjects, number - 1)
            for part, chunk in enumerate(self._discord_embed_chunks(narrative), start=1):
                continuation = f" (continued {part})" if part > 1 else ""
                narrative_embed = discord.Embed(
                    title=f"{narrative_label} — {submission.request_id}",
                    color=discord.Color.dark_grey(),
                    timestamp=datetime.fromisoformat(closed_at),
                )
                narrative_embed.add_field(
                    name=f"Subject {number}: {subject_name}{continuation}",
                    value=chunk,
                    inline=False,
                )
                narrative_embed.set_footer(text=f"Staff record for {submission.request_id}")
                records.append(narrative_embed)
        for number, evidence in enumerate(evidence_links, start=1):
            if evidence.strip().upper() in {"", "N/A", "NA", "NONE"}:
                continue
            for part, chunk in enumerate(self._discord_embed_chunks(evidence), start=1):
                continuation = f" (continued {part})" if part > 1 else ""
                evidence_embed = discord.Embed(
                    title=f"Submitted evidence — {submission.request_id}",
                    color=discord.Color.dark_grey(),
                    timestamp=datetime.fromisoformat(closed_at),
                )
                evidence_embed.add_field(
                    name=f"Subject {number} evidence links{continuation}",
                    value=chunk,
                    inline=False,
                )
                evidence_embed.set_footer(text=f"Staff record for {submission.request_id}")
                records.append(evidence_embed)
        return records

    async def _finalize_request_channel(
        self,
        channel: discord.TextChannel | discord.Thread,
        submission: Submission,
    ) -> None:
        """Remove a private ticket or archive and lock a tracked Forum post after logging."""
        if isinstance(channel, discord.Thread):
            await channel.edit(
                archived=True,
                locked=True,
                reason=f"NERP - Case Management closed request {submission.request_id}",
            )
            return
        await channel.delete(
            reason=f"NERP - Case Management closed request {submission.request_id}",
        )

    async def _create_court_order_ticket(self, submission: Submission) -> discord.TextChannel:
        guild = self.get_guild(self.environment.guild.id)
        if not guild:
            raise RuntimeError("The configured Discord server was not available.")
        category = await self._resolve_category(
            "off_docket_tickets_category", "off_docket_tickets"
        )
        me = guild.me
        if not me:
            raise RuntimeError("The bot member was not available in the configured guild.")
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = dict(
            category.overwrites
        )
        for target, overwrite in overwrites.items():
            if isinstance(target, discord.Role) and target != guild.default_role:
                overwrite.view_channel = False
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )
        for group in ("administrators", "judges", "prosecutors", "defense_attorneys"):
            for role_id in self.environment.roles.get(group, []):
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    )
        channel = await guild.create_text_channel(
            name=self._court_order_ticket_name(submission),
            category=category,
            overwrites=overwrites,
            topic=f"Court Order request {submission.request_id}; awaiting claimant verification.",
            reason=f"NERP - Case Management Court Order intake {submission.request_id}",
        )
        await self._post_court_order_intake(channel, submission)
        return channel

    @staticmethod
    def _discord_channel_name_component(value: str, *, fallback: str) -> str:
        """Turn a Discord display name into a short, stable channel-name component."""
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return normalized or fallback

    def _court_order_ticket_name(
        self,
        submission: Submission,
        requester: discord.Member | None = None,
    ) -> str:
        """Use the request ID plus a claimant nickname, without the redundant workflow prefix."""
        requester_label = (
            requester.nick
            if requester and requester.nick
            else requester.name if requester else submission.requester_username
        )
        request_component = self._discord_channel_name_component(
            submission.request_id, fallback="court-order"
        )
        requester_component = self._discord_channel_name_component(
            requester_label, fallback="requester"
        )
        return f"{request_component}-{requester_component}"[:100].rstrip("-")

    async def _find_reusable_court_order_ticket(
        self, reference: str
    ) -> discord.TextChannel | None:
        """Resolve an open bot-managed Court Order ticket from its request ID or channel name."""
        normalized = reference.strip().lower()
        if not normalized:
            return None
        request_match = re.search(r"\bcor-\d{6}\b", normalized, flags=re.IGNORECASE)
        candidate: Submission | None = None
        if request_match:
            candidate = await self.store.get_by_request_id(request_match.group(0))

        guild = self.get_guild(self.environment.guild.id)
        if candidate and candidate.status != "closed" and guild:
            channel = guild.get_channel(candidate.discord_channel_id or 0)
            if isinstance(channel, discord.TextChannel):
                return channel

        # A custom docket/off-docket reference (rather than COR-######) can be
        # reused when it was recorded on an earlier still-open Court Order request.
        if guild:
            for active_submission in await self.store.find_open_submissions("court_order"):
                if self._existing_docket_reference(active_submission.payload).strip().lower() != normalized:
                    continue
                channel = guild.get_channel(active_submission.discord_channel_id or 0)
                if isinstance(channel, discord.TextChannel):
                    return channel

        # A user may supply the existing bot-managed channel name instead of COR-######.
        channel_name = normalized.removeprefix("#")
        if guild:
            for channel in guild.text_channels:
                if channel.name != channel_name:
                    continue
                ticket_submission = await self.store.get_by_channel_id(channel.id)
                if ticket_submission and ticket_submission.workflow == "court_order" and ticket_submission.status != "closed":
                    return channel
        return None

    async def _post_court_order_intake(
        self,
        channel: discord.TextChannel,
        submission: Submission,
        *,
        existing_reference: str | None = None,
        refreshed: bool = False,
    ) -> None:
        """Post one complete Court Order intake to either a new or existing private ticket."""
        if existing_reference:
            await channel.send(
                f"**Corrected / additional Court Order submission received** as "
                f"**{submission.request_id}**, linked to existing reference "
                f"**{existing_reference}**. The requester must claim this new submission with "
                "`/claim-court-order` before it can be generated or approved."
            )
        embed = discord.Embed(
            title=(
                f"Refreshed Court Order Request {submission.request_id}"
                if refreshed
                else f"Court Order Request {submission.request_id}"
            ),
            description=(
                "Reloaded from the original Google Form response by authorized staff. "
                "This ticket's active Form data has been updated."
                if refreshed
                else "Awaiting requester verification through `/claim-court-order`."
            ),
            color=discord.Color.dark_blue(),
        )
        for label, field, show_when_empty in (
            ("Requester", "Requestors Name:", False),
            ("Requester role", "Requestors Role:", True),
            ("Requesting agency", "Requesting Agency:", False),
            ("Primary case officer", "Primary Case Officer", True),
            ("Case officer agency", "Law Enforcement Agency Assigned to Case:", True),
            ("Request type", "Request Type", False),
            ("Existing docket / off-docket", "__existing_reference__", False),
        ):
            value = (
                self._existing_docket_reference(submission.payload)
                if field == "__existing_reference__"
                else self._answer_prefix(submission.payload, field)
            )
            if value or show_when_empty:
                embed.add_field(name=label, value=(value or "Unspecified")[:1024], inline=False)
        await channel.send(embed=embed)
        await channel.send(embed=await self._case_assignment_embed(submission))
        if not self._has_dedicated_court_order_workflow(submission):
            await channel.send(
                f"**{self._request_type(submission) or 'This Court Order type'}** was received and "
                "recorded in this private ticket. Its dedicated document-generation and judicial "
                "commands are not configured yet."
            )
        for number, details in enumerate(self._court_order_subject_details(submission.payload), start=1):
            for part, chunk in enumerate(self._discord_text_chunks(details), start=1):
                suffix = f" (continued {part})" if part > 1 else ""
                await channel.send(
                    embed=discord.Embed(
                        title=f"Subject {number} details{suffix}",
                        description=chunk,
                        color=discord.Color.dark_blue(),
                    )
                )

    def _arrest_warrant_replacements(
        self,
        *,
        submission: Submission,
    ) -> tuple[dict[str, str], str | None]:
        """Build template text from the Court Order Form without truncating facts."""
        subjects = self._answers_for_prefix(submission.payload, "Subject / Arrestee Name:")
        citizen_ids = self._answers_for_prefix(submission.payload, "Subject Citizen ID:")
        charges = self._answers_for_prefix(
            submission.payload, "Initial Charges To Be Filed Against Subject:"
        )
        probable_causes = self._answers_for_prefix(submission.payload, "Probable Cause For Arrest:")
        for number, charge in enumerate(charges, start=1):
            if len(charge) > 88:
                return {}, f"subject {number}'s initial charges exceed the 88-character limit"
        probable_cause = "\n\n".join(probable_causes)
        if len(probable_cause) > 2080:
            return {}, "the probable-cause statement exceeds the 2,080-character limit"

        classified_answer = self._answer_prefix(
            submission.payload,
            'Has the Case or it\'s evidence been designated "Classified"',
        )
        classified_designation = (
            "Classified" if classified_answer.strip().lower() in {"yes", "y"} else "Unclassified"
        )

        replacements = {
            "{{REQUEST_ID}}": submission.request_id,
            "{{DOCKET_ID}}": self._existing_docket_reference(submission.payload) or "N/A",
            "{{CLASSIFIED}}": classified_designation,
            "{{REQUESTER_NAME}}": self._answer(submission.payload, "Requestors Name:") or "N/A",
            "{{REQUESTING_AGENCY}}": self._answer(submission.payload, "Requesting Agency:") or "N/A",
            "{{PCO}}": self._answer(submission.payload, "Primary Case Officer") or "N/A",
            "{{CASE_OFFICER_AGENCY}}": self._answer(
                submission.payload, "Law Enforcement Agency Assigned to Case:"
            ) or "N/A",
            "{{PROBABLE_CAUSE}}": probable_cause or "N/A",
            "{{APPROVER_NAME}}": "Pending judicial approval",
            "{{ISSUE_DATE}}": "Pending approval",
        }
        for number in range(1, 7):
            index = number - 1
            replacements[f"{{{{SUBJECT_{number}_NAME}}}}"] = self._value_at(subjects, index)
            replacements[f"{{{{S{number}ID}}}}"] = self._value_at(citizen_ids, index)
            replacements[f"{{{{SUBJECT_{number}_INITIAL_CHARGES}}}}"] = self._value_at(charges, index)
        return replacements, None

    def _court_order_document_config(self, document_kind: str):
        if document_kind == "subpoena":
            return self.environment.court_order_subpoena
        return self.environment.court_order_search_seizure_warrant

    @staticmethod
    def _court_order_document_label(document_kind: str) -> str:
        return "Subpoena" if document_kind == "subpoena" else "Search / Seizure Warrant"

    def _is_court_order_document_type(self, submission: Submission, document_kind: str) -> bool:
        return self._is_subpoena(submission) if document_kind == "subpoena" else self._is_search_seizure_warrant(submission)

    def _court_order_document_replacements(
        self, *, submission: Submission, document_kind: str
    ) -> tuple[dict[str, str], str | None]:
        if document_kind == "subpoena":
            return self._subpoena_replacements(submission=submission)
        return self._search_seizure_warrant_replacements(submission=submission)

    def _subpoena_replacements(
        self,
        *,
        submission: Submission,
    ) -> tuple[dict[str, str], str | None]:
        """Build a three-subject Subpoena template without suppressing submitted facts."""
        subjects = self._answers_for_prefix(submission.payload, "Subject Name of Subpoena:")
        citizen_ids = self._answers_for_prefix(submission.payload, "Subject Citizen ID:")
        produce_dates = self._answers_for_prefix(
            submission.payload, "Date to Produce Materials By or Appear"
        )
        purposes = self._answers_for_prefix(submission.payload, "Purpose of Subpoena:")
        details = self._answers_for_prefix(
            submission.payload, "Subpoena Details as It Will Appear on The Order:"
        )
        subject_count = max(
            len(subjects), len(citizen_ids), len(produce_dates), len(purposes), len(details)
        )
        if subject_count > 3:
            return {}, "the request contains more than the supported three subjects"
        classified_answer = self._answer_prefix(
            submission.payload,
            'Has the Case or it\'s evidence been designated "Classified"',
        )
        classified_designation = (
            "Classified" if classified_answer.strip().lower() in {"yes", "y"} else "Unclassified"
        )
        replacements = {
            "{{REQUEST_ID}}": submission.request_id,
            "{{DOCKET_ID}}": self._existing_docket_reference(submission.payload) or "N/A",
            "{{CLASSIFIED}}": classified_designation,
            "{{REQUESTER_NAME}}": self._answer(submission.payload, "Requestors Name:") or "N/A",
            "{{REQUESTING_AGENCY}}": self._answer(submission.payload, "Requesting Agency:") or "N/A",
            "{{PCO}}": self._answer(submission.payload, "Primary Case Officer") or "N/A",
            "{{CASE_OFFICER_AGENCY}}": self._answer(
                submission.payload, "Law Enforcement Agency Assigned to Case:"
            ) or "N/A",
            "{{SUBPOENA_DETAILS}}": "\n\n".join(details) or "N/A",
            "{{APPROVER_NAME}}": "Pending judicial approval",
            "{{ISSUE_DATE}}": "Pending approval",
        }
        for number in range(1, 4):
            index = number - 1
            replacements[f"{{{{SUBJECT_{number}_NAME}}}}"] = self._value_at(subjects, index)
            replacements[f"{{{{S{number}ID}}}}"] = self._value_at(citizen_ids, index)
            replacements[f"{{{{SUBJECT_{number}_DATE}}}}"] = self._value_at(produce_dates, index)
            replacements[f"{{{{SUBJECT_{number}_SUBPOENA_TYPE}}}}"] = self._value_at(
                purposes, index
            )
        return replacements, None

    def _search_seizure_warrant_replacements(
        self,
        *,
        submission: Submission,
    ) -> tuple[dict[str, str], str | None]:
        """Build a three-subject Search / Seizure template without truncating facts."""
        subjects = self._answers_for_prefix(
            submission.payload, "Subject Name of Search or Seizure:"
        )
        citizen_ids = self._answers_for_prefix(submission.payload, "Subject Citizen ID:")
        incident_dates = self._answers_for_prefix(submission.payload, "Date Of Incident:")
        warrant_types = self._answers_for_prefix(submission.payload, "Request Type:")
        probable_causes = self._answers_for_prefix(
            submission.payload, "Probable Cause For Search or Seizure:"
        )
        subject_count = max(
            len(subjects), len(citizen_ids), len(incident_dates), len(warrant_types), len(probable_causes)
        )
        if subject_count > 3:
            return {}, "the request contains more than the supported three subjects"
        probable_cause = "\n\n".join(probable_causes)
        if len(probable_cause) > 2080:
            return {}, "the combined probable-cause statement exceeds the 2,080-character limit"
        classified_answer = self._answer_prefix(
            submission.payload,
            'Has the Case or it\'s evidence been designated "Classified"',
        )
        classified_designation = (
            "Classified" if classified_answer.strip().lower() in {"yes", "y"} else "Unclassified"
        )
        replacements = {
            "{{REQUEST_ID}}": submission.request_id,
            "{{DOCKET_ID}}": self._existing_docket_reference(submission.payload) or "N/A",
            "{{CLASSIFIED}}": classified_designation,
            "{{REQUESTER_NAME}}": self._answer(submission.payload, "Requestors Name:") or "N/A",
            "{{REQUESTING_AGENCY}}": self._answer(submission.payload, "Requesting Agency:") or "N/A",
            "{{PCO}}": self._answer(submission.payload, "Primary Case Officer") or "N/A",
            "{{CASE_OFFICER_AGENCY}}": self._answer(
                submission.payload, "Law Enforcement Agency Assigned to Case:"
            ) or "N/A",
            "{{PROBABLE_CAUSE}}": probable_cause or "N/A",
            "{{APPROVER_NAME}}": "Pending judicial approval",
            "{{ISSUE_DATE}}": "Pending approval",
        }
        for number in range(1, 4):
            index = number - 1
            replacements[f"{{{{SUBJECT_{number}_NAME}}}}"] = self._value_at(subjects, index)
            replacements[f"{{{{S{number}ID}}}}"] = self._value_at(citizen_ids, index)
            replacements[f"{{{{SUBJECT_{number}_INCIDENT_DATE}}}}"] = self._value_at(
                incident_dates, index
            )
            replacements[f"{{{{SUBJECT_{number}_WARRANT_TYPE}}}}"] = self._value_at(
                warrant_types, index
            )
        return replacements, None

    async def _notify_warrant_fit_failure(
        self, channel: discord.TextChannel, submission: Submission, reason: str
    ) -> None:
        """Tell the verified requester why no player-facing warrant was issued."""
        requester = channel.guild.get_member(submission.claimed_user_id or 0)
        mention = requester.mention if requester else "The verified requester"
        warrant_name = (
            "Subpoena"
            if self._is_subpoena(submission)
            else "Search / Seizure Warrant"
            if self._is_search_seizure_warrant(submission)
            else "Arrest Warrant"
        )
        await channel.send(
            f"{mention}, no player-facing {warrant_name} was issued for **{submission.request_id}** "
            f"because {reason}. Please shorten or revise the relevant information with DOJ staff, "
            "then request a new warrant generation. Your information was not silently truncated."
        )

    @staticmethod
    def _normalized_question_header(value: str) -> str:
        """Treat cosmetic whitespace changes in Google Forms headings as equivalent."""
        return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()

    @classmethod
    def _answers_for_prefix(cls, payload: dict[str, str], prefix: str) -> list[str]:
        normalized_prefix = cls._normalized_question_header(prefix)
        return [
            value
            for name, value in payload.items()
            if cls._normalized_question_header(name).startswith(normalized_prefix) and value
        ]

    @staticmethod
    def _value_at(values: list[str], index: int) -> str:
        return values[index] if index < len(values) and values[index] else "N/A"

    def _court_order_subject_details(self, payload: dict[str, str]) -> list[str]:
        """Render every submitted subject in the private intake ticket without omitting repeats."""
        request_type = self._answer(payload, "Request Type").strip().casefold()
        is_search_seizure = request_type == "search or seizure warrant"
        is_subpoena = request_type.startswith("subpoena")
        subjects = self._answers_for_prefix(
            payload,
            "Subject Name of Subpoena:"
            if is_subpoena
            else "Subject Name of Search or Seizure:"
            if is_search_seizure
            else "Subject / Arrestee Name:",
        )
        citizen_ids = self._answers_for_prefix(payload, "Subject Citizen ID:")
        if is_subpoena:
            produce_dates = self._answers_for_prefix(
                payload, "Date to Produce Materials By or Appear"
            )
            subpoena_types = self._answers_for_prefix(payload, "Purpose of Subpoena:")
            subpoena_details = self._answers_for_prefix(
                payload, "Subpoena Details as It Will Appear on The Order:"
            )
            evidence_links = self._answers_for_prefix(payload, "Please include any evidence")
            count = max(
                len(subjects),
                len(citizen_ids),
                len(produce_dates),
                len(subpoena_types),
                len(subpoena_details),
                len(evidence_links),
            )
            return [
                "\n".join(
                    (
                        f"**Name:** {self._value_at(subjects, index)}",
                        f"**Citizen ID:** {self._value_at(citizen_ids, index)}",
                        f"**Date to produce / appear:** {self._value_at(produce_dates, index)}",
                        f"**Subpoena type:** {self._value_at(subpoena_types, index)}",
                        f"**Subpoena details:** {self._value_at(subpoena_details, index)}",
                        f"**Evidence links:** {self._value_at(evidence_links, index)}",
                    )
                )
                for index in range(count)
            ]

        details_label = "Warrant type" if is_search_seizure else "Initial charges"
        charges = self._answers_for_prefix(
            payload,
            "Purpose of Subpoena:"
            if is_subpoena
            else "Request Type:"
            if is_search_seizure
            else "Initial Charges To Be Filed Against Subject:",
        )
        probable_causes = self._answers_for_prefix(
            payload,
            "Subpoena Details as It Will Appear on The Order:"
            if is_subpoena
            else "Probable Cause For Search or Seizure:"
            if is_search_seizure
            else "Probable Cause For Arrest:",
        )
        evidence_links = self._answers_for_prefix(payload, "Please include any evidence")
        count = max([len(subjects), len(citizen_ids), len(charges), len(probable_causes), len(evidence_links)])
        return [
            "\n".join(
                (
                    f"**Name:** {self._value_at(subjects, index)}",
                    f"**Citizen ID:** {self._value_at(citizen_ids, index)}",
                    f"**{details_label}:** {self._value_at(charges, index)}",
                    f"**Probable cause:** {self._value_at(probable_causes, index)}",
                    f"**Evidence links:** {self._value_at(evidence_links, index)}",
                )
            )
            for index in range(count)
        ]

    @staticmethod
    def _discord_text_chunks(value: str, maximum_length: int = 3900) -> list[str]:
        """Split long evidence/probable-cause details without silently truncating them."""
        return [value[start : start + maximum_length] for start in range(0, len(value), maximum_length)]

    @staticmethod
    def _discord_embed_chunks(value: str, maximum_length: int = 1000) -> list[str]:
        """Keep every submitted evidence link within Discord's per-field limit."""
        return [value[start : start + maximum_length] for start in range(0, len(value), maximum_length)]

    @staticmethod
    def _normalize_username(value: str) -> str:
        return re.sub(r"^@", "", value.strip()).lower()

    def _request_type(self, submission: Submission) -> str:
        return self._answer(submission.payload, "Request Type").strip()

    def _is_arrest_warrant(self, submission: Submission) -> bool:
        return self._request_type(submission).casefold() == "arrest warrant"

    def _is_search_seizure_warrant(self, submission: Submission) -> bool:
        return self._request_type(submission).casefold() == "search or seizure warrant"

    def _is_subpoena(self, submission: Submission) -> bool:
        """Accept the main Subpoena type and its descriptive Form choice labels."""
        return self._request_type(submission).casefold().startswith("subpoena")

    def _has_dedicated_court_order_workflow(self, submission: Submission) -> bool:
        """Keep the intake notice limited to Court Order types the bot cannot process yet."""
        return (
            self._is_arrest_warrant(submission)
            or self._is_search_seizure_warrant(submission)
            or self._is_subpoena(submission)
        )

    @staticmethod
    def _answer(payload: dict[str, str], *names: str) -> str:
        for name in names:
            if payload.get(name):
                return payload[name]
        return ""

    @classmethod
    def _answer_prefix(cls, payload: dict[str, str], prefix: str) -> str:
        values = cls._answers_for_prefix(payload, prefix)
        return values[0] if values else ""

    async def on_ready(self) -> None:
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id if self.user else "unknown")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    master_deployment = load_master_deployment()
    if master_deployment is not None:
        settings = Settings.model_validate(master_deployment.runtime)
        config = master_deployment.environment
    else:
        settings = Settings()
        config = load_environment(settings.nerp_environment)
    NerpFormsBot(config, settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
