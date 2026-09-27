"""Gmail inbox insights: read-only, per-job, cached.

Pulls the email thread(s) for a job's company using the Gmail API with the
**read-only** scope, asks the LLM to extract status + action items, and caches
the result per job until the user clears it ("Free").

Security:
- Scope is ``gmail.readonly`` -- the token can never send, delete, or modify mail.
- The OAuth client JSON and refresh token stay local; nothing sensitive is
  committed. Revoke access anytime from the Google account.
- This module never writes to the mailbox.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from urllib.parse import urlparse

from applypilot.config import get_gmail_config, load_env
from applypilot.database import (
    delete_inbox_cache,
    get_inbox_cache,
    save_inbox_cache,
)
from applypilot.llm import get_client, resolve_model

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Hosts that are job boards / ATS, not the employer's own domain.
_ATS_HOSTS = (
    "greenhouse", "lever", "workday", "myworkday", "linkedin", "indeed",
    "ashby", "smartrecruiters", "jobvite", "icims", "taleo", "bamboohr",
    "workable", "recruitee", "teamtailor", "jobbnorge", "nav.no", "finn.no",
)


class GmailNotConfigured(RuntimeError):
    """Raised when Gmail credentials/token are missing or invalid."""


# ── OAuth / service ──────────────────────────────────────────────────────

def gmail_configured() -> bool:
    """True if a token or OAuth client file exists on disk."""
    cfg = get_gmail_config()
    return Path(cfg["token"]).exists() or Path(cfg["credentials"]).exists()


def run_oauth_flow() -> Path:
    """Run the one-time installed-app OAuth flow and persist a refresh token."""
    load_env()
    cfg = get_gmail_config()
    creds_path = Path(cfg["credentials"])
    token_path = Path(cfg["token"])
    if not creds_path.exists():
        raise GmailNotConfigured(
            f"Gmail OAuth client not found at {creds_path}. Download a Desktop OAuth "
            "client JSON from Google Cloud Console and set GMAIL_CREDENTIALS_PATH in "
            "~/.applypilot/.env."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass
    log.info("Gmail token saved to %s", token_path)
    return token_path


def _credentials():
    load_env()
    cfg = get_gmail_config()
    token_path = Path(cfg["token"])
    if not token_path.exists():
        raise GmailNotConfigured("Gmail not authorized yet. Run `applypilot gmail-auth` once.")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        raise GmailNotConfigured(f"Could not read Gmail token: {exc}") from exc

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise GmailNotConfigured("Gmail token is invalid; re-run `applypilot gmail-auth`.")
    return creds


def _service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


# ── Search + fetch ───────────────────────────────────────────────────────

def _employer_domain(job: dict) -> str:
    """Best-effort employer domain from the job's apply/listing URLs."""
    for key in ("application_url", "url"):
        raw = job.get(key)
        if not raw:
            continue
        host = (urlparse(raw).netloc or "").lower().split(":")[0]
        host = host.removeprefix("www.")
        if host and not any(a in host for a in _ATS_HOSTS):
            return host
    return ""


def _build_query(job: dict, label: str) -> str:
    """Compose a Gmail search query for this job's company/role."""
    name = (job.get("company") or job.get("site") or "").strip()
    domain = _employer_domain(job)
    keywords = (
        "(applied OR application OR interview OR recruiter OR assessment OR "
        "offer OR schedule OR scheduling OR \"next steps\" OR position OR candidacy)"
    )
    parts = []
    if label:
        parts.append(f'label:"{label}"')
    if name:
        parts.append(f'"{name}"')
    elif domain:
        parts.append(f"from:({domain})")
    parts.append(keywords)
    return " ".join(parts)


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _extract_body(payload: dict) -> str:
    def walk(part: dict) -> str:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            return _decode(data)
        for child in part.get("parts") or []:
            text = walk(child)
            if text:
                return text
        if data:
            return _decode(data)
        return ""

    return walk(payload or {})


def fetch_company_messages(job: dict, max_results: int = 8) -> list[dict]:
    """Fetch up to ``max_results`` messages matching this job's company/role."""
    service = _service()
    label = get_gmail_config().get("label", "")
    query = _build_query(job, label)
    log.info("Gmail search: %s", query)

    listing = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages: list[dict] = []
    for meta in listing.get("messages", []) or []:
        msg = service.users().messages().get(
            userId="me", id=meta["id"], format="full"
        ).execute()
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (msg.get("payload", {}).get("headers") or [])
        }
        messages.append({
            "id": meta["id"],
            "thread_id": msg.get("threadId", ""),
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", ""),
            "body": _extract_body(msg.get("payload", {}))[:4000],
        })
    return messages


# ── LLM action-item extraction ───────────────────────────────────────────

_ACTION_PROMPT = """You read a candidate's email thread about a job application \
and extract only what is actionable. Never invent facts. If the emails are \
irrelevant to this company/role, say so plainly.

Output a short plain-text brief using ONLY these labels (omit empty ones):
STATUS: one line (applied, screening, interview scheduled, assessment, rejection, offer, waiting)
NEXT STEP: the single next action for the candidate, including any date/time/deadline
ACTION ITEMS: short bullets of what the employer or candidate must do
KEY DETAILS: names, interview format/platform, documents requested, links, salary if stated
SUGGESTION: one concrete, truthful suggestion for the candidate's next move

The CANDIDATE owns this mailbox. Keep it tight and factual."""


def summarize_thread(job: dict, messages: list[dict]) -> str:
    """Ask the LLM for the status + action items from the fetched messages."""
    if not messages:
        return "No matching emails found for this company."
    thread_text = "\n\n".join(
        f"[{m['date']}] From: {m['from']}\nSubject: {m['subject']}\n{m['body'] or m['snippet']}"
        for m in messages
    )[:12000]
    job_text = (
        f"TITLE: {job.get('title')}\n"
        f"COMPANY: {job.get('company') or job.get('site')}\n"
    )
    client = get_client("inbox")
    return client.chat(
        [
            {"role": "system", "content": _ACTION_PROMPT},
            {
                "role": "user",
                "content": f"COMPANY/ROLE:\n{job_text}\n\nEMAIL THREAD:\n{thread_text}",
            },
        ],
        max_tokens=800,
        temperature=0.2,
    )


# ── Public API ───────────────────────────────────────────────────────────

def scan_and_cache(job: dict, max_results: int = 8) -> dict:
    """Fetch the company thread, summarize it, and cache the result on the job."""
    messages = fetch_company_messages(job, max_results=max_results)
    summary = summarize_thread(job, messages)
    message_ids = ",".join(m["id"] for m in messages)
    save_inbox_cache(job["url"], message_ids, "", summary, resolve_model("inbox") or "")
    return {"messages": len(messages), "summary": summary}


def cached_summary(job_url: str) -> dict | None:
    """Return the cached insight for a job, or None."""
    return get_inbox_cache(job_url)


def free(job_url: str) -> None:
    """Clear a job's cached inbox insight so it can be regenerated."""
    delete_inbox_cache(job_url)
