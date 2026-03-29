#!/usr/bin/env python3
"""
Approve a restaurant -- updates selected row to approved, others to rejected.
"""

import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GSHEET_ID               = os.environ["GSHEET_ID"]
APPROVED_PLACE_ID       = os.environ["APPROVED_PLACE_ID"]
GSHEET_TAB              = "restaurants"

creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON),
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=creds)

# Read all rows
result = sheets_service.spreadsheets().values().get(
    spreadsheetId=GSHEET_ID,
    range=f"{GSHEET_TAB}!A:X"
).execute()
rows = result.get("values", [])

if len(rows) < 2:
    print("No data rows found. Exiting.")
    exit(0)

headers = rows[0]
status_col   = headers.index("status")   if "status"   in headers else 14
place_id_col = headers.index("place_id") if "place_id" in headers else 0

updates = []
approved_row = None

for i, row in enumerate(rows[1:], start=2):
    if len(row) <= max(status_col, place_id_col):
        continue
    if row[status_col] != "pending":
        continue

    place_id   = row[place_id_col]
    new_status = "approved" if place_id == APPROVED_PLACE_ID else "rejected"

    updates.append({
        "range":  f"{GSHEET_TAB}!{chr(65 + status_col)}{i}",
        "values": [[new_status]]
    })
    print(f"{new_status}: {row[1] if len(row) > 1 else place_id}")

    if new_status == "approved":
        approved_row = row

if updates:
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=GSHEET_ID,
        body={"valueInputOption": "RAW", "data": updates}
    ).execute()
    print(f"Updated {len(updates)} rows")

if approved_row:
    print(f"\nApproved: {approved_row[1] if len(approved_row) > 1 else 'Unknown'}")
else:
    print("No matching pending restaurant found for that place_id.")
