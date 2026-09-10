from pathlib import Path

from nerp_forms_bot.config import load_environment


def test_altitude_government_profile_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "config" / "environments"
    config = load_environment("test", root)

    assert config.guild.id == 1414109505394053132
    assert config.channels["docket_forum"] == 1547671517805154304
    assert config.categories["off_docket_tickets"] == 1547691431185879180
    assert config.workflows["attorney_request"].mode == "private_ticket_channel"
