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
