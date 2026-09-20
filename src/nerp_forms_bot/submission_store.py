"""Small SQLite persistence layer for idempotent Form intake and ticket claims."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class Submission:
    id: int
    request_id: str
    source_key: str
    workflow: str
    requester_username: str
    payload: dict[str, str]
    status: str
    discord_channel_id: int | None
    tracker_spreadsheet_id: str | None
    tracker_row: int | None


class SubmissionStore:
    """Persist every source row before creating a Discord resource for it."""

    def __init__(self, database_url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError("The initial NERP Forms BOT tracker supports SQLite database URLs only.")
        self.path = Path(database_url.removeprefix(prefix))

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE,
                    source_key TEXT UNIQUE NOT NULL,
                    workflow TEXT NOT NULL,
                    requester_username TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    discord_channel_id INTEGER,
                    tracker_spreadsheet_id TEXT,
                    tracker_row INTEGER,
                    claimed_user_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await database.commit()

    async def begin_submission(
        self,
        *,
        source_key: str,
        workflow: str,
        requester_username: str,
        payload: dict[str, str],
    ) -> Submission | None:
        """Create one durable source-row record, or return None for a duplicate."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO submissions
                (source_key, workflow, requester_username, payload_json, status)
                VALUES (?, ?, ?, ?, 'processing')
                """,
                (source_key, workflow, requester_username, json.dumps(payload, sort_keys=True)),
            )
            if cursor.rowcount == 0:
                return None
            request_id = f"COR-{cursor.lastrowid:06d}"
            await database.execute(
                "UPDATE submissions SET request_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (request_id, cursor.lastrowid),
            )
            await database.commit()
            return Submission(
                id=cursor.lastrowid,
                request_id=request_id,
                source_key=source_key,
                workflow=workflow,
                requester_username=requester_username,
                payload=payload,
                status="processing",
                discord_channel_id=None,
                tracker_spreadsheet_id=None,
                tracker_row=None,
            )

    async def mark_ticket_created(
        self,
        submission_id: int,
        channel_id: int,
        tracker_spreadsheet_id: str | None,
        tracker_row: int | None,
    ) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                UPDATE submissions
                SET status = 'awaiting_claim', discord_channel_id = ?,
                    tracker_spreadsheet_id = ?, tracker_row = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (channel_id, tracker_spreadsheet_id, tracker_row, submission_id),
            )
            await database.commit()

    async def mark_failed(self, submission_id: int) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                "UPDATE submissions SET status = 'error', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (submission_id,),
            )
            await database.commit()

    async def find_claimable(self, workflow: str, requester_username: str) -> list[Submission]:
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT * FROM submissions
                WHERE workflow = ? AND requester_username = ? AND status = 'awaiting_claim'
                      AND claimed_user_id IS NULL AND discord_channel_id IS NOT NULL
                ORDER BY id DESC LIMIT 2
                """,
                (workflow, requester_username),
            )
            rows = await cursor.fetchall()
        return [self._submission_from_row(row) for row in rows]

    async def mark_claimed(self, submission_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                UPDATE submissions
                SET status = 'awaiting_review', claimed_user_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (user_id, submission_id),
            )
            await database.commit()

    @staticmethod
    def _submission_from_row(row: aiosqlite.Row) -> Submission:
        return Submission(
            id=row["id"],
            request_id=row["request_id"],
            source_key=row["source_key"],
            workflow=row["workflow"],
            requester_username=row["requester_username"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            discord_channel_id=row["discord_channel_id"],
            tracker_spreadsheet_id=row["tracker_spreadsheet_id"],
            tracker_row=row["tracker_row"],
        )
