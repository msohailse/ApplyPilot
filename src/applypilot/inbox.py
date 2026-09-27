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
import re
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


_BOARD_NAMES = {"linkedin", "indeed", "glassdoor", "google", "workopolis"}

# Words that carry no employer signal in a job title.
_TITLE_STOP = {
    "the", "and", "or", "for", "with", "of", "a", "an", "to", "in", "on", "at", "by",
    "de", "la", "le", "les", "des", "du", "une", "un", "et", "en", "sur", "pour",
    "senior", "junior", "mid", "lead", "staff", "principal", "head", "chief",
    "developer", "developeur", "developpeur", "developpeuse", "engineer", "engineering",
    "development", "full", "stack", "part", "time", "remote", "hybrid", "onsite",
    "contract", "permanent", "freelance", "intern", "internship", "trainee",
    "job", "role", "position", "opportunity", "needed", "wanted", "urgent", "all",
    "h", "f", "m", "w", "d", "genders", "candidate", "consultant",
}


def _title_terms(title: str) -> list[str]:
    """Distinctive, mostly tech-looking tokens from a job title.

    Prefers acronyms / mixed-case / words with digits (Node, AWS, JS, C++, 3D)
    over generic role words, and drops accented/filler tokens.
    """
    import unicodedata

    def _deaccent(s: str) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
        )

    scored: list[tuple[int, str]] = []
    for w in re.findall(r"[^\W_]+", title or ""):
        key = _deaccent(w).lower()
        if len(key) < 2 or key in _TITLE_STOP or key.isdigit():
            continue
        score = 0
        if any(ch.isupper() for ch in w[1:]):
            score += 2
        if any(ch.isdigit() or ch in "#+" for ch in w):
            score += 1
        scored.append((score, key))

    seen: set[str] = set()
    out: list[str] = []
    for _score, key in sorted(scored, key=lambda x: -x[0]):
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out[:4]


def _title_phrase(title: str) -> str:
    """A cleaned, quotable version of the title for Gmail phrase search."""
    import unicodedata

    s = "".join(
        c
        for c in unicodedata.normalize("NFKD", title or "")
        if not unicodedata.combining(c)
    )
    s = re.sub(r"[^\w\s+#]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) >= 3 else ""


def _build_query(job: dict, label: str) -> str:
    """Compose a Gmail search query for this job's company/role."""
    name = (job.get("company") or "").strip()
    domain = _employer_domain(job)
    keywords = (
        "(applied OR application OR interview OR recruiter OR assessment OR "
        "offer OR schedule OR scheduling OR \"next steps\" OR position OR candidacy)"
    )
    # Drop marketing/forum noise (bank promos match "application"), but KEEP
    # category:social -- LinkedIn/Indeed application confirmations live there.
    noise = "-category:promotions -category:forums -in:chats"
    parts = []
    if label:
        parts.append(f'label:"{label}"')
    applied = (job.get("applied_at") or "")[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", applied):
        # Only look at mail that arrived after the application was sent.
        parts.append(f"after:{applied.replace('-', '/')}")
    if domain:
        parts.append(f"from:({domain})")
    elif name and name.lower() not in _BOARD_NAMES:
        parts.append(f'"{name}"')
    else:
        # No real employer on file (e.g. applied via LinkedIn/Indeed): fall back
        # to the distinctive words of the job title.
        terms = _title_terms(job.get("title") or "")
        if terms:
            parts.append("(" + " OR ".join(terms) + ")")
        else:
            phrase = _title_phrase(job.get("title") or "")
            if phrase:
                parts.append(f'"{phrase}"')
            elif name:
                parts.append(f'"{name}"')
    parts.append(keywords)
    parts.append(noise)
    return " ".join(parts)


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _html_to_text(html: str) -> str:
    """Convert an HTML email body to clean readable text."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "img", "head"]):
            tag.decompose()
        # Keep link targets visible next to their text.
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if href.startswith("http") and href not in (a.get_text() or ""):
                a.append(f" ({href})")
        text = soup.get_text("\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:  # noqa: BLE001 - fall back to crude stripping
        return re.sub(r"<[^>]+>", " ", html or "").strip()


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
            raw = _decode(data)
            return _html_to_text(raw) if mime == "text/html" else raw
        return ""

    return walk(payload or {})


def _extract_html(payload: dict) -> str:
    def walk(part: dict) -> str:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/html":
            return _decode(data)
        for child in part.get("parts") or []:
            html = walk(child)
            if html:
                return html
        return ""

    return walk(payload or {})


# Links that are never useful in a job thread (tracking / social boilerplate).
_LINK_NOISE = (
    "unsubscribe", "twitter.com", "x.com/", "facebook.com", "instagram.com",
    "linkedin.com/comm", "google.com/maps", "googleusercontent", "list-manage",
    "sendgrid", "mailchimp", "pixel", "click.pstmrk", "/track",
)


def _extract_links(payload: dict) -> list[str]:
    """Pull real URLs out of an email (HTML hrefs first, then plain text)."""
    links: list[str] = []
    for u in re.findall(r'href="(https?://[^"]+)"', _extract_html(payload) or ""):
        links.append(u)
    for u in re.findall(r"https?://[^\s<>\"')]+", _extract_body(payload) or ""):
        links.append(u)
    out: list[str] = []
    for u in links:
        u = u.rstrip(").,;:'\"")
        low = u.lower()
        if any(n in low for n in _LINK_NOISE):
            continue
        if u not in out:
            out.append(u)
    return out[:6]


def _msg_to_dict(msg: dict) -> dict:
    """Normalise a Gmail message (headers + body + sent/received)."""
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (msg.get("payload", {}).get("headers") or [])
    }
    labels = msg.get("labelIds", []) or []
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body": _extract_body(msg.get("payload", {}))[:4000],
        "links": _extract_links(msg.get("payload", {})),
        "direction": "sent" if "SENT" in labels else "received",
    }


def fetch_company_messages(job: dict, max_results: int = 8) -> list[dict]:
    """Fetch the full conversation(s) for this job's company/role.

    Finds matching messages, then expands each thread so the whole exchange is
    included -- both what the employer sent and what the candidate sent.
    """
    service = _service()
    label = get_gmail_config().get("label", "")
    query = _build_query(job, label)
    log.info("Gmail search: %s", query)

    listing = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    thread_ids: list[str] = []
    for meta in listing.get("messages", []) or []:
        tid = meta.get("threadId")
        if tid and tid not in thread_ids:
            thread_ids.append(tid)

    messages: list[dict] = []
    seen: set[str] = set()
    for tid in thread_ids[:6]:
        try:
            thread = service.users().threads().get(
                userId="me", id=tid, format="full"
            ).execute()
        except Exception:  # noqa: BLE001 - skip unreadable thread
            continue
        for msg in thread.get("messages", []) or []:
            mid = msg.get("id", "")
            if mid and mid not in seen:
                seen.add(mid)
                messages.append(_msg_to_dict(msg))
    return messages[:20]


def _thread_text(messages: list[dict]) -> str:
    """Render the conversation as a readable, cached transcript."""
    parts = []
    for m in messages:
        who = (
            "You (candidate)"
            if m.get("direction") == "sent"
            else (m.get("from") or "Them")
        )
        block = (
            f"[{m.get('date')}] {who}\nSubject: {m.get('subject')}\n"
            f"{m.get('body') or m.get('snippet')}"
        )
        if m.get("links"):
            block += "\nLinks:\n" + "\n".join(m["links"])
        parts.append(block)
    return "\n\n---\n\n".join(parts)[:12000]


# ── LLM action-item extraction ───────────────────────────────────────────

_ACTION_PROMPT = """You read a candidate's email thread about a job application \
and extract only what is actionable. Never invent facts.

If the emails are not genuinely about THIS job application (for example bank, \
retail or travel promotions, newsletters, or unrelated notifications), reply \
with exactly the single word: NONE

Output a short plain-text brief using ONLY these labels (omit empty ones):
STATUS: one line (applied, screening, interview scheduled, assessment, rejection, offer, waiting)
NEXT STEP: the single next action for the candidate, including any date/time/deadline
ACTION ITEMS: short bullets of what the employer or candidate must do
KEY DETAILS: names, interview format/platform, documents requested, salary if stated
LINK: any URL from the emails that the candidate should act on (application/status page, scheduling link, document to upload). Copy the full URL exactly. Omit if none.
SUGGESTION: one concrete, truthful suggestion for the candidate's next move

The CANDIDATE owns this mailbox. Keep it tight and factual."""


_SKILLS_PROMPT = """From a job posting, list the skills in exactly two lines:
TOP SKILLS: comma-separated must-have skills/technologies (max 12)
NICE TO HAVE: comma-separated nice-to-have/bonus skills (max 8), or NONE
Use ONLY the posting. No prose, no explanation."""


def extract_job_skills(job: dict) -> str:
    """Two-line TOP SKILLS / NICE TO HAVE block derived from the job posting."""
    desc = (job.get("full_description") or "").strip()
    if not desc:
        return ""
    client = get_client("inbox")
    out = client.chat(
        [
            {"role": "system", "content": _SKILLS_PROMPT},
            {"role": "user", "content": f"TITLE: {job.get('title')}\n\n{desc[:5000]}"},
        ],
        max_tokens=300,
        temperature=0.2,
    )
    return (out or "").strip()


def summarize_thread(job: dict, messages: list[dict]) -> str:
    """Ask the LLM for the status + action items from the fetched messages."""
    if not messages:
        return "No matching emails found for this company."
    thread_text = _thread_text(messages)
    job_text = (
        f"TITLE: {job.get('title')}\n"
        f"COMPANY: {job.get('company') or job.get('site')}\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:4000]}\n"
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


# ── Categorisation ───────────────────────────────────────────────────────

# Tab groups, in display order: key -> (label, job status to auto-assign or None)
CATEGORY_META: dict[str, tuple[str, str | None]] = {
    "offer": ("Offers", "offer"),
    "interview": ("Interviewing", "interviewing"),
    "assessment": ("Assessments / tests", None),
    "screening": ("Screening", None),
    "waiting": ("In progress (waiting)", None),
    "rejected": ("Rejected", "rejected"),
    "other": ("Other", None),
    "none": ("No reply yet", None),
    "unscanned": ("Not scanned yet", None),
}
CATEGORY_ORDER = list(CATEGORY_META)


def _category_from_summary(summary: str) -> str:
    """Map an email-thread summary to a coarse category (theme of the mail)."""
    text = (summary or "").lower()
    if "no matching emails" in text:
        return "none"
    # If the model says the mail is unrelated, treat it as "no reply yet"
    # rather than trusting a bogus STATUS line.
    if any(
        p in text
        for p in ("unrelated", "not related", "irrelevant", "not relevant",
                  "no relevant", "not job-related", "no job-related")
    ):
        return "none"
    # The summarizer emits a "STATUS: ..." line; trust that first.
    m = re.search(r"status:\s*(.+)", summary or "", re.IGNORECASE)
    status = (m.group(1) if m else text).lower()
    if "offer" in status:
        return "offer"
    if "reject" in status or "not moving forward" in status or "not selected" in status:
        return "rejected"
    if "interview" in status:
        return "interview"
    if any(w in status for w in ("assessment", "test task", "coding challenge", "take-home", "hackerrank")):
        return "assessment"
    if "screen" in status or "recruiter" in status:
        return "screening"
    if "waiting" in status or "applied" in status or "under review" in status:
        return "waiting"
    return "other"


# ── Public API ───────────────────────────────────────────────────────────

def scan_and_cache(job: dict, max_results: int = 8) -> dict:
    """Fetch the company thread, summarize it, categorize, and cache it."""
    messages = fetch_company_messages(job, max_results=max_results)
    summary = summarize_thread(job, messages)
    category = _category_from_summary(summary)
    if not messages or (summary or "").strip().upper().startswith("NONE"):
        category = "none"
    if category == "none":
        # Don't cache misleading promo text; keep it clean and honest.
        summary = "No job-related emails found for this role."
        thread_text = ""
    else:
        thread_text = _thread_text(messages)

    # Skills come from the job description, independent of the email.
    try:
        skills = extract_job_skills(job)
        if skills:
            summary = f"{summary}\n\n{skills}"
    except Exception:  # noqa: BLE001 - skills are a bonus
        log.debug("Skill extraction failed for %s", job.get("url"))

    message_ids = ",".join(m["id"] for m in messages)
    save_inbox_cache(
        job["url"], message_ids, thread_text, summary,
        resolve_model("inbox") or "", category,
    )

    # Auto-assign the application status from the email theme.
    status = CATEGORY_META.get(category, (None, None))[1]
    if status and job.get("url"):
        try:
            from applypilot.database import set_job_status

            set_job_status(job["url"], status)
        except Exception:  # noqa: BLE001 - never fail a scan over this
            log.debug("Could not auto-assign status %s for %s", status, job.get("url"))

    return {"messages": len(messages), "summary": summary, "category": category}


def scan_applied_jobs(limit: int = 20, max_results: int = 8) -> dict:
    """Scan the most recent applied jobs' inboxes and categorize them."""
    from applypilot.database import get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT url, title, site, company, location, application_url, applied_at, "
        "apply_status, full_description FROM jobs WHERE apply_status IN "
        "('applied','success','interviewing','offer','rejected','no_deal') "
        "ORDER BY COALESCE(applied_at, '') DESC LIMIT ?",
        (int(limit),),
    ).fetchall()

    scanned = 0
    errors = 0
    for row in rows:
        try:
            scan_and_cache(dict(row), max_results=max_results)
            scanned += 1
        except GmailNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - keep going
            errors += 1
            log.warning("Inbox scan failed for %s: %s", row["url"], exc)
    return {"scanned": scanned, "errors": errors, "total": len(rows)}


def cached_summary(job_url: str) -> dict | None:
    """Return the cached insight for a job, or None."""
    return get_inbox_cache(job_url)


def free(job_url: str) -> None:
    """Clear a job's cached inbox insight so it can be regenerated."""
    delete_inbox_cache(job_url)
