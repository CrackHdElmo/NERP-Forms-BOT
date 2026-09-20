"""Discord process entry point for the NERP Forms BOT scaffold."""

from __future__ import annotations

import asyncio
import logging
import re
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
            username = self._normalize_username(interaction.user.name)
            matches = await self.store.find_claimable("court_order", username)
            if not matches:
                await interaction.response.send_message(
                    "No unclaimed Court Order request matches your Discord username yet. "
                    "Submit the form first, then allow up to a minute for DOJ to synchronize it.",
                    ephemeral=True,
                )
                return
            if len(matches) > 1:
                await interaction.response.send_message(
                    "More than one unclaimed request matches your username. Please contact DOJ staff "
                    "so they can link the correct request safely.",
                    ephemeral=True,
                )
                return
            submission = matches[0]
            channel = self.get_channel(submission.discord_channel_id or 0)
            if not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Your request ticket could not be located. Please contact DOJ staff.", ephemeral=True
                )
                return
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
            name="generate-arrest-warrant",
            description="Create a one-page Arrest Warrant from a claimed Court Order request.",
            guild=guild,
        )
        @app_commands.describe(
            request_id="Court Order request ID, for example COR-000001.",
            docket_id="The docket ID to print on the warrant.",
            classified_designation="Classification to print on the warrant, or Unclassified.",
            primary_case_officer="Primary case officer name and title.",
            case_officer_agency="Agency responsible for the case officer.",
        )
        async def generate_arrest_warrant(
            interaction: discord.Interaction,
            request_id: str,
            docket_id: str,
            classified_designation: str,
            primary_case_officer: str,
            case_officer_agency: str,
        ) -> None:
            if not self._is_administrator(interaction):
                await interaction.response.send_message(
                    "Only a configured bot administrator can generate a warrant.", ephemeral=True
                )
                return
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
            if not submission.claimed_user_id:
                await interaction.response.send_message(
                    "The requester must first verify ownership with `/claim-court-order`.",
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
            replacements, validation_error = self._arrest_warrant_replacements(
                submission=submission,
                docket_id=docket_id,
                classified_designation=classified_designation,
                primary_case_officer=primary_case_officer,
                case_officer_agency=case_officer_agency,
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
        embed = discord.Embed(
            title=f"Court Order Request {submission.request_id}",
            description="Awaiting requester verification through `/claim-court-order`.",
            color=discord.Color.dark_blue(),
        )
        for label, field in (
            ("Requester", "Requestors Name:"),
            ("Requesting agency", "Requesting Agency:"),
            ("Request type", "Request Type"),
            ("Existing docket", "Please indicate the Docket Name/ID."),
            ("Subject", "Subject / Arrestee Name:"),
            ("Initial charges", "Initial Charges To Be Filed Against Subject:"),
            ("Probable cause", "Probable Cause For Arrest:"),
            ("Evidence links", "Please include any evidence"),
        ):
            value = self._answer_prefix(submission.payload, field)
            if value:
                embed.add_field(name=label, value=value[:1024], inline=False)
        await channel.send(embed=embed)
        return channel

    def _arrest_warrant_replacements(
        self,
        *,
        submission: Submission,
        docket_id: str,
        classified_designation: str,
        primary_case_officer: str,
        case_officer_agency: str,
    ) -> tuple[dict[str, str], str | None]:
        """Build template text without silently truncating player-facing facts."""
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

        replacements = {
            "{{REQUEST_ID}}": submission.request_id,
            "{{DOCKET_ID}}": docket_id.strip() or "N/A",
            "{{CLASSIFIED}}": classified_designation.strip() or "Unclassified",
            "{{REQUESTER_NAME}}": self._answer(submission.payload, "Requestors Name:") or "N/A",
            "{{REQUESTING_AGENCY}}": self._answer(submission.payload, "Requesting Agency:") or "N/A",
            "{{PCO}}": primary_case_officer.strip() or "N/A",
            "{{CASE_OFFICER_AGENCY}}": case_officer_agency.strip() or "N/A",
            "{{PROBABLE_CAUSE}}": probable_cause or "N/A",
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
