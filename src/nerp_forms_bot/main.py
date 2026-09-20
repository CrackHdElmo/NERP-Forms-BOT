"""Discord process entry point for the NERP Forms BOT scaffold."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import EnvironmentConfig, load_environment
from .google_workspace import GoogleWorkspaceService

LOGGER = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Runtime settings loaded from protected environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    nerp_environment: str = "test"
    google_service_account_file: str | None = None


class NerpFormsBot(discord.Client):
    """Minimal Discord client; workflow handlers are added in later milestones."""

    def __init__(self, environment: EnvironmentConfig, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.environment = environment
        self.settings = settings
        self.tree = app_commands.CommandTree(self)

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

        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id if self.user else "unknown")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    config = load_environment(settings.nerp_environment)
    NerpFormsBot(config, settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
