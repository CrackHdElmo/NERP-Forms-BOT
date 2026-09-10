"""Discord process entry point for the NERP Forms BOT scaffold."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import EnvironmentConfig, load_environment

LOGGER = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Runtime settings loaded from protected environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    nerp_environment: str = "test"


class NerpFormsBot(discord.Client):
    """Minimal Discord client; workflow handlers are added in later milestones."""

    def __init__(self, environment: EnvironmentConfig) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.environment = environment
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.environment.guild.id)
        self.tree.add_command(self.bot_status, guild=guild)
        await self.tree.sync(guild=guild)

    @app_commands.command(name="bot-status", description="Show the current NERP Forms BOT environment.")
    async def bot_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.environment.guild.id:
            await interaction.response.send_message(
                "This command is only configured for the selected NERP environment.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"NERP Forms BOT is connected to **{self.environment.guild.name}** "
            f"using the **{self.environment.environment}** configuration profile.",
            ephemeral=True,
        )

    async def on_ready(self) -> None:
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id if self.user else "unknown")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    config = load_environment(settings.nerp_environment)
    NerpFormsBot(config).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    run()
