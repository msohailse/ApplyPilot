"""Aggregate skill analysis across high-scoring jobs (LLM-powered).

Reads the jobs you're strongly matched to (fit_score >= threshold), asks the
configured LLM for a prioritized, deduplicated skill breakdown, and saves the
result so it survives dashboard regeneration.
"""

import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import APP_DIR
from applypilot.database import get_connection
from applypilot.llm import get_client

log = logging.getLogger(__name__)

SKILLS_PATH = APP_DIR / "skills_analysis.md"
SKILLS_META_PATH = APP_DIR / "skills_analysis.json"

# Keep the prompt bounded: top N jobs, each description capped.
DEFAULT_LIMIT = 40
DESC_CHARS = 1200

_PROMPT = (
    "You are a career analytics assistant. You receive many job postings the "
    "candidate is strongly matched to. Produce ONE consolidated, prioritized "
    "skills analysis in GitHub-flavored Markdown.\n\n"
    "Use exactly these sections:\n"
    "## Top skills (ranked)\n"
    "Ranked list of the most in-demand skills/technologies. For each, give an "
    "approximate count of how many postings mention it, e.g. \"Kubernetes - ~18/40\".\n"
    "## By category\n"
    "Group the skills under: Languages, Frameworks & Libraries, Cloud & DevOps, "
    "Data & AI, Databases, Testing & Quality, Other.\n"
    "## Must-have vs nice-to-have\n"
    "Split into the recurring requirements vs the differentiators.\n"
    "## Gaps to close next\n"
    "Specific, actionable things the candidate should learn or strengthen, with "
    "a one-line reason each.\n"
    "## Role themes\n"
    "The dominant domains/titles and what they have in common.\n\n"
    "Rules: deduplicate synonyms (React.js = React). Only use skills that appear "
    "in the postings. Be concrete and scannable. No preamble."
)


def _fetch_jobs(min_score: int, limit: int) -> list[dict]:
    rows = get_connection().execute(
        "SELECT title, company, site, location, fit_score, full_description "
        "FROM jobs WHERE fit_score >= ? AND full_description IS NOT NULL "
        "AND full_description != '' ORDER BY fit_score DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def analyze_skills(min_score: int = 7, limit: int = DEFAULT_LIMIT) -> dict:
    """Run the LLM analysis over high-scoring jobs and persist the markdown."""
    jobs = _fetch_jobs(min_score, limit)
    if not jobs:
        raise ValueError(f"No jobs with score >= {min_score} and a description.")

    blocks = []
    for j in jobs:
        company = j.get("company") or j.get("site") or ""
        blocks.append(
            f"### {j.get('title') or 'Role'} — {company} (score {j.get('fit_score')})\n"
            f"{(j.get('full_description') or '')[:DESC_CHARS]}"
        )
    corpus = "\n\n".join(blocks)

    client = get_client("highlight")
    markdown = client.chat(
        [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": f"JOB POSTINGS ({len(jobs)}):\n\n{corpus}"},
        ],
        temperature=0.2,
        max_tokens=2600,
    )

    meta = {
        "jobs": len(jobs),
        "min_score": min_score,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    SKILLS_PATH.write_text(markdown, encoding="utf-8")
    SKILLS_META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("Skills analysis saved (%d jobs, score >= %d).", len(jobs), min_score)
    return {"ok": True, **meta}


def load_skills_analysis() -> dict | None:
    """Return the saved analysis (markdown + meta), or None if absent."""
    if not SKILLS_PATH.exists():
        return None
    markdown = SKILLS_PATH.read_text(encoding="utf-8")
    meta: dict = {}
    if SKILLS_META_PATH.exists():
        try:
            meta = json.loads(SKILLS_META_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
    return {"markdown": markdown, **meta}


def delete_skills_analysis() -> None:
    """Remove the saved analysis."""
    for path in (SKILLS_PATH, SKILLS_META_PATH):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("Could not delete %s: %s", path, exc)


def md_to_html(md: str) -> str:
    """Minimal Markdown -> HTML (headings, bullets, bold, paragraphs)."""
    out: list[str] = []
    in_list = False
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape HTML then apply inline bold/code."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text
