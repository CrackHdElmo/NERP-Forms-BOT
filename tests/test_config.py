from pathlib import Path

from nerp_forms_bot.config import load_environment, load_master_deployment


def test_production_profile_template_loads() -> None:
    root = Path(__file__).resolve().parents[1] / "config" / "environments"
    config = load_environment("production.example", root)

    assert config.guild.id == 0
    assert config.channels["docket_forum"] is None
    assert config.categories["off_docket_tickets"] is None
    assert config.workflows["attorney_request"].mode == "private_ticket_channel"
    assert config.google_workspace.drive_folder_id == "REPLACE_WITH_SHARED_DRIVE_FOLDER_ID"
    assert config.court_order_intake is not None
    assert config.court_order_intake.response_sheet_name == "Form Responses 1"
    assert config.court_order_warrant is not None
    assert config.court_order_warrant.template_document_id == "REPLACE_WITH_ARREST_WARRANT_TEMPLATE_ID"
    assert config.court_order_search_seizure_warrant is not None
    assert config.court_order_search_seizure_warrant.template_document_id == "REPLACE_WITH_SEARCH_SEIZURE_TEMPLATE_ID"


def test_private_master_deployment_loads_runtime_and_environment_once(tmp_path) -> None:
    master_path = tmp_path / "master.yaml"
    master_path.write_text(
        """
runtime:
  discord_token: private-token
  database_url: sqlite+aiosqlite:///./data/private.db
environment:
  environment: production
  guild:
    id: 42
    name: Private Deployment Server
  roles:
    administrators: [7]
""",
        encoding="utf-8",
    )

    deployment = load_master_deployment(master_path)

    assert deployment is not None
    assert deployment.runtime["discord_token"] == "private-token"
    assert deployment.environment.environment == "production"
    assert deployment.environment.guild.id == 42
    assert deployment.environment.roles["administrators"] == [7]
