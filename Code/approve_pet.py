import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GSHEET_ID               = os.environ["GSHEET_ID"]
APPROVED_URL            = os.environ["APPROVED_URL"]
GSHEET_TAB              = "Pets"

creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON),
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheets_service = build("sheets", "v4", credentials=creds)

# Read all rows
result = sheets_service.spreadsheets().values().get(
    spreadsheetId=GSHEET_ID,
    range=f"{GSHEET_TAB}!A:K"
).execute()
rows = result.get("values", [])

# Find pending rows and update status
updates = []
for i, row in enumerate(rows[1:], start=2):  # skip header, 1-indexed
    if len(row) < 11:
        continue
    url    = row[0]
    status = row[10]
    if status == "pending":
        new_status = "approved" if url == APPROVED_URL else "rejected"
        updates.append({
            "range": f"{GSHEET_TAB}!K{i}",
            "values": [[new_status]]
        })
        print(f"{new_status}: {url}")

if updates:
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=GSHEET_ID,
        body={"valueInputOption": "RAW", "data": updates}
    ).execute()
    print(f"Updated {len(updates)} rows")
else:
    print("No pending rows found")
