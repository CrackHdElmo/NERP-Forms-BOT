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
