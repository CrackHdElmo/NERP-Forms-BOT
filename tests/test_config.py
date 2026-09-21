from pathlib import Path

from nerp_forms_bot.config import load_environment, load_master_deployment


def test_altitude_government_profile_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "config" / "environments"
    config = load_environment("test", root)

    assert config.guild.id == 1414109505394053132
    assert config.channels["docket_forum"] == 1547671517805154304
    assert config.categories["off_docket_tickets"] == 1547691431185879180
    assert config.workflows["attorney_request"].mode == "private_ticket_channel"
    assert config.google_workspace.test_drive_folder_id == "1tSB26NtY7wX5I0yRhUfVrRXj0B3ADItb"
    assert config.court_order_intake is not None
    assert config.court_order_intake.response_sheet_name == "Form Responses 1"
    assert config.court_order_warrant is not None
    assert config.court_order_warrant.template_document_id == "1Mp2ALt82lIVIJ_d9lr4TD0kx7U_ybWF9gBV4eZtWl2o"
    assert config.court_order_search_seizure_warrant is not None
    assert (
        config.court_order_search_seizure_warrant.template_document_id
        == "12wCoKHco_ZVoxR5CMXXS0KbB_BUG3J7ptCBeAnNqUjo"
    )


def test_private_master_deployment_loads_runtime_and_environment_once(tmp_path) -> None:
    master_path = tmp_path / "master.yaml"
    master_path.write_text(
        """
runtime:
  discord_token: private-token
  database_url: sqlite+aiosqlite:///./data/private.db
environment:
  environment: private-test
  guild:
    id: 42
    name: Private Test Server
  roles:
    administrators: [7]
""",
        encoding="utf-8",
    )

    deployment = load_master_deployment(master_path)

    assert deployment is not None
    assert deployment.runtime["discord_token"] == "private-token"
    assert deployment.environment.environment == "private-test"
    assert deployment.environment.guild.id == 42
    assert deployment.environment.roles["administrators"] == [7]
