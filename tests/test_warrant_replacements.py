from pathlib import Path

from nerp_forms_bot.config import load_environment
from nerp_forms_bot.main import NerpFormsBot, Settings
from nerp_forms_bot.submission_store import Submission


def _bot() -> NerpFormsBot:
    root = Path(__file__).resolve().parents[1] / "config" / "environments"
    return NerpFormsBot(load_environment("test", root), Settings(discord_token="test-token"))


def _submission(payload: dict[str, str]) -> Submission:
    return Submission(
        id=1,
        request_id="COR-000001",
        source_key="source:2",
        workflow="court_order",
        requester_username="tester",
        payload=payload,
        status="awaiting_review",
        discord_channel_id=1,
        tracker_spreadsheet_id=None,
        tracker_row=None,
        claimed_user_id=2,
        approved_by_user_id=None,
        approved_by_name=None,
        approved_at=None,
        approved_document_id=None,
    )


def test_warrant_replacements_keep_six_subjects_distinct() -> None:
    payload = {
        "Requestors Name:": "Officer Example",
        "Requesting Agency:": "LSPD",
        "Subject / Arrestee Name:": "Subject One",
        "Subject / Arrestee Name: (2)": "Subject Two",
        "Subject Citizen ID:": "1001",
        "Subject Citizen ID: (2)": "1002",
        "Initial Charges To Be Filed Against Subject:": "Charge one",
        "Initial Charges To Be Filed Against Subject: (2)": "Charge two",
        "Probable Cause For Arrest:": "A concise probable-cause statement.",
        "Please indicate the Docket Name/ID.": "D-42",
        'Has the Case or it\'s evidence been designated "Classified" by The Commissioner\'s Officer or FIB? ': "No",
        "Primary Case Officer": "Officer Example",
        "Law Enforcement Agency Assigned to Case:": "LSPD",
    }
    replacements, error = _bot()._arrest_warrant_replacements(submission=_submission(payload))

    assert error is None
    assert replacements["{{SUBJECT_1_NAME}}"] == "Subject One"
    assert replacements["{{SUBJECT_2_NAME}}"] == "Subject Two"
    assert replacements["{{S1ID}}"] == "1001"
    assert replacements["{{S2ID}}"] == "1002"
    assert replacements["{{SUBJECT_6_NAME}}"] == "N/A"
    assert replacements["{{DOCKET_ID}}"] == "D-42"
    assert replacements["{{CLASSIFIED}}"] == "Unclassified"
    assert replacements["{{PCO}}"] == "Officer Example"
    assert replacements["{{CASE_OFFICER_AGENCY}}"] == "LSPD"
    assert "{{EVIDENCE_LINKS}}" not in replacements


def test_warrant_replacements_reject_overlong_player_facing_text() -> None:
    payload = {
        "Initial Charges To Be Filed Against Subject:": "x" * 89,
        "Probable Cause For Arrest:": "A valid statement.",
    }
    _, error = _bot()._arrest_warrant_replacements(submission=_submission(payload))

    assert error == "subject 1's initial charges exceed the 88-character limit"


def test_private_ticket_renders_each_submitted_subject() -> None:
    payload = {
        "Subject / Arrestee Name:": "Subject One",
        "Subject / Arrestee Name: (2)": "Subject Two",
        "Subject Citizen ID:": "1001",
        "Subject Citizen ID: (2)": "1002",
        "Initial Charges To Be Filed Against Subject:": "Charge one",
        "Initial Charges To Be Filed Against Subject: (2)": "Charge two",
        "Probable Cause For Arrest:": "Cause one",
        "Probable Cause For Arrest: (2)": "Cause two",
        "Please include any evidence": "https://example.test/evidence-one",
        "Please include any evidence (2)": "https://example.test/evidence-two",
    }

    details = _bot()._court_order_subject_details(payload)

    assert len(details) == 2
    assert "Subject One" in details[0]
    assert "Charge one" in details[0]
    assert "Cause one" in details[0]
    assert "evidence-one" in details[0]
    assert "Subject Two" in details[1]
    assert "Charge two" in details[1]
    assert "Cause two" in details[1]
    assert "evidence-two" in details[1]
