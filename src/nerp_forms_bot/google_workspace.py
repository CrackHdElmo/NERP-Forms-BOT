"""Small, synchronous Google Workspace client wrapped by Discord commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

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
