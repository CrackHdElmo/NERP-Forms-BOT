import asyncio

from nerp_forms_bot.submission_store import SubmissionStore


def test_warrant_approval_is_recorded_once(tmp_path) -> None:
    async def exercise() -> tuple[bool, bool, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:2",
            workflow="court_order",
            requester_username="requester",
            payload={"Request Type": "Arrest Warrant"},
        )
        assert submission is not None
        first = await store.mark_warrant_approved(
            submission_id=submission.id,
            approver_user_id=42,
            approver_name="Judge Example",
            approved_at="2026-09-20T15:00:00+00:00",
            approved_document_id="approved-document-id",
        )
        second = await store.mark_warrant_approved(
            submission_id=submission.id,
            approver_user_id=43,
            approver_name="Judge Two",
            approved_at="2026-09-20T15:01:00+00:00",
            approved_document_id="different-document-id",
        )
        return first, second, await store.get_by_request_id(submission.request_id)

    first, second, saved = asyncio.run(exercise())

    assert first is True
    assert second is False
    assert saved is not None
    assert saved.status == "approved"
    assert saved.approved_by_user_id == 42
    assert saved.approved_by_name == "Judge Example"
    assert saved.approved_document_id == "approved-document-id"


def test_closure_is_recorded_once_and_resource_channel_is_reused(tmp_path) -> None:
    async def exercise() -> tuple[bool, bool, object, int | None]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:3",
            workflow="court_order",
            requester_username="requester",
            payload={"Request Type": "Arrest Warrant"},
        )
        assert submission is not None
        first = await store.mark_closed(
            submission_id=submission.id,
            closer_user_id=42,
            closer_name="Judge Example",
            closed_at="2026-09-20T15:00:00+00:00",
            closed_note="Approved and concluded.",
        )
        second = await store.mark_closed(
            submission_id=submission.id,
            closer_user_id=43,
            closer_name="Another Closer",
            closed_at="2026-09-20T15:01:00+00:00",
            closed_note=None,
        )
        await store.set_resource_channel_id("doj_case_records", 1234)
        saved = await store.get_by_request_id(submission.request_id)
        resource_id = await store.get_resource_channel_id("doj_case_records")
        return first, second, saved, resource_id

    first, second, saved, resource_id = asyncio.run(exercise())

    assert first is True
    assert second is False
    assert saved is not None
    assert saved.status == "closed"
    assert saved.closed_by_user_id == 42
    assert saved.closed_note == "Approved and concluded."
    assert resource_id == 1234


def test_denial_is_recorded_once_and_keeps_the_request_available(tmp_path) -> None:
    async def exercise() -> tuple[bool, bool, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:4",
            workflow="court_order",
            requester_username="requester",
            payload={"Request Type": "Arrest Warrant"},
        )
        assert submission is not None
        first = await store.mark_warrant_denied(
            submission_id=submission.id,
            denier_user_id=42,
            denier_name="Judge Example",
            denied_at="2026-09-20T15:00:00+00:00",
            denial_note="Add the missing probable cause details.",
        )
        second = await store.mark_warrant_denied(
            submission_id=submission.id,
            denier_user_id=43,
            denier_name="Judge Two",
            denied_at="2026-09-20T15:01:00+00:00",
            denial_note="Different reason.",
        )
        return first, second, await store.get_by_request_id(submission.request_id)

    first, second, saved = asyncio.run(exercise())

    assert first is True
    assert second is False
    assert saved is not None
    assert saved.status == "denied"
    assert saved.denied_by_user_id == 42
    assert saved.denied_by_name == "Judge Example"
    assert saved.denial_note == "Add the missing probable cause details."


def test_later_approval_preserves_the_earlier_denial_audit(tmp_path) -> None:
    async def exercise() -> tuple[bool, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:approval-after-denial",
            workflow="court_order",
            requester_username="requester",
            payload={"Request Type": "Arrest Warrant"},
        )
        assert submission is not None
        denied = await store.mark_warrant_denied(
            submission_id=submission.id,
            denier_user_id=42,
            denier_name="Judge One",
            denied_at="2026-09-20T15:00:00+00:00",
            denial_note="Initial review denied.",
        )
        approved = await store.mark_warrant_approved(
            submission_id=submission.id,
            approver_user_id=43,
            approver_name="Judge Two",
            approved_at="2026-09-20T15:10:00+00:00",
            approved_document_id="approved-document-id",
        )
        return denied and approved, await store.get_by_request_id(submission.request_id)

    recorded, saved = asyncio.run(exercise())

    assert recorded is True
    assert saved is not None
    assert saved.status == "approved"
    assert saved.denied_by_name == "Judge One"
    assert saved.denial_note == "Initial review denied."
    assert saved.approved_by_name == "Judge Two"


def test_open_submission_lookup_excludes_closed_requests(tmp_path) -> None:
    async def exercise() -> list[str]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        open_submission = await store.begin_submission(
            source_key="sheet:5",
            workflow="court_order",
            requester_username="requester",
            payload={},
        )
        closed_submission = await store.begin_submission(
            source_key="sheet:6",
            workflow="court_order",
            requester_username="requester",
            payload={},
        )
        assert open_submission and closed_submission
        await store.mark_ticket_created(open_submission.id, 100, None, None)
        await store.mark_ticket_created(closed_submission.id, 101, None, None)
        await store.mark_closed(
            submission_id=closed_submission.id,
            closer_user_id=42,
            closer_name="Administrator",
            closed_at="2026-09-20T15:00:00+00:00",
            closed_note=None,
        )
        return [item.request_id for item in await store.find_open_submissions("court_order")]

    assert asyncio.run(exercise()) == ["COR-000001"]


def test_form_refresh_updates_an_active_ticket_but_preserves_closed_case_data(tmp_path) -> None:
    async def exercise() -> tuple[bool, bool, object, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        active = await store.begin_submission(
            source_key="sheet:7",
            workflow="court_order",
            requester_username="requester",
            payload={"Subject Name of Search or Seizure:": "Missing before refresh"},
        )
        closed = await store.begin_submission(
            source_key="sheet:8",
            workflow="court_order",
            requester_username="requester",
            payload={"Request Type": "Arrest Warrant"},
        )
        assert active and closed
        await store.mark_closed(
            submission_id=closed.id,
            closer_user_id=42,
            closer_name="Administrator",
            closed_at="2026-09-20T15:00:00+00:00",
            closed_note=None,
        )
        refreshed_active = await store.refresh_active_submission_payload(
            source_key=active.source_key,
            workflow="court_order",
            requester_username="updated-requester",
            payload={"Subject Name of Search or Seizure:": "Current form value"},
        )
        refreshed_closed = await store.refresh_active_submission_payload(
            source_key=closed.source_key,
            workflow="court_order",
            requester_username="updated-requester",
            payload={"Request Type": "Changed"},
        )
        return (
            refreshed_active,
            refreshed_closed,
            await store.get_by_request_id(active.request_id),
            await store.get_by_request_id(closed.request_id),
        )

    refreshed_active, refreshed_closed, active, closed = asyncio.run(exercise())

    assert refreshed_active is True
    assert refreshed_closed is False
    assert active is not None
    assert active.requester_username == "updated-requester"
    assert active.payload["Subject Name of Search or Seizure:"] == "Current form value"
    assert closed is not None
    assert closed.payload == {"Request Type": "Arrest Warrant"}


def test_case_assignment_and_accepted_transfer_are_auditable(tmp_path) -> None:
    async def exercise() -> tuple[object, object, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:assignment",
            workflow="court_order",
            requester_username="requester",
            payload={},
        )
        assert submission is not None
        await store.set_case_assignment(
            submission_id=submission.id,
            assignment_type="prosecutor",
            user_id=10,
            user_name="Original Prosecutor",
            assigned_by_user_id=10,
            assigned_by_name="Original Prosecutor",
            assigned_at="2026-09-21T12:00:00+00:00",
        )
        offer = await store.propose_assignment_transfer(
            submission_id=submission.id,
            assignment_type="prosecutor",
            from_user_id=10,
            from_user_name="Original Prosecutor",
            to_user_id=11,
            to_user_name="Receiving Prosecutor",
            proposed_by_user_id=10,
            proposed_by_name="Original Prosecutor",
            proposed_at="2026-09-21T12:01:00+00:00",
        )
        pending = await store.get_pending_assignment_transfer(
            submission_id=submission.id,
            assignment_type="prosecutor",
            recipient_user_id=11,
        )
        accepted = await store.accept_assignment_transfer(
            offer,
            accepted_at="2026-09-21T12:02:00+00:00",
        )
        return accepted, await store.get_case_assignments(submission.id), pending

    accepted, assignments, pending = asyncio.run(exercise())

    assert accepted is True
    assert pending is not None
    assert assignments[0].assignment_type == "prosecutor"
    assert assignments[0].user_id == 11
    assert assignments[0].assigned_by_user_id == 10


def test_court_order_docket_merge_tracks_its_destination_and_sync_checkpoint(tmp_path) -> None:
    async def exercise() -> tuple[object, object]:
        store = SubmissionStore(f"sqlite+aiosqlite:///{tmp_path / 'tracker.db'}")
        await store.initialize()
        submission = await store.begin_submission(
            source_key="sheet:merge",
            workflow="court_order",
            requester_username="requester",
            payload={},
        )
        assert submission is not None
        await store.mark_ticket_created(submission.id, 100, None, None)
        await store.record_court_order_docket_merge(
            submission_id=submission.id,
            docket_thread_id=200,
            merged_by_user_id=42,
            merged_by_name="Judge Example",
            merged_at="2026-09-21T12:00:00+00:00",
            last_forwarded_message_id=300,
            source_closed=False,
        )
        await store.record_court_order_docket_merge(
            submission_id=submission.id,
            docket_thread_id=200,
            merged_by_user_id=42,
            merged_by_name="Judge Example",
            merged_at="2026-09-21T12:05:00+00:00",
            last_forwarded_message_id=301,
            source_closed=True,
        )
        return (
            await store.get_court_order_docket_merge(submission.id),
            await store.get_by_channel_id(200),
        )

    merge, linked_submission = asyncio.run(exercise())

    assert merge is not None
    assert merge.docket_thread_id == 200
    assert merge.last_forwarded_message_id == 301
    assert merge.source_closed is True
    assert linked_submission is not None
    assert linked_submission.request_id == "COR-000001"
