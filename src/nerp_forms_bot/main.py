"""Discord process entry point for the NERP Forms BOT scaffold."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from io import BytesIO

import discord
from discord import app_commands
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import EnvironmentConfig, load_environment
from .fivemanage import FiveManageService
from .google_workspace import GoogleWorkspaceService, TrackingRow
from .submission_store import Submission, SubmissionStore

LOGGER = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Runtime settings loaded from protected environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    nerp_environment: str = "test"
    google_service_account_file: str | None = None
    database_url: str = "sqlite+aiosqlite:///./data/nerp_forms_bot.db"
    google_forms_poll_interval_seconds: int = 60
    fivemanage_api_token: str | None = None
    fivemanage_storage_path: str = "nerp-doj/court-orders"


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
        administrator_role_ids = set(self.environment.roles.get("administrators", []))
        return any(role.id in administrator_role_ids for role in member.roles)

    def _can_approve_warrant(self, interaction: discord.Interaction) -> bool:
        """Allow the configured judicial role; administrators remain a test-safe override."""
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

    def _role_ids(self, *role_groups: str) -> set[int]:
        """Resolve configured role groups while allowing test and production aliases."""
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
        if self._is_administrator(interaction) or member.id == submission.claimed_user_id:
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

    async def setup_hook(self) -> None:
        await self.store.initialize()
        guild = discord.Object(id=self.environment.guild.id)
        # Guild commands update immediately during development.  Clearing the scoped
        # tree first also replaces any stale command schema that Discord retained
        # from an earlier test run.
        self.tree.clear_commands(guild=guild)

        @self.tree.command(
            name="bot-status",
            description="Show the current NERP Forms BOT environment.",
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
                f"NERP Forms BOT is connected to **{self.environment.guild.name}** "
                f"using the **{self.environment.environment}** configuration profile.",
                ephemeral=True,
            )

        @self.tree.command(
            name="google-workspace-status",
            description="Verify Google access and create the test request tracker if needed.",
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
            folder_id = self.environment.google_workspace.test_drive_folder_id
            if not folder_id:
                await interaction.response.send_message(
                    "The test Drive folder ID is not configured for this environment.", ephemeral=True
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
                f"{action} the test request tracker: {tracker.url}", ephemeral=True
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
                await self.store.mark_claimed(submission.id, interaction.user.id)
                if submission.tracker_spreadsheet_id and submission.tracker_row:
                    await asyncio.to_thread(
                        GoogleWorkspaceService(
                            self.settings.google_service_account_file or "",
                            self.environment.google_workspace.test_drive_folder_id or "",
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
                for record_embed in self._case_record_embeds(
                    submission=submission,
                    closed_by=interaction.user,
                    closed_at=closed_at,
                    closure_note=closure_note,
                    source_deleted=isinstance(interaction.channel, discord.TextChannel),
                ):
                    await records_channel.send(embed=record_embed)
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
                        self.environment.google_workspace.test_drive_folder_id or "",
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
            if submission.status == "denied":
                await interaction.response.send_message(
                    "This Arrest Warrant was denied. Submit a corrected Court Order Form that references "
                    f"**{submission.request_id}** to continue in this request ticket.",
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
                        self.environment.google_workspace.test_drive_folder_id or "",
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

        await self.tree.sync(guild=guild)
        self._court_order_poll_task = asyncio.create_task(self._court_order_poll_loop())

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
            source_key = f"{intake.response_spreadsheet_id}:{row['_source_row']}"
            username = self._normalize_username(
                self._answer(row, "Discord Username", "Your Discord Name")
            )
            if not username:
                LOGGER.warning("Skipping Court Order response %s without a Discord username", source_key)
                continue
            submission = await self.store.begin_submission(
                source_key=source_key,
                workflow="court_order",
                requester_username=username,
                payload=row,
            )
            if not submission:
                continue
            try:
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

    async def _get_court_order_rows(self) -> tuple[GoogleWorkspaceService, list[dict[str, str]]]:
        intake = self.environment.court_order_intake
        if not intake or not self.settings.google_service_account_file:
            raise RuntimeError("Court Order Form intake is not configured on this host.")
        workspace = GoogleWorkspaceService(
            self.settings.google_service_account_file,
            self.environment.google_workspace.test_drive_folder_id or "",
        )
        rows = await asyncio.to_thread(
            workspace.get_form_response_rows,
            intake.response_spreadsheet_id,
            intake.response_sheet_name,
        )
        return workspace, rows

    async def _get_or_create_case_records_channel(self) -> discord.TextChannel:
        """Lazily create the permanent DOJ/PD-only records channel on first closure."""
        guild = self.get_guild(self.environment.guild.id)
        category_id = self.environment.categories.get("court_administration")
        category = guild.get_channel(category_id) if guild and category_id else None
        if not guild or not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("The Court Administration category is not configured correctly.")

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
            topic="NERP Forms BOT permanent DOJ/PD case and request closure records.",
            overwrites=overwrites,
            reason="NERP Forms BOT first request closure",
        )
        await self.store.set_resource_channel_id("doj_case_records", channel.id)
        return channel

    def _case_record_embeds(
        self,
        *,
        submission: Submission,
        closed_by: discord.abc.User,
        closed_at: str,
        closure_note: str | None,
        source_deleted: bool,
    ) -> list[discord.Embed]:
        """Create staff-only records, including all Form-submitted evidence links."""
        classified_answer = self._answer_prefix(
            submission.payload,
            'Has the Case or it\'s evidence been designated "Classified"',
        )
        classified = "Classified" if classified_answer.lower() in {"yes", "y"} else "Unclassified"
        subjects = self._answers_for_prefix(submission.payload, "Subject / Arrestee Name:")
        citizen_ids = self._answers_for_prefix(submission.payload, "Subject Citizen ID:")
        charges = self._answers_for_prefix(
            submission.payload, "Initial Charges To Be Filed Against Subject:"
        )
        evidence_links = self._answers_for_prefix(submission.payload, "Please include any evidence")
        subject_lines = [
            f"{number}. {self._value_at(subjects, number - 1)} "
            f"({self._value_at(citizen_ids, number - 1)}) — "
            f"{self._value_at(charges, number - 1)}"
            for number in range(1, max(len(subjects), len(citizen_ids), len(charges)) + 1)
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
                f"**Agency:** {self._answer(submission.payload, 'Requesting Agency:') or 'N/A'}\n"
                f"**Docket / Off-Docket:** {self._existing_docket_reference(submission.payload) or 'N/A'}"
            ),
            inline=False,
        )
        embed.add_field(name="Classification", value=classified, inline=True)
        embed.add_field(
            name="Status at closure", value=submission.status.replace("_", " ").title(), inline=True
        )
        embed.add_field(name="Closed by", value=closed_by.mention, inline=True)
        if submission.approved_by_name:
            embed.add_field(
                name="Judicial decision",
                value=f"Approved by <@{submission.approved_by_user_id}> on {submission.approved_at}",
                inline=False,
            )
        elif submission.denied_by_name:
            decision = f"Denied by <@{submission.denied_by_user_id}> on {submission.denied_at}"
            if submission.denial_note:
                decision += f"\n**Denial notes:** {submission.denial_note}"
            embed.add_field(name="Judicial decision", value=decision[:1024], inline=False)
        if subject_lines:
            embed.add_field(name="Subjects", value="\n".join(subject_lines)[:1024], inline=False)
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
                reason=f"NERP Forms BOT closed request {submission.request_id}",
            )
            return
        await channel.delete(
            reason=f"NERP Forms BOT closed request {submission.request_id}",
        )

    async def _create_court_order_ticket(self, submission: Submission) -> discord.TextChannel:
        guild = self.get_guild(self.environment.guild.id)
        category_id = self.environment.categories.get("off_docket_tickets")
        category = guild.get_channel(category_id) if guild and category_id else None
        if not guild or not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("The Off-Docket Requests category is not configured correctly.")
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
            name=f"court-order-{submission.request_id.lower()}",
            category=category,
            overwrites=overwrites,
            topic=f"Court Order request {submission.request_id}; awaiting claimant verification.",
            reason=f"NERP Forms BOT Court Order intake {submission.request_id}",
        )
        await self._post_court_order_intake(channel, submission)
        return channel

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
            title=f"Court Order Request {submission.request_id}",
            description="Awaiting requester verification through `/claim-court-order`.",
            color=discord.Color.dark_blue(),
        )
        for label, field in (
            ("Requester", "Requestors Name:"),
            ("Requesting agency", "Requesting Agency:"),
            ("Request type", "Request Type"),
            ("Existing docket / off-docket", "__existing_reference__"),
        ):
            value = (
                self._existing_docket_reference(submission.payload)
                if field == "__existing_reference__"
                else self._answer_prefix(submission.payload, field)
            )
            if value:
                embed.add_field(name=label, value=value[:1024], inline=False)
        await channel.send(embed=embed)
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

    async def _notify_warrant_fit_failure(
        self, channel: discord.TextChannel, submission: Submission, reason: str
    ) -> None:
        """Tell the verified requester why no player-facing warrant was issued."""
        requester = channel.guild.get_member(submission.claimed_user_id or 0)
        mention = requester.mention if requester else "The verified requester"
        await channel.send(
            f"{mention}, no player-facing Arrest Warrant was issued for **{submission.request_id}** "
            f"because {reason}. Please shorten or revise the relevant information with DOJ staff, "
            "then request a new warrant generation. Your information was not silently truncated."
        )

    @staticmethod
    def _answers_for_prefix(payload: dict[str, str], prefix: str) -> list[str]:
        return [value for name, value in payload.items() if name.startswith(prefix) and value]

    @staticmethod
    def _value_at(values: list[str], index: int) -> str:
        return values[index] if index < len(values) and values[index] else "N/A"

    def _court_order_subject_details(self, payload: dict[str, str]) -> list[str]:
        """Render every submitted subject in the private intake ticket without omitting repeats."""
        subjects = self._answers_for_prefix(payload, "Subject / Arrestee Name:")
        citizen_ids = self._answers_for_prefix(payload, "Subject Citizen ID:")
        charges = self._answers_for_prefix(payload, "Initial Charges To Be Filed Against Subject:")
        probable_causes = self._answers_for_prefix(payload, "Probable Cause For Arrest:")
        evidence_links = self._answers_for_prefix(payload, "Please include any evidence")
        count = max([len(subjects), len(citizen_ids), len(charges), len(probable_causes), len(evidence_links)])
        return [
            "\n".join(
                (
                    f"**Name:** {self._value_at(subjects, index)}",
                    f"**Citizen ID:** {self._value_at(citizen_ids, index)}",
                    f"**Initial charges:** {self._value_at(charges, index)}",
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

    @staticmethod
    def _answer(payload: dict[str, str], *names: str) -> str:
        for name in names:
            if payload.get(name):
                return payload[name]
        return ""

    @staticmethod
    def _answer_prefix(payload: dict[str, str], prefix: str) -> str:
        for name, value in payload.items():
            if name.startswith(prefix) and value:
                return value
        return ""

    async def on_ready(self) -> None:
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id if self.user else "unknown")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    config = load_environment(settings.nerp_environment)
    NerpFormsBot(config, settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
