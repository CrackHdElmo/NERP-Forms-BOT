"""Small, synchronous Google Workspace client wrapped by Discord commands."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pypdf import PdfReader

TRACKER_TITLE = "NERP Forms BOT - Test Requests"
SHEET_HEADERS = [
    "Request ID",
    "Submission ID",
    "Request Type",
    "Status",
    "Submitted At (UTC)",
    "Requester Name",
    "Requester Discord User ID",
    "Requester Email",
    "Existing Docket ID",
    "Discord Destination",
    "Discord Channel or Thread ID",
    "Discord Link",
    "Drive Request Folder",
    "Draft Document",
    "Draft PDF",
    "Final Document",
    "Final PDF",
    "Assigned To",
    "Decision",
    "Decisioned By",
    "Decisioned At (UTC)",
    "Last Synced At (UTC)",
    "Retry Count",
    "Sync Error",
    "Internal Notes",
]


@dataclass(frozen=True)
class TrackingSheet:
    """The tracking Sheet Google resource returned to Discord."""

    id: str
    url: str
    created: bool


@dataclass(frozen=True)
class TrackingRow:
    spreadsheet_id: str
    row_number: int


@dataclass(frozen=True)
class GeneratedWarrant:
    """An internal Google Doc draft and its player-facing PDF rendering."""

    document_id: str
    document_url: str
    pdf_bytes: bytes
    page_count: int


class GoogleWorkspaceService:
    """Create or locate the one test tracking Sheet in the shared Drive folder."""

    def __init__(self, service_account_file: str, drive_folder_id: str) -> None:
        self.service_account_file = Path(service_account_file)
        self.drive_folder_id = drive_folder_id

    def ensure_tracking_sheet(self) -> TrackingSheet:
        """Return the shared tracker, creating it only once when it is absent."""
        if not self.service_account_file.is_file():
            raise FileNotFoundError(
                "The Google service-account file was not found at the configured server path."
            )

        credentials = service_account.Credentials.from_service_account_file(
            self.service_account_file,
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

        search = drive.files().list(
            q=(
                f"name = '{TRACKER_TITLE}' and '{self.drive_folder_id}' in parents "
                "and trashed = false and mimeType = 'application/vnd.google-apps.spreadsheet'"
            ),
            spaces="drive",
            fields="files(id, webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        matches = search.get("files", [])
        if matches:
            return TrackingSheet(
                id=matches[0]["id"],
                url=matches[0].get("webViewLink", f"https://docs.google.com/spreadsheets/d/{matches[0]['id']}"),
                created=False,
            )

        created = drive.files().create(
            body={
                "name": TRACKER_TITLE,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [self.drive_folder_id],
            },
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        spreadsheet_id = created["id"]
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": 0,
                                "title": "Requests",
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "title,gridProperties.frozenRowCount",
                        }
                    }
                ]
            },
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="Requests!A1:Y1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()
        return TrackingSheet(
            id=spreadsheet_id,
            url=created.get("webViewLink", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"),
            created=True,
        )

    def get_form_response_rows(self, spreadsheet_id: str, sheet_name: str) -> list[dict[str, str]]:
        """Return nonblank response rows as header-keyed dictionaries."""
        sheets = self._sheets_service()
        response = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{sheet_name}'!A:BB",
        ).execute()
        values = response.get("values", [])
        if not values:
            return []
        headers = [value.strip() for value in values[0]]
        rows: list[dict[str, str]] = []
        for index, values_row in enumerate(values[1:], start=2):
            if not any(values_row):
                continue
            row: dict[str, str] = {}
            occurrences: dict[str, int] = {}
            for column, header in enumerate(headers):
                if not header:
                    continue
                occurrences[header] = occurrences.get(header, 0) + 1
                key = header if occurrences[header] == 1 else f"{header} ({occurrences[header]})"
                row[key] = str(values_row[column]).strip() if column < len(values_row) else ""
            row["_source_row"] = str(index)
            rows.append(row)
        return rows

    def append_tracking_row(
        self,
        *,
        request_id: str,
        source_key: str,
        payload: dict[str, str],
        channel_id: int,
        channel_url: str,
    ) -> TrackingRow:
        """Append the initial operational record to the central tracker."""
        tracker = self.ensure_tracking_sheet()
        request_type = self._answer(payload, "Request Type")
        timestamp = self._answer(payload, "Timestamp")
        requester_name = self._answer(payload, "Requestors Name:")
        docket_id = self._answer(payload, "Please indicate the Docket Name/ID.")
        destination = "Private Off-Docket ticket"
        row = [
            request_id, source_key, request_type, "Awaiting Claim", timestamp, requester_name,
            "", self._answer(payload, "Discord Username", "Your Discord Name"), docket_id,
            destination, str(channel_id), channel_url, "", "", "", "", "", "", "Pending",
            "", "", "", "0", "", self._answer(payload, "Evidence Links"),
        ]
        response = self._sheets_service().spreadsheets().values().append(
            spreadsheetId=tracker.id,
            range="Requests!A:Y",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        updated_range = response["updates"]["updatedRange"]
        row_number = int(updated_range.rsplit("!", 1)[-1].split(":", 1)[0][1:])
        return TrackingRow(spreadsheet_id=tracker.id, row_number=row_number)

    def mark_tracking_row_claimed(self, tracking: TrackingRow, user_id: int) -> None:
        """Record the verified Discord identity and claim state in the tracker row."""
        self._sheets_service().spreadsheets().values().batchUpdate(
            spreadsheetId=tracking.spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"Requests!D{tracking.row_number}", "values": [["Awaiting Review"]]},
                    {"range": f"Requests!G{tracking.row_number}", "values": [[str(user_id)]]},
                ],
            },
        ).execute()

    def generate_arrest_warrant(
        self,
        *,
        template_document_id: str,
        output_drive_folder_id: str,
        request_id: str,
        replacements: dict[str, str],
    ) -> GeneratedWarrant:
        """Copy a native template, fill it, and measure the rendered PDF page count.

        The caller must treat any result other than exactly one page as an internal
        draft only.  The method deliberately does not upload or share the PDF.
        """
        drive = self._drive_service()
        docs = build("docs", "v1", credentials=self._credentials(), cache_discovery=False)
        copied = drive.files().copy(
            fileId=template_document_id,
            body={
                "name": f"Arrest Warrant {request_id}",
                "parents": [output_drive_folder_id],
            },
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        document_id = copied["id"]
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": placeholder, "matchCase": True},
                    "replaceText": value,
                }
            }
            for placeholder, value in replacements.items()
        ]
        docs.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

        stream = BytesIO()
        download = MediaIoBaseDownload(
            stream,
            drive.files().export(fileId=document_id, mimeType="application/pdf"),
        )
        finished = False
        while not finished:
            _, finished = download.next_chunk()
        pdf_bytes = stream.getvalue()
        return GeneratedWarrant(
            document_id=document_id,
            document_url=copied.get("webViewLink", f"https://docs.google.com/document/d/{document_id}/edit"),
            pdf_bytes=pdf_bytes,
            page_count=len(PdfReader(BytesIO(pdf_bytes)).pages),
        )

    def _sheets_service(self):
        credentials = self._credentials()
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def _drive_service(self):
        return build("drive", "v3", credentials=self._credentials(), cache_discovery=False)

    def _credentials(self):
        if not self.service_account_file.is_file():
            raise FileNotFoundError(
                "The Google service-account file was not found at the configured server path."
            )
        return service_account.Credentials.from_service_account_file(
            self.service_account_file,
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/documents",
            ],
        )

    @staticmethod
    def _answer(payload: dict[str, str], *names: str) -> str:
        for name in names:
            if payload.get(name):
                return payload[name]
        return ""
