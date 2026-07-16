"""Minimal Google Sheets reader with no write scopes or write methods."""

from __future__ import annotations

from typing import Sequence


READ_ONLY_SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)


class GoogleSheetsSource:
    def __init__(self, credentials_file: str):
        from google.oauth2.service_account import Credentials
        import gspread

        credentials = Credentials.from_service_account_file(
            credentials_file, scopes=READ_ONLY_SCOPES
        )
        self._client = gspread.authorize(credentials)

    def read_values(
        self, spreadsheet_id: str, sheet_name: str
    ) -> Sequence[Sequence[object]]:
        spreadsheet = self._client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        return worksheet.get_all_values()
