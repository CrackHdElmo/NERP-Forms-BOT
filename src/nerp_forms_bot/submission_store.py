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
    claimed_user_id: int | None
    approved_by_user_id: int | None
    approved_by_name: str | None
    approved_at: str | None
    approved_document_id: str | None
    denied_by_user_id: int | None
    denied_by_name: str | None
    denied_at: str | None
    denial_note: str | None
    closed_by_user_id: int | None
    closed_by_name: str | None
    closed_at: str | None
    closed_note: str | None


@dataclass(frozen=True)
class CaseAssignment:
    """One current judicial or counsel assignment for a tracked request."""

    submission_id: int
    assignment_type: str
    assignment_slot: int
    user_id: int
    user_name: str
    assigned_by_user_id: int
    assigned_by_name: str
    assigned_at: str


@dataclass(frozen=True)
class AssignmentTransfer:
    """A pending or completed handoff that retains who initiated it."""

    id: int
    submission_id: int
    assignment_type: str
    assignment_slot: int
    from_user_id: int | None
    from_user_name: str | None
    to_user_id: int
    to_user_name: str
    proposed_by_user_id: int
    proposed_by_name: str
    proposed_at: str
    status: str
    accepted_at: str | None


@dataclass(frozen=True)
class CourtOrderDocketMerge:
    """Audit state for Court Order messages forwarded into a Docket Forum post."""

    submission_id: int
    docket_thread_id: int
    merged_by_user_id: int
    merged_by_name: str
    merged_at: str
    last_forwarded_message_id: int | None
    source_closed: bool


@dataclass(frozen=True)
class BotAdministrator:
    """One direct, durable bot-administrator grant made inside Discord."""

    user_id: int
    user_name: str
    added_by_user_id: int
    added_by_name: str
    added_at: str


@dataclass(frozen=True)
class DiscordRoleMapping:
    """One Discord role assigned to a durable NERP function category."""

    function_key: str
    role_id: int
    added_by_user_id: int
    added_by_name: str
    added_at: str


class SubmissionStore:
    """Persist every source row before creating a Discord resource for it."""

    def __init__(self, database_url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not database_url.startswith(prefix):
            raise ValueError("The initial NERP - Case Management tracker supports SQLite database URLs only.")
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
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS case_assignments (
                    submission_id INTEGER NOT NULL,
                    assignment_type TEXT NOT NULL,
                    assignment_slot INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    assigned_by_user_id INTEGER NOT NULL,
                    assigned_by_name TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY (submission_id, assignment_type, assignment_slot)
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS assignment_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER NOT NULL,
                    assignment_type TEXT NOT NULL,
                    assignment_slot INTEGER NOT NULL,
                    from_user_id INTEGER,
                    from_user_name TEXT,
                    to_user_id INTEGER NOT NULL,
                    to_user_name TEXT NOT NULL,
                    proposed_by_user_id INTEGER NOT NULL,
                    proposed_by_name TEXT NOT NULL,
                    proposed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    accepted_at TEXT
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS court_order_docket_merges (
                    submission_id INTEGER PRIMARY KEY,
                    docket_thread_id INTEGER NOT NULL,
                    merged_by_user_id INTEGER NOT NULL,
                    merged_by_name TEXT NOT NULL,
                    merged_at TEXT NOT NULL,
                    last_forwarded_message_id INTEGER,
                    source_closed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_resources (
                    resource_key TEXT PRIMARY KEY,
                    discord_channel_id INTEGER NOT NULL
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_administrators (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    added_by_user_id INTEGER NOT NULL,
                    added_by_name TEXT NOT NULL,
                    added_at TEXT NOT NULL
                )
                """
            )
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_role_mappings (
                    function_key TEXT NOT NULL,
                    role_id INTEGER NOT NULL,
                    added_by_user_id INTEGER NOT NULL,
                    added_by_name TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (function_key, role_id)
                )
                """
            )
            await self._add_missing_columns(database)
            await self._migrate_case_assignment_slots(database)
            await database.commit()

    @staticmethod
    async def _add_missing_columns(database: aiosqlite.Connection) -> None:
        """Apply additive migrations without disrupting existing stored submissions."""
        cursor = await database.execute("PRAGMA table_info(submissions)")
        existing = {row[1] for row in await cursor.fetchall()}
        for name, definition in (
            ("approved_by_user_id", "INTEGER"),
            ("approved_by_name", "TEXT"),
            ("approved_at", "TEXT"),
            ("approved_document_id", "TEXT"),
            ("denied_by_user_id", "INTEGER"),
            ("denied_by_name", "TEXT"),
            ("denied_at", "TEXT"),
            ("denial_note", "TEXT"),
            ("closed_by_user_id", "INTEGER"),
            ("closed_by_name", "TEXT"),
            ("closed_at", "TEXT"),
            ("closed_note", "TEXT"),
        ):
            if name not in existing:
                await database.execute(f"ALTER TABLE submissions ADD COLUMN {name} {definition}")

    @staticmethod
    async def _migrate_case_assignment_slots(database: aiosqlite.Connection) -> None:
        """Expand the original single-holder assignment table without losing existing audit data."""
        cursor = await database.execute("PRAGMA table_info(case_assignments)")
        assignment_columns = {row[1] for row in await cursor.fetchall()}
        if "assignment_slot" not in assignment_columns:
            await database.execute("ALTER TABLE case_assignments RENAME TO case_assignments_legacy")
            await database.execute(
                """
                CREATE TABLE case_assignments (
                    submission_id INTEGER NOT NULL,
                    assignment_type TEXT NOT NULL,
                    assignment_slot INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    assigned_by_user_id INTEGER NOT NULL,
                    assigned_by_name TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY (submission_id, assignment_type, assignment_slot)
                )
                """
            )
            await database.execute(
                """
                INSERT INTO case_assignments
                (submission_id, assignment_type, assignment_slot, user_id, user_name,
                 assigned_by_user_id, assigned_by_name, assigned_at)
                SELECT submission_id, assignment_type, 1, user_id, user_name,
                       assigned_by_user_id, assigned_by_name, assigned_at
                FROM case_assignments_legacy
                """
            )
            await database.execute("DROP TABLE case_assignments_legacy")

        cursor = await database.execute("PRAGMA table_info(assignment_transfers)")
        transfer_columns = {row[1] for row in await cursor.fetchall()}
        if "assignment_slot" not in transfer_columns:
            await database.execute(
                "ALTER TABLE assignment_transfers ADD COLUMN assignment_slot INTEGER NOT NULL DEFAULT 1"
            )

    async def begin_submission(
        self,
        *,
        source_key: str,
        workflow: str,
        requester_username: str,
        payload: dict[str, str],
        request_prefix: str = "COR",
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
            request_id = f"{request_prefix.upper()}-{cursor.lastrowid:06d}"
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
                claimed_user_id=None,
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

    async def record_baseline(
        self, *, source_key: str, workflow: str, requester_username: str, payload: dict[str, str]
    ) -> bool:
        """Mark a pre-existing source row as seen without creating a Discord ticket."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO submissions
                (source_key, workflow, requester_username, payload_json, status)
                VALUES (?, ?, ?, ?, 'baseline')
                """,
                (source_key, workflow, requester_username, json.dumps(payload, sort_keys=True)),
            )
            await database.commit()
            return cursor.rowcount == 1

    async def mark_ticket_created(
        self,
        submission_id: int,
        channel_id: int,
        tracker_spreadsheet_id: str | None,
        tracker_row: int | None,
        *,
        status: str = "awaiting_claim",
    ) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                UPDATE submissions
                SET status = ?, discord_channel_id = ?,
                    tracker_spreadsheet_id = ?, tracker_row = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, channel_id, tracker_spreadsheet_id, tracker_row, submission_id),
            )
            await database.commit()

    async def mark_failed(self, submission_id: int) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                "UPDATE submissions SET status = 'error', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (submission_id,),
            )
            await database.commit()

    async def refresh_active_submission_payload(
        self,
        *,
        source_key: str,
        workflow: str,
        requester_username: str,
        payload: dict[str, str],
    ) -> bool:
        """Refresh an open ticket's Form data without creating a second request or altering closure records."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE submissions
                SET requester_username = ?, payload_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE source_key = ? AND workflow = ? AND status != 'closed'
                """,
                (
                    requester_username,
                    json.dumps(payload, sort_keys=True),
                    source_key,
                    workflow,
                ),
            )
            await database.commit()
        return cursor.rowcount == 1

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

    async def get_by_request_id(self, request_id: str) -> Submission | None:
        """Return one submission so staff commands cannot act on an arbitrary ticket."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                "SELECT * FROM submissions WHERE request_id = ?", (request_id.upper(),)
            )
            row = await cursor.fetchone()
        return self._submission_from_row(row) if row else None

    async def get_by_id(self, submission_id: int) -> Submission | None:
        """Return a tracked request by its durable internal identifier."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            row = await cursor.fetchone()
        return self._submission_from_row(row) if row else None

    async def get_by_source_key(self, source_key: str) -> Submission | None:
        """Return the durable record for one Form response row."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                "SELECT * FROM submissions WHERE source_key = ?", (source_key,)
            )
            row = await cursor.fetchone()
        return self._submission_from_row(row) if row else None

    async def requeue_failed_uncreated_submission(
        self,
        *,
        submission_id: int,
        requester_username: str,
        payload: dict[str, str],
    ) -> bool:
        """Retry a Form response only when its prior failure created no Discord resource."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE submissions
                SET status = 'processing', requester_username = ?, payload_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'error' AND discord_channel_id IS NULL
                """,
                (requester_username, json.dumps(payload, sort_keys=True), submission_id),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def get_by_channel_id(self, channel_id: int) -> Submission | None:
        """Find the record owned by a ticket or Forum post.

        A Docket Forum post can contain one or more imported Court Orders.  The
        Docket itself must win when its thread is queried; otherwise actions
        such as closing the Docket can accidentally operate on an imported
        Court Order.  The merge lookup remains as a fallback for Court Order
        commands intentionally run from the linked Docket.
        """
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT * FROM submissions
                WHERE discord_channel_id = ?
                ORDER BY submissions.id DESC LIMIT 1
                """,
                (channel_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                cursor = await database.execute(
                    """
                    SELECT submissions.* FROM submissions
                    INNER JOIN court_order_docket_merges
                        ON court_order_docket_merges.submission_id = submissions.id
                    WHERE court_order_docket_merges.docket_thread_id = ?
                    ORDER BY submissions.id DESC LIMIT 1
                    """,
                    (channel_id,),
                )
                row = await cursor.fetchone()
        return self._submission_from_row(row) if row else None

    async def find_open_submissions(self, workflow: str) -> list[Submission]:
        """Return active requests so the workflow can resolve a user-supplied case reference."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT * FROM submissions
                WHERE workflow = ? AND status != 'closed' AND discord_channel_id IS NOT NULL
                ORDER BY id DESC
                """,
                (workflow,),
            )
            rows = await cursor.fetchall()
        return [self._submission_from_row(row) for row in rows]

    async def mark_warrant_approved(
        self,
        *,
        submission_id: int,
        approver_user_id: int,
        approver_name: str,
        approved_at: str,
        approved_document_id: str,
    ) -> bool:
        """Persist one approval audit record and reject a duplicate approval."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE submissions
                SET status = 'approved', approved_by_user_id = ?, approved_by_name = ?,
                    approved_at = ?, approved_document_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status != 'approved'
                """,
                (approver_user_id, approver_name, approved_at, approved_document_id, submission_id),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def mark_warrant_denied(
        self,
        *,
        submission_id: int,
        denier_user_id: int,
        denier_name: str,
        denied_at: str,
        denial_note: str,
    ) -> bool:
        """Persist a judicial denial and retain the ticket for a corrected resubmission."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE submissions
                SET status = 'denied', denied_by_user_id = ?, denied_by_name = ?,
                    denied_at = ?, denial_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status NOT IN ('approved', 'denied', 'closed')
                """,
                (denier_user_id, denier_name, denied_at, denial_note, submission_id),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def get_resource_channel_id(self, resource_key: str) -> int | None:
        """Return a durable Discord resource created lazily by the bot."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                "SELECT discord_channel_id FROM bot_resources WHERE resource_key = ?", (resource_key,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_resource_channel_id(self, resource_key: str, channel_id: int) -> None:
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                INSERT INTO bot_resources (resource_key, discord_channel_id) VALUES (?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET discord_channel_id = excluded.discord_channel_id
                """,
                (resource_key, channel_id),
            )
            await database.commit()

    async def get_bot_setting(self, setting_key: str) -> str | None:
        """Return a durable, non-secret bot setting by key."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                "SELECT setting_value FROM bot_settings WHERE setting_key = ?", (setting_key,)
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_bot_setting(self, setting_key: str, setting_value: str) -> None:
        """Store a durable, non-secret bot setting by key."""
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
                """,
                (setting_key, setting_value),
            )
            await database.commit()

    async def get_bot_admin_user_ids(self) -> set[int]:
        """Return the direct bot-administrator grants stored by the server."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute("SELECT user_id FROM bot_administrators")
            rows = await cursor.fetchall()
        return {int(row[0]) for row in rows}

    async def list_bot_administrators(self) -> list[BotAdministrator]:
        """Return direct grants in their original assignment order for an admin-only review."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT user_id, user_name, added_by_user_id, added_by_name, added_at
                FROM bot_administrators
                ORDER BY added_at ASC, user_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [
            BotAdministrator(
                user_id=row["user_id"],
                user_name=row["user_name"],
                added_by_user_id=row["added_by_user_id"],
                added_by_name=row["added_by_name"],
                added_at=row["added_at"],
            )
            for row in rows
        ]

    async def add_bot_administrator(
        self,
        *,
        user_id: int,
        user_name: str,
        added_by_user_id: int,
        added_by_name: str,
        added_at: str,
    ) -> bool:
        """Grant direct bot administration once, retaining the assignment audit data."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO bot_administrators
                (user_id, user_name, added_by_user_id, added_by_name, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, user_name, added_by_user_id, added_by_name, added_at),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def remove_bot_administrator(self, user_id: int) -> bool:
        """Remove only a direct bot-administrator grant, never Discord role privileges."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                "DELETE FROM bot_administrators WHERE user_id = ?", (user_id,)
            )
            await database.commit()
        return cursor.rowcount == 1

    async def list_discord_role_mappings(self) -> list[DiscordRoleMapping]:
        """Return all Discord-managed function-role mappings in a stable order."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT function_key, role_id, added_by_user_id, added_by_name, added_at
                FROM discord_role_mappings
                ORDER BY function_key ASC, added_at ASC, role_id ASC
                """
            )
            rows = await cursor.fetchall()
        return [
            DiscordRoleMapping(
                function_key=row["function_key"],
                role_id=row["role_id"],
                added_by_user_id=row["added_by_user_id"],
                added_by_name=row["added_by_name"],
                added_at=row["added_at"],
            )
            for row in rows
        ]

    async def add_discord_role_mapping(
        self,
        *,
        function_key: str,
        role_id: int,
        added_by_user_id: int,
        added_by_name: str,
        added_at: str,
    ) -> bool:
        """Map one existing Discord role to a NERP function category once."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                INSERT OR IGNORE INTO discord_role_mappings
                (function_key, role_id, added_by_user_id, added_by_name, added_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (function_key, role_id, added_by_user_id, added_by_name, added_at),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def remove_discord_role_mapping(self, *, function_key: str, role_id: int) -> bool:
        """Remove only a Discord-managed mapping, leaving config recovery roles intact."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                "DELETE FROM discord_role_mappings WHERE function_key = ? AND role_id = ?",
                (function_key, role_id),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def mark_closed(
        self,
        *,
        submission_id: int,
        closer_user_id: int,
        closer_name: str,
        closed_at: str,
        closed_note: str | None,
    ) -> bool:
        """Record a terminal closure and reject a duplicate close action."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE submissions
                SET status = 'closed', closed_by_user_id = ?, closed_by_name = ?,
                    closed_at = ?, closed_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status != 'closed'
                """,
                (closer_user_id, closer_name, closed_at, closed_note, submission_id),
            )
            await database.commit()
        return cursor.rowcount == 1

    async def get_case_assignments(self, submission_id: int) -> list[CaseAssignment]:
        """Return the current role assignments in a stable player-facing order."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT * FROM case_assignments WHERE submission_id = ?
                ORDER BY CASE assignment_type
                    WHEN 'prosecutor' THEN 1
                    WHEN 'judge' THEN 2
                    WHEN 'defense_attorney' THEN 3
                    ELSE 4
                END
                , assignment_slot
                """,
                (submission_id,),
            )
            rows = await cursor.fetchall()
        return [self._case_assignment_from_row(row) for row in rows]

    async def set_case_assignment(
        self,
        *,
        submission_id: int,
        assignment_type: str,
        assignment_slot: int,
        user_id: int,
        user_name: str,
        assigned_by_user_id: int,
        assigned_by_name: str,
        assigned_at: str,
    ) -> None:
        """Set or reassign a named case role while retaining the assigning staff member."""
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                INSERT INTO case_assignments
                (submission_id, assignment_type, assignment_slot, user_id, user_name, assigned_by_user_id,
                 assigned_by_name, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id, assignment_type, assignment_slot) DO UPDATE SET
                    user_id = excluded.user_id,
                    user_name = excluded.user_name,
                    assigned_by_user_id = excluded.assigned_by_user_id,
                    assigned_by_name = excluded.assigned_by_name,
                    assigned_at = excluded.assigned_at
                """,
                (
                    submission_id,
                    assignment_type,
                    assignment_slot,
                    user_id,
                    user_name,
                    assigned_by_user_id,
                    assigned_by_name,
                    assigned_at,
                ),
            )
            await database.commit()

    async def propose_assignment_transfer(
        self,
        *,
        submission_id: int,
        assignment_type: str,
        assignment_slot: int,
        from_user_id: int | None,
        from_user_name: str | None,
        to_user_id: int,
        to_user_name: str,
        proposed_by_user_id: int,
        proposed_by_name: str,
        proposed_at: str,
    ) -> AssignmentTransfer:
        """Create one acceptance-required transfer and supersede any older pending offer."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            await database.execute(
                """
                UPDATE assignment_transfers SET status = 'superseded'
                WHERE submission_id = ? AND assignment_type = ? AND assignment_slot = ? AND status = 'pending'
                """,
                (submission_id, assignment_type, assignment_slot),
            )
            cursor = await database.execute(
                """
                INSERT INTO assignment_transfers
                (submission_id, assignment_type, assignment_slot, from_user_id, from_user_name, to_user_id,
                 to_user_name, proposed_by_user_id, proposed_by_name, proposed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    submission_id,
                    assignment_type,
                    assignment_slot,
                    from_user_id,
                    from_user_name,
                    to_user_id,
                    to_user_name,
                    proposed_by_user_id,
                    proposed_by_name,
                    proposed_at,
                ),
            )
            await database.commit()
            cursor = await database.execute(
                "SELECT * FROM assignment_transfers WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
        assert row is not None
        return self._assignment_transfer_from_row(row)

    async def get_pending_assignment_transfer(
        self, *, submission_id: int, assignment_type: str, assignment_slot: int, recipient_user_id: int
    ) -> AssignmentTransfer | None:
        """Return the one transfer that the current user is permitted to accept."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                """
                SELECT * FROM assignment_transfers
                WHERE submission_id = ? AND assignment_type = ? AND assignment_slot = ?
                      AND to_user_id = ? AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                (submission_id, assignment_type, assignment_slot, recipient_user_id),
            )
            row = await cursor.fetchone()
        return self._assignment_transfer_from_row(row) if row else None

    async def get_assignment_transfers(self, submission_id: int) -> list[AssignmentTransfer]:
        """Return the full handoff audit trail for a permanent case record."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                "SELECT * FROM assignment_transfers WHERE submission_id = ? ORDER BY id", (submission_id,)
            )
            rows = await cursor.fetchall()
        return [self._assignment_transfer_from_row(row) for row in rows]

    async def accept_assignment_transfer(
        self, transfer: AssignmentTransfer, *, accepted_at: str) -> bool:
        """Atomically accept an offer and make the recipient the current assignee."""
        async with aiosqlite.connect(self.path) as database:
            cursor = await database.execute(
                """
                UPDATE assignment_transfers SET status = 'accepted', accepted_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (accepted_at, transfer.id),
            )
            if cursor.rowcount != 1:
                await database.commit()
                return False
            await database.execute(
                """
                INSERT INTO case_assignments
                (submission_id, assignment_type, assignment_slot, user_id, user_name, assigned_by_user_id,
                 assigned_by_name, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id, assignment_type, assignment_slot) DO UPDATE SET
                    user_id = excluded.user_id,
                    user_name = excluded.user_name,
                    assigned_by_user_id = excluded.assigned_by_user_id,
                    assigned_by_name = excluded.assigned_by_name,
                    assigned_at = excluded.assigned_at
                """,
                (
                    transfer.submission_id,
                    transfer.assignment_type,
                    transfer.assignment_slot,
                    transfer.to_user_id,
                    transfer.to_user_name,
                    transfer.proposed_by_user_id,
                    transfer.proposed_by_name,
                    accepted_at,
                ),
            )
            await database.commit()
        return True

    async def get_court_order_docket_merge(
        self, submission_id: int
    ) -> CourtOrderDocketMerge | None:
        """Return the durable Docket target and last forwarded source message."""
        async with aiosqlite.connect(self.path) as database:
            database.row_factory = aiosqlite.Row
            cursor = await database.execute(
                "SELECT * FROM court_order_docket_merges WHERE submission_id = ?", (submission_id,)
            )
            row = await cursor.fetchone()
        return self._court_order_docket_merge_from_row(row) if row else None

    async def record_court_order_docket_merge(
        self,
        *,
        submission_id: int,
        docket_thread_id: int,
        merged_by_user_id: int,
        merged_by_name: str,
        merged_at: str,
        last_forwarded_message_id: int | None,
        source_closed: bool,
    ) -> None:
        """Record a one-way Court Order copy into a Docket and its sync checkpoint."""
        async with aiosqlite.connect(self.path) as database:
            await database.execute(
                """
                INSERT INTO court_order_docket_merges
                (submission_id, docket_thread_id, merged_by_user_id, merged_by_name, merged_at,
                 last_forwarded_message_id, source_closed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    docket_thread_id = excluded.docket_thread_id,
                    merged_by_user_id = excluded.merged_by_user_id,
                    merged_by_name = excluded.merged_by_name,
                    merged_at = excluded.merged_at,
                    last_forwarded_message_id = excluded.last_forwarded_message_id,
                    source_closed = excluded.source_closed
                """,
                (
                    submission_id,
                    docket_thread_id,
                    merged_by_user_id,
                    merged_by_name,
                    merged_at,
                    last_forwarded_message_id,
                    int(source_closed),
                ),
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
            claimed_user_id=row["claimed_user_id"],
            approved_by_user_id=row["approved_by_user_id"],
            approved_by_name=row["approved_by_name"],
            approved_at=row["approved_at"],
            approved_document_id=row["approved_document_id"],
            denied_by_user_id=row["denied_by_user_id"],
            denied_by_name=row["denied_by_name"],
            denied_at=row["denied_at"],
            denial_note=row["denial_note"],
            closed_by_user_id=row["closed_by_user_id"],
            closed_by_name=row["closed_by_name"],
            closed_at=row["closed_at"],
            closed_note=row["closed_note"],
        )

    @staticmethod
    def _case_assignment_from_row(row: aiosqlite.Row) -> CaseAssignment:
        return CaseAssignment(
            submission_id=row["submission_id"],
            assignment_type=row["assignment_type"],
            assignment_slot=row["assignment_slot"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            assigned_by_user_id=row["assigned_by_user_id"],
            assigned_by_name=row["assigned_by_name"],
            assigned_at=row["assigned_at"],
        )

    @staticmethod
    def _assignment_transfer_from_row(row: aiosqlite.Row) -> AssignmentTransfer:
        return AssignmentTransfer(
            id=row["id"],
            submission_id=row["submission_id"],
            assignment_type=row["assignment_type"],
            assignment_slot=row["assignment_slot"],
            from_user_id=row["from_user_id"],
            from_user_name=row["from_user_name"],
            to_user_id=row["to_user_id"],
            to_user_name=row["to_user_name"],
            proposed_by_user_id=row["proposed_by_user_id"],
            proposed_by_name=row["proposed_by_name"],
            proposed_at=row["proposed_at"],
            status=row["status"],
            accepted_at=row["accepted_at"],
        )

    @staticmethod
    def _court_order_docket_merge_from_row(row: aiosqlite.Row) -> CourtOrderDocketMerge:
        return CourtOrderDocketMerge(
            submission_id=row["submission_id"],
            docket_thread_id=row["docket_thread_id"],
            merged_by_user_id=row["merged_by_user_id"],
            merged_by_name=row["merged_by_name"],
            merged_at=row["merged_at"],
            last_forwarded_message_id=row["last_forwarded_message_id"],
            source_closed=bool(row["source_closed"]),
        )
