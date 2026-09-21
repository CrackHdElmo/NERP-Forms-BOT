import asyncio
from pathlib import Path
from types import SimpleNamespace

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
        denied_by_user_id=None,
        denied_by_name=None,
        denied_at=None,
        denial_note=None,
        closed_by_user_id=None,
        closed_by_name=None,
        closed_at=None,
        closed_note=None,
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


def test_docket_or_off_docket_reference_accepts_the_updated_form_question() -> None:
    payload = {
        "Please indicate the Docket or Off-Docket Name/ID .": "COR-000006",
    }

    assert _bot()._existing_docket_reference(payload) == "COR-000006"


def test_docket_or_off_docket_reference_accepts_google_forms_nonbreaking_spaces() -> None:
    payload = {
        "Please indicate the\u00a0 Docket or Off-Docket Name/ID\u00a0 .": "COR-000007",
    }

    assert _bot()._existing_docket_reference(payload) == "COR-000007"


def test_court_order_ticket_name_uses_requester_username_then_server_nickname() -> None:
    bot = _bot()
    submission = _submission({})

    assert bot._court_order_ticket_name(submission) == "cor-000001-tester"
    assert (
        bot._court_order_ticket_name(
            submission,
            SimpleNamespace(nick="Nik Skyy", name="nikskyy"),
        )
        == "cor-000001-nik-skyy"
    )


def test_only_arrest_warrant_uses_arrest_warrant_commands() -> None:
    bot = _bot()

    assert bot._is_arrest_warrant(_submission({"Request Type": "Arrest Warrant"}))
    assert not bot._is_arrest_warrant(_submission({"Request Type": "Search or Seizure Warrant"}))


def test_search_seizure_warrant_is_a_supported_court_order_workflow() -> None:
    bot = _bot()

    assert bot._has_dedicated_court_order_workflow(
        _submission({"Request Type": "Arrest Warrant"})
    )
    assert bot._has_dedicated_court_order_workflow(
        _submission({"Request Type": "Search or Seizure Warrant"})
    )
    assert bot._has_dedicated_court_order_workflow(
        _submission({"Request Type": "Subpoena"})
    )


def test_search_seizure_warrant_replacements_keep_three_subjects_distinct() -> None:
    payload = {
        "Request Type": "Search or Seizure Warrant",
        "Requestors Name:": "Officer Example",
        "Requesting Agency:": "LSPD",
        "Subject Name of Search or Seizure:": "Property One",
        "Subject Name of Search or Seizure: (2)": "Vehicle Two",
        "Subject Citizen ID:": "1001",
        "Subject Citizen ID: (2)": "1002",
        "Date Of Incident:": "September 20, 2026",
        "Date Of Incident: (2)": "September 19, 2026",
        "Request Type:": "Search",
        "Request Type: (2)": "Seizure",
        "Probable Cause For Search or Seizure:": "Cause one.",
        "Probable Cause For Search or Seizure: (2)": "Cause two.",
    }

    replacements, error = _bot()._search_seizure_warrant_replacements(
        submission=_submission(payload)
    )

    assert error is None
    assert replacements["{{SUBJECT_1_NAME}}"] == "Property One"
    assert replacements["{{SUBJECT_2_NAME}}"] == "Vehicle Two"
    assert replacements["{{SUBJECT_1_INCIDENT_DATE}}"] == "September 20, 2026"
    assert replacements["{{SUBJECT_2_WARRANT_TYPE}}"] == "Seizure"
    assert replacements["{{SUBJECT_3_NAME}}"] == "N/A"
    assert replacements["{{PROBABLE_CAUSE}}"] == "Cause one.\n\nCause two."


def test_search_seizure_replacements_reject_more_than_three_subjects() -> None:
    payload = {
        "Request Type": "Search or Seizure Warrant",
        "Subject Name of Search or Seizure:": "One",
        "Subject Name of Search or Seizure: (2)": "Two",
        "Subject Name of Search or Seizure: (3)": "Three",
        "Subject Name of Search or Seizure: (4)": "Four",
    }

    _, error = _bot()._search_seizure_warrant_replacements(submission=_submission(payload))

    assert error == "the request contains more than the supported three subjects"


def test_subpoena_replacements_match_the_live_template_placeholders() -> None:
    payload = {
        "Request Type": "Subpoena",
        "Requestors Name:": "Officer Example",
        "Requesting Agency:": "LSPD",
        "Subject Name of Subpoena:": "Records Custodian",
        "Subject Name of Subpoena: (2)": "Witness Example",
        "Subject Citizen ID:": "1001",
        "Subject Citizen ID: (2)": "1002",
        "Date to Produce Materials By or Appear:": "September 25, 2026",
        "Date to Produce Materials By or Appear By: (2)": "September 26, 2026",
        "Purpose of Subpoena:": "Produce records",
        "Purpose of Subpoena: (2)": "Appear to testify",
        "Subpoena Details as It Will Appear on The Order:": "Produce the requested records.",
        "Subpoena Details as It Will Appear on The Order: (2)": "Appear before the Court.",
    }

    replacements, error = _bot()._subpoena_replacements(submission=_submission(payload))

    assert error is None
    assert replacements["{{SUBJECT_1_NAME}}"] == "Records Custodian"
    assert replacements["{{SUBJECT_2_NAME}}"] == "Witness Example"
    assert replacements["{{SUBJECT_1_DATE}}"] == "September 25, 2026"
    assert replacements["{{SUBJECT_2_DATE}}"] == "September 26, 2026"
    assert replacements["{{SUBJECT_1_SUBPOENA_TYPE}}"] == "Produce records"
    assert replacements["{{SUBJECT_3_NAME}}"] == "N/A"
    assert replacements["{{SUBPOENA_DETAILS}}"] == (
        "Produce the requested records.\n\nAppear before the Court."
    )


def test_subpoena_is_a_supported_court_order_workflow() -> None:
    bot = _bot()

    assert bot._is_subpoena(_submission({"Request Type": "Subpoena"}))
    assert bot._has_dedicated_court_order_workflow(_submission({"Request Type": "Subpoena"}))


def test_subpoena_descriptive_form_choice_uses_the_dedicated_workflow() -> None:
    bot = _bot()
    submission = _submission(
        {"Request Type": "Subpoena - Documents/Media & Order to Appear"}
    )

    assert bot._is_subpoena(submission)
    assert bot._has_dedicated_court_order_workflow(submission)


def test_subpoena_private_ticket_uses_subpoena_specific_subject_fields() -> None:
    payload = {
        "Request Type": "Subpoena - Order to Produce Documents, Media, Materials",
        "Subject Name of Subpoena:": "Records Custodian",
        "Subject Citizen ID:": "DHDY5287",
        "Date to Produce Materials By or Appear:": "September 24, 2026",
        "Purpose of Subpoena:": "Order to appear",
        "Subpoena Details as It Will Appear on The Order:": "Appear before the Court.",
        "Please include any evidence": "https://example.test/subpoena-evidence",
    }

    details = _bot()._court_order_subject_details(payload)

    assert len(details) == 1
    assert "Records Custodian" in details[0]
    assert "DHDY5287" in details[0]
    assert "September 24, 2026" in details[0]
    assert "Order to appear" in details[0]
    assert "Appear before the Court." in details[0]
    assert "subpoena-evidence" in details[0]
    assert "Initial charges" not in details[0]
    assert "Probable cause" not in details[0]


def test_refresh_court_order_reloads_the_original_form_row(tmp_path: Path) -> None:
    async def exercise() -> tuple[Submission, Submission | None]:
        root = Path(__file__).resolve().parents[1] / "config" / "environments"
        bot = NerpFormsBot(
            load_environment("test", root),
            Settings(
                discord_token="test-token",
                google_service_account_file="service-account.json",
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}",
            ),
        )
        await bot.store.initialize()
        intake = bot.environment.court_order_intake
        assert intake is not None
        original = await bot.store.begin_submission(
            source_key=f"{intake.response_spreadsheet_id}:9",
            workflow="court_order",
            requester_username="old-requester",
            payload={"Request Type": "Search or Seizure Warrant"},
        )
        assert original is not None

        async def source_rows() -> tuple[None, list[dict[str, str]]]:
            return (
                None,
                [
                    {
                        "_source_row": "9",
                        "Discord Username": "refreshed-requester",
                        "Request Type": "Search or Seizure Warrant",
                        "Subject Name of Search or Seizure:": "Updated property",
                    }
                ],
            )

        bot._get_court_order_rows = source_rows  # type: ignore[method-assign]
        return original, await bot._refresh_court_order_submission(original)

    original, refreshed = asyncio.run(exercise())

    assert refreshed is not None
    assert refreshed.id == original.id
    assert refreshed.request_id == original.request_id
    assert refreshed.requester_username == "refreshed-requester"
    assert refreshed.payload["Subject Name of Search or Seizure:"] == "Updated property"


def test_setup_plan_uses_safe_resource_names_without_changing_category_case() -> None:
    plan = _bot()._default_setup_plan(
        "court_administration",
        {
            "court_administration_category": "Court Operations",
            "service_desk_channel": "Service Desk!",
            "docket_forum_channel": "Active Dockets",
            "doj_case_records": "DOJ Case Records",
        },
    )

    assert plan.names["court_administration_category"] == "Court Operations"
    assert plan.names["service_desk_channel"] == "service-desk"
    assert plan.names["docket_forum_channel"] == "active-dockets"
    assert plan.names["doj_case_records"] == "doj-case-records"


def test_service_desk_embed_uses_configured_title_image_and_link_sections() -> None:
    embed = _bot()._service_desk_embed(
        {
            "title": "Court & Case Services",
            "description": "Start here.",
            "image_url": "https://example.test/banner.png",
            "form_links": [{"label": "Case Management Form", "url": "https://example.test/form"}],
            "external_links": [{"label": "Court SOP", "url": "https://example.test/sop"}],
        }
    )

    assert embed.title == "Court & Case Services"
    assert embed.image.url == "https://example.test/banner.png"
    assert embed.fields[0].name == "Case & Court Forms"
    assert "Case Management Form" in embed.fields[0].value
    assert embed.fields[1].name == "Policies & External Resources"
    assert "Court SOP" in embed.fields[1].value
