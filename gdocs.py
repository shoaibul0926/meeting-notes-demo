"""Pushes generated meeting notes into a new Google Doc (OAuth desktop-app flow)."""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents",
          "https://www.googleapis.com/auth/drive.file"]
ROOT = Path(__file__).parent
CLIENT_SECRET = ROOT / "client_secret.json"
TOKEN = ROOT / "token.json"


def available():
    return CLIENT_SECRET.exists()


def get_credentials():
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return creds


def _plan(markdown_text):
    """Turns our notes.md markdown (## headings, - bullets, plain lines) into Docs API
    insert text plus the paragraph-style/bullet ranges to apply afterward."""
    full_text, headings, bullets = "", [], []
    pos = 1  # Docs body content starts at index 1
    for line in markdown_text.splitlines():
        if line.startswith("## "):
            text = line[3:] + "\n"
            headings.append((pos, pos + len(text) - 1, "HEADING_2"))
        elif line.startswith("# "):
            text = line[2:] + "\n"
            headings.append((pos, pos + len(text) - 1, "HEADING_1"))
        elif line.startswith("- "):
            text = line[2:] + "\n"
            bullets.append((pos, pos + len(text) - 1))
        else:
            text = line + "\n"
        full_text += text
        pos += len(text)
    return full_text, headings, bullets


def push_notes(title, markdown_text):
    """Creates a new Google Doc with the given title/content and returns its URL."""
    creds = get_credentials()
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    full_text, headings, bullets = _plan(markdown_text)
    requests = [{"insertText": {"location": {"index": 1}, "text": full_text}}]
    for start, end, style in headings:
        requests.append({"updateParagraphStyle": {
            "range": {"startIndex": start, "endIndex": end},
            "paragraphStyle": {"namedStyleType": style},
            "fields": "namedStyleType"}})
    for start, end in bullets:
        requests.append({"createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": end},
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    if len(requests) > 1:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"
