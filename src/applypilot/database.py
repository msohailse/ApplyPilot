"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Gmail inbox insights cache (one row per job). Generated on demand and
    # kept until the user clears it ("Free") -- never regenerated automatically.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inbox_cache (
            url          TEXT PRIMARY KEY,
            message_ids  TEXT,
            thread_text  TEXT,
            summary      TEXT,
            category     TEXT,
            model        TEXT,
            created_at   TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    _ensure_inbox_columns(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Classification (derived from description)
    "language_requirement": "TEXT",
    "employment_type": "TEXT",
    "country": "TEXT",
    "work_mode": "TEXT",
    # User notes
    "notes": "TEXT",
    # "Role to be studied" note (marks a job as a learning/role-model target)
    "study_note": "TEXT",
    # Focus flag: user-marked jobs they're actively applying to (card is bolded)
    "focused": "INTEGER DEFAULT 0",
    # Highlight flag + LLM-extracted key concepts for the job
    "highlighted": "INTEGER DEFAULT 0",
    "highlight_concepts": "TEXT",
    # Set the moment the user touches a job (note/status/focus/study/docs) so it
    # is never silently deleted by a later discovery/enrichment run.
    "protected": "INTEGER DEFAULT 0",
    # Manually hidden from the main list. Kept forever (and in exports) for
    # success-rate calculations -- never deleted.
    "stale": "INTEGER DEFAULT 0",
    # Company name (employer), separate from `site` (job board / source)
    "company": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Combined (LaTeX) resume rendered from the base template
    "combined_tex_path": "TEXT",
    "combined_at": "TEXT",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


# Manual triage statuses settable from the dashboard.
# "outcome" = post-application stages (job was applied to, then progressed).
MANUAL_STATUSES = (
    "applied", "interviewing", "rejected", "no_deal", "offer",
    "not_available", "not_interested", "failed",
)
OUTCOME_STATUSES = ("interviewing", "rejected", "no_deal", "offer")


def set_job_status(url: str, status: str, reason: str | None = None) -> None:
    """Set a job's manual apply status.

    Args:
        url: Job URL to update.
        status: One of "applied", "not_available", "not_interested", "failed",
            or "reset" to clear the status back to not-applied.
        reason: Optional note stored in apply_error.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()

    if status == "applied":
        # Applying clears the Focus flag: it's no longer a job "to apply to".
        conn.execute(
            "UPDATE jobs SET apply_status = 'applied', applied_at = ?, "
            "apply_error = NULL, focused = 0, protected = 1 WHERE url = ?",
            (now, url),
        )
    elif status in OUTCOME_STATUSES:
        # Post-application outcome: keep applied_at (the job WAS applied to).
        conn.execute(
            "UPDATE jobs SET apply_status = ?, apply_error = ?, focused = 0, "
            "protected = 1 WHERE url = ?",
            (status, reason or status, url),
        )
    elif status in ("not_available", "not_interested", "failed"):
        conn.execute(
            "UPDATE jobs SET apply_status = ?, apply_error = ?, "
            "applied_at = NULL, protected = 1 WHERE url = ?",
            (status, reason or status, url),
        )
    else:  # "reset" (or any unknown) -> clear
        conn.execute(
            "UPDATE jobs SET apply_status = NULL, apply_error = NULL, "
            "applied_at = NULL, apply_attempts = 0, protected = 1 WHERE url = ?",
            (url,),
        )
    conn.commit()


def set_job_note(url: str, note: str) -> None:
    """Save a free-text note for a job (marks it protected)."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET notes = ?, protected = 1 WHERE url = ?", (note or "", url))
    conn.commit()


def set_job_study(url: str, note: str) -> None:
    """Mark/update a job as a 'role to study' with a note (blank clears it)."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET study_note = ?, protected = 1 WHERE url = ?", (note or "", url))
    conn.commit()


def set_job_focus(url: str, focused: bool) -> None:
    """Mark/unmark a job as focused (the user is actively applying to it)."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET focused = ?, protected = 1 WHERE url = ?",
        (1 if focused else 0, url),
    )
    conn.commit()


def set_job_highlight(url: str, highlighted: bool, concepts: list[str] | None = None) -> None:
    """Mark/unmark a job as highlighted, optionally storing its key concepts."""
    import json as _json

    conn = get_connection()
    if concepts is not None:
        conn.execute(
            "UPDATE jobs SET highlighted = ?, highlight_concepts = ?, protected = 1 "
            "WHERE url = ?",
            (1 if highlighted else 0, _json.dumps(concepts), url),
        )
    else:
        conn.execute(
            "UPDATE jobs SET highlighted = ?, protected = 1 WHERE url = ?",
            (1 if highlighted else 0, url),
        )
    conn.commit()


def set_job_stale(url: str, stale: bool = True) -> None:
    """Move a job to (or restore it from) the Stale section.

    Stale jobs are never deleted -- they stay in the data and in exports so
    success-rate stats remain accurate.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET stale = ?, protected = 1 WHERE url = ?",
        (1 if stale else 0, url),
    )
    conn.commit()


def mark_job_protected(url: str) -> None:
    """Flag a job as user-touched so a later run never silently deletes it."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET protected = 1 WHERE url = ?", (url,))
    conn.commit()


def job_has_user_data(url: str, conn=None) -> bool:
    """True if the job carries any user-created data (so it must not be deleted)."""
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT notes, study_note, apply_status, applied_at, apply_error, focused, "
        "tailored_resume_path, cover_letter_path, protected, highlighted, "
        "highlight_concepts "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if row is None:
        return True  # unknown row -> be safe and never delete
    if int(row["protected"] or 0):
        return True
    for key in ("notes", "study_note", "apply_status", "applied_at", "apply_error",
                "tailored_resume_path", "cover_letter_path", "highlight_concepts"):
        if (row[key] or "").strip():
            return True
    if int(row["focused"] or 0) or int(row["highlighted"] or 0):
        return True
    if conn.execute("SELECT 1 FROM inbox_cache WHERE url = ?", (url,)).fetchone():
        return True
    try:
        from applypilot.todos import load_todos

        if any((t.get("url") or "") == url for t in load_todos()):
            return True
    except Exception:  # pragma: no cover - defensive
        pass
    return False


def delete_job_if_unused(url: str, conn=None) -> bool:
    """Delete a job row only when it has no user data. Returns True if deleted.

    Used during URL canonicalization: a row with notes / applied status / docs /
    focused / study note / inbox cache / todo reference is kept (never dropped)
    even when its URL is superseded by another listing.
    """
    conn = conn or get_connection()
    if job_has_user_data(url, conn):
        return False
    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
    conn.commit()
    return True


def get_study_jobs(conn=None) -> list[dict]:
    """Return jobs marked as 'role to study' (non-empty study_note)."""
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT url, title, company, site, fit_score, study_note FROM jobs "
        "WHERE study_note IS NOT NULL AND trim(study_note) != '' "
        "ORDER BY fit_score DESC NULLS LAST, title"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Gmail inbox insights cache ───────────────────────────────────────────

def _ensure_inbox_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the ``inbox_cache`` table."""
    if conn is None:
        conn = get_connection()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(inbox_cache)").fetchall()}
    added = []
    if "category" not in existing:
        conn.execute("ALTER TABLE inbox_cache ADD COLUMN category TEXT")
        added.append("category")
    if added:
        conn.commit()
    return added


def get_inbox_cache(url: str, conn=None) -> dict | None:
    """Return the cached inbox insight for a job, or None."""
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM inbox_cache WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def get_inbox_cached_urls(conn=None) -> set[str]:
    """Return the set of job URLs that already have a cached inbox insight."""
    conn = conn or get_connection()
    return {r[0] for r in conn.execute("SELECT url FROM inbox_cache").fetchall()}


def get_inbox_summaries(conn=None) -> dict[str, str]:
    """Return {job_url: cached_summary} for all cached inbox insights."""
    conn = conn or get_connection()
    rows = conn.execute("SELECT url, summary FROM inbox_cache").fetchall()
    return {r[0]: (r[1] or "") for r in rows}


def save_inbox_cache(
    url: str, message_ids: str, thread_text: str, summary: str, model: str,
    category: str = "",
) -> None:
    """Insert or replace the cached inbox insight for a job."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO inbox_cache (url, message_ids, thread_text, summary, category, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET message_ids=excluded.message_ids, "
        "thread_text=excluded.thread_text, summary=excluded.summary, "
        "category=excluded.category, model=excluded.model, created_at=excluded.created_at",
        (
            url, message_ids, thread_text, summary, category or "", model,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_inbox_rows(conn=None) -> dict[str, dict]:
    """Return {job_url: inbox_cache_row} for all cached insights."""
    conn = conn or get_connection()
    rows = conn.execute("SELECT * FROM inbox_cache").fetchall()
    return {r["url"]: dict(r) for r in rows}


def delete_inbox_cache(url: str) -> None:
    """Clear the cached inbox insight for a job."""
    conn = get_connection()
    conn.execute("DELETE FROM inbox_cache WHERE url = ?", (url,))
    conn.commit()


def clear_inbox_cache() -> int:
    """Wipe the entire inbox index so the next scan refetches. Returns rows removed."""
    conn = get_connection()
    cur = conn.execute("DELETE FROM inbox_cache")
    conn.commit()
    return cur.rowcount
