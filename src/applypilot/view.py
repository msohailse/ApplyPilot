"""ApplyPilot HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering
"""

from __future__ import annotations

import os
import shutil
import webbrowser
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

from rich.console import Console

from applypilot.config import (
    APP_DIR,
    DB_PATH,
    TAILORED_DIR,
    COVER_LETTER_DIR,
    list_resume_variants,
    load_profile,
)
from applypilot.database import (
    ensure_columns, get_connection, get_inbox_summaries, get_study_jobs,
)
from applypilot.enrichment.classify import backfill_classifications, backfill_company_from_site
from applypilot.todos import load_todos
from applypilot.answers import load_answers

console = Console()


def _render_study_html() -> str:
    """Render the 'Personal Note for Study' panel (jobs marked as learning targets)."""
    jobs = get_study_jobs()
    if not jobs:
        return ""
    items = ""
    for j in jobs:
        url = escape(j.get("url") or "")
        title = escape(j.get("title") or "Job")
        comp = escape(j.get("company") or j.get("site") or "")
        note = escape(j.get("study_note") or "").replace("\n", "<br>")
        score = j.get("fit_score")
        meta = f' <span class="study-co">{comp}</span>' if comp else ""
        score_s = f' <span class="study-score">score {score}</span>' if score else ""
        items += (
            f'<div class="study-item">'
            f'<a href="{url}" target="_blank" rel="noopener">{title}</a>{meta}{score_s}'
            f'<div class="study-note">{note}</div>'
            f"</div>"
        )
    return f'<div class="study-panel"><h3>Personal Note for Study</h3>{items}</div>'


def _render_answers_html() -> str:
    """Render the 'Answer ideas' panel (click-to-expand application answers)."""
    answers = load_answers()
    items = ""
    for a in answers:
        aid = escape(a.get("id", ""))
        q = escape(a.get("question", "") or "")
        ans = escape(a.get("answer", "") or "").replace("\n", "<br>")
        items += (
            f'<details class="answer-item" data-id="{aid}">'
            f"<summary>{q}</summary>"
            f'<div class="answer-text">{ans}</div>'
            f'<button class="answer-del" title="Delete" onclick="deleteAnswer(this)">×</button>'
            f"</details>"
        )
    if not items:
        items = '<p class="todo-empty">No answer ideas yet.</p>'
    return f"""<div class="answers-panel">
  <h3>Answer ideas (click to expand)</h3>
  {items}
  <details class="answer-add-wrap">
    <summary class="answer-add-summary">+ Add an answer idea</summary>
    <div class="answer-add">
      <input id="answer-q" class="todo-input" placeholder="Question">
      <textarea id="answer-a" class="todo-input answer-textarea" placeholder="Your answer"></textarea>
      <button class="todo-add-btn" onclick="addAnswer()">Add</button>
    </div>
  </details>
</div>"""


def _render_todos_html() -> str:
    """Render the todo panel (notes/tasks with optional link + tag)."""
    todos = load_todos()

    tags: list[str] = []
    for t in todos:
        tag = (t.get("tag") or "").strip()
        if tag and tag not in tags:
            tags.append(tag)
    tags.sort(key=str.lower)

    items = ""
    for t in todos:
        tid = escape(t.get("id", ""))
        text = escape(t.get("text", "") or "")
        url = escape(t.get("url", "") or "")
        tag = (t.get("tag") or "").strip()
        done = bool(t.get("done"))
        body = (
            f'<a href="{url}" target="_blank" rel="noopener">{text}</a>'
            if url else f"<span>{text}</span>"
        )
        tag_html = f'<span class="todo-tag">{escape(tag)}</span>' if tag else ""
        checked = "checked" if done else ""
        cls = "todo-item done" if done else "todo-item"
        items += (
            f'<li class="{cls}" data-id="{tid}" data-tag="{escape(tag)}">'
            f'<input type="checkbox" {checked} onchange="toggleTodo(this)">'
            f'<div class="todo-main">{body}{tag_html}</div>'
            f'<button class="todo-del" title="Delete" onclick="deleteTodo(this)">×</button>'
            f"</li>"
        )
    if not items:
        items = '<li class="todo-empty">No todos yet.</li>'

    tag_options = '<option value="__all">All tags</option>'
    for tag in tags:
        tag_options += f'<option value="{escape(tag)}">{escape(tag)}</option>'
    tag_datalist = "".join(f'<option value="{escape(tag)}"></option>' for tag in tags)

    return f"""<div class="todo-panel">
  <h3>Todo</h3>
  <div class="todo-filter-row">
    <span class="todo-filter-label">Filter by tag:</span>
    <select id="todo-tag-filter" class="filter-select" onchange="filterTodos(this.value)">{tag_options}</select>
  </div>
  <ul class="todo-list">{items}</ul>
  <div class="todo-add">
    <input id="todo-text" class="todo-input" placeholder="Add a note / task..." onkeydown="if(event.key==='Enter')addTodo()">
    <input id="todo-tag" class="todo-input todo-tag-input" list="todo-tag-list" placeholder="tag (optional)" onkeydown="if(event.key==='Enter')addTodo()">
    <input id="todo-url" class="todo-input" placeholder="link (optional)" onkeydown="if(event.key==='Enter')addTodo()">
    <button class="todo-add-btn" onclick="addTodo()">Add</button>
  </div>
  <datalist id="todo-tag-list">{tag_datalist}</datalist>
</div>"""


def _job_files_html(job: dict) -> str:
    """Files row (Resume/Cover links, each with a per-file delete ×)."""
    job_url_q = quote(job.get("url") or "")
    resume_txt = job.get("tailored_resume_path") or ""
    combined_tex = job.get("combined_tex_path") or ""
    cover_txt = job.get("cover_letter_path") or ""
    items: list[str] = []

    def add(kind: str, label: str, path: "Path | None") -> None:
        if not path or not path.exists():
            return
        items.append(
            f'<span class="file-item">'
            f'<a class="file-link" href="/download?url={job_url_q}&kind={kind}" '
            f'download title="Download {label}">{label}</a>'
            f'<button class="file-x" title="Delete {label} permanently" '
            f'onclick="deleteJobFile(this, \'{kind}\', \'{label}\')">×</button>'
            f'</span>'
        )

    if resume_txt:
        add("resume_pdf", "Resume PDF", Path(resume_txt).with_suffix(".pdf"))
        add("resume_txt", "Resume TXT", Path(resume_txt))
    if combined_tex:
        add("resume_tex", "Resume TEX", Path(combined_tex))
    if cover_txt:
        add("cover_pdf", "Cover PDF", Path(cover_txt).with_suffix(".pdf"))
        add("cover_txt", "Cover TXT", Path(cover_txt))
    return (
        f'<div class="files-row"><span class="files-label">Files:</span>{"".join(items)}</div>'
        if items else ""
    )


def _job_actions_html(job: dict) -> str:
    """Generate Resume / Generate Cover Letter buttons."""
    html = ""
    if job.get("full_description"):
        html += (
            '<button class="mark-btn combine" onclick="combineResume(this)" '
            'title="Tailor this job (if needed) and render into your LaTeX resume">Generate Resume</button>'
        )
        html += (
            '<button class="mark-btn cover-generate" onclick="generateCoverLetter(this)" '
            'title="Generate a cover letter for this job">Generate Cover Letter</button>'
        )
    return html


def _job_concepts_html(job: dict) -> str:
    """Key-concepts chips row (from the highlight LLM extraction)."""
    import json as _json

    try:
        concepts = _json.loads(job.get("highlight_concepts") or "[]")
    except (ValueError, TypeError):
        concepts = []
    if not concepts:
        return ""
    chips = "".join(f'<span class="concept-chip">{escape(str(c))}</span>' for c in concepts)
    return f'<div class="highlight-row"><span class="highlight-label">Key concepts:</span>{chips}</div>'


def _job_inbox_html(job: dict, inbox_cached: str | None) -> str:
    """Inbox-insights panel for a job (only when cached or applied)."""
    st = job.get("apply_status") or ""
    if inbox_cached is None and st not in ("applied", "success"):
        return ""
    if inbox_cached is not None:
        return (
            '<div class="inbox-panel">'
            '<div class="inbox-head">'
            '<span class="inbox-title">Inbox insights</span>'
            '<button class="mark-btn inbox-scan" onclick="scanInbox(this)">Refresh</button>'
            '<button class="mark-btn combine-del" onclick="freeInbox(this)">Free</button>'
            '</div>'
            f'<pre class="inbox-body">{escape(inbox_cached)}</pre>'
            '</div>'
        )
    return (
        '<div class="inbox-panel">'
        '<button class="mark-btn inbox-scan" onclick="scanInbox(this)">Inbox Insights</button>'
        '</div>'
    )


def _downloads_dir(kind: str) -> Path:
    """Per-kind downloads folder: a ``downloads`` subdir of the resume/cover dir."""
    base = TAILORED_DIR if kind.startswith("resume") else COVER_LETTER_DIR
    return base / "downloads"


def _resolve_job_file_path(job_url: str, kind: str) -> "Path | None":
    """Resolve the on-disk path for a job file kind (None if unavailable)."""
    row = get_connection().execute(
        "SELECT tailored_resume_path, cover_letter_path, combined_tex_path "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()
    if not row:
        return None
    if kind == "resume_tex":
        base = row[2]
    elif kind.startswith("resume"):
        base = row[0]
    else:
        base = row[1]
    if not base:
        return None
    path = Path(base)
    if kind.endswith("_pdf"):
        path = path.with_suffix(".pdf")
    return path.resolve()


def render_dashboard_html() -> str:
    """Build the full HTML dashboard as a string.

    Returns:
        The complete HTML document.
    """
    conn = get_connection()

    # Forward-migrate any columns added after this DB was created (e.g. the
    # Combine Resume fields) so the query below never fails on an older DB.
    ensure_columns(conn)

    # Cached Gmail inbox insights (job_url -> summary), rendered on applied jobs.
    inbox_summaries = get_inbox_summaries(conn)

    # Regex-only safety backfill (never calls the LLM). Country/language are
    # decided during the pipeline (discovery + scoring) and via `applypilot classify`.
    backfill_classifications(conn)
    backfill_company_from_site(conn)

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All jobs (scored and unscored), ordered by score desc with unscored last
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning, apply_status,
               language_requirement, employment_type, country, work_mode,
               tailored_resume_path, combined_tex_path, cover_letter_path, notes, apply_error, company, study_note,
               focused, stale, highlighted, highlight_concepts
        FROM jobs
        ORDER BY fit_score DESC NULLS LAST, site, title
    """).fetchall()

    # Distinct values for the filters
    lang_values = [r[0] for r in conn.execute(
        "SELECT DISTINCT language_requirement FROM jobs "
        "WHERE language_requirement IS NOT NULL ORDER BY language_requirement"
    ).fetchall()]
    type_values = [r[0] for r in conn.execute(
        "SELECT DISTINCT employment_type FROM jobs "
        "WHERE employment_type IS NOT NULL ORDER BY employment_type"
    ).fetchall()]
    country_values = [r[0] for r in conn.execute(
        "SELECT DISTINCT country FROM jobs "
        "WHERE country IS NOT NULL ORDER BY country"
    ).fetchall()]
    mode_values = [r[0] for r in conn.execute(
        "SELECT DISTINCT work_mode FROM jobs "
        "WHERE work_mode IS NOT NULL"
    ).fetchall()]
    mode_order = {"Remote": 0, "Hybrid": 1, "On-site": 2}
    mode_values.sort(key=lambda m: mode_order.get(m, 99))
    company_values = [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs "
        "WHERE company IS NOT NULL AND trim(company) != '' ORDER BY lower(company)"
    ).fetchall()]

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row clickable" data-score="{s}" onclick="filterExactScore(this.dataset.score)" title="Show only jobs scored {s}">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Site stats rows
    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        site_rows += f"""
        <div class="site-row clickable" data-site="{escape(site)}" onclick="filterSite(this.dataset.site)" title="Show only {escape(site)} jobs">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    unscored_count = total - scored
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            if score == 0:
                score_color = "#64748b"
                score_label = "Not Scored"
                score_badge_text = "–"
                count_at_score = unscored_count
            else:
                score_color = "#10b981" if score >= 7 else ("#f59e0b" if score >= 5 else "#ef4444")
                score_label = {
                    10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                    7: "Good Fit", 6: "Moderate+", 5: "Moderate",
                }.get(score, f"Score {score}")
                score_badge_text = str(score)
                count_at_score = score_dist.get(score, 0)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score_badge_text}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")
        language_req = escape(j["language_requirement"] or "")
        job_note = escape(j["notes"] or "")
        company_display = escape(j["company"] or "")
        study_display = escape(j["study_note"] or "")
        study_marked = " in-study" if (j["study_note"] or "").strip() else ""
        focused_flag = int(j["focused"] or 0)
        focused_cls = " focused" if focused_flag else ""
        focus_btn_label = "Unfocus" if focused_flag else "Focus"
        focus_btn = (
            f'<button class="mark-btn focus{" active" if focused_flag else ""}" '
            f'onclick="toggleFocus(this)">{focus_btn_label}</button>'
        )
        highlight_flag = int(j["highlighted"] or 0)
        highlight_cls = " highlighted" if highlight_flag else ""
        highlight_btn_label = "Remove Highlight" if highlight_flag else "Generate Highlight"
        highlight_btn = (
            f'<button class="mark-btn highlight{" active" if highlight_flag else ""}" '
            f'onclick="toggleHighlight(this)">{highlight_btn_label}</button>'
        )
        import json as _json
        try:
            _concepts = _json.loads(j["highlight_concepts"] or "[]")
        except (ValueError, TypeError):
            _concepts = []
        concepts_html = ""
        if _concepts:
            chips = "".join(
                f'<span class="concept-chip">{escape(str(c))}</span>' for c in _concepts
            )
            concepts_html = (
                f'<div class="highlight-row"><span class="highlight-label">Key concepts:'
                f'</span>{chips}</div>'
            )
        stale_flag = int(j["stale"] or 0)
        stale_cls = " stale" if stale_flag else ""
        stale_btn = (
            f'<button class="stale-x" title="'
            f'{"Restore from Stale" if stale_flag else "Move to Stale"}" '
            f'onclick="toggleStale(this)">{"↩" if stale_flag else "×"}</button>'
        )
        # Post-application outcome buttons. Always rendered; shown via the
        # card's "is-applied" class (so they appear right after marking Applied).
        outcome_btns = (
            '<button class="mark-btn interview" onclick="markJob(this,\'interviewing\')">Interviewing</button>'
            '<button class="mark-btn reject" onclick="markJob(this,\'rejected\')">Rejected</button>'
            '<button class="mark-btn nodeal" onclick="markJob(this,\'no_deal\')">No deal</button>'
        )
        applied_cls = " is-applied" if (j["apply_status"] or "") in (
            "applied", "success", "interviewing", "offer", "rejected", "no_deal"
        ) else ""

        # Share links (email + WhatsApp) with a pre-filled message.
        share_title_raw = (j["title"] or "Job").strip()
        share_msg = share_title_raw
        if j["site"]:
            share_msg += f" at {j['site']}"
        if j["location"]:
            share_msg += f" ({j['location']})"
        share_msg += f"\n{j['url'] or ''}"
        share_enc = quote(share_msg)
        email_href = "mailto:?subject=" + quote(f"Job: {share_title_raw}") + "&body=" + share_enc
        wa_href = "https://wa.me/?text=" + share_enc
        if language_req:
            lang_flag_html = (
                f'<span class="lang-flag-tag req">Language required: {language_req}</span>'
            )
        else:
            lang_flag_html = '<span class="lang-flag-tag none">No language required</span>'
        employment_type = escape(j["employment_type"] or "")
        country = escape(j["country"] or "")
        work_mode = escape(j["work_mode"] or "")

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")

        meta_parts = []
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        if language_req:
            meta_parts.append(f'<span class="meta-tag lang-tag">Lang: {language_req}</span>')
        if employment_type:
            meta_parts.append(f'<span class="meta-tag type-tag">{employment_type}</span>')
        if work_mode:
            meta_parts.append(f'<span class="meta-tag mode-tag">{work_mode}</span>')
        if country:
            meta_parts.append(f'<span class="meta-tag country-tag">{country}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = ""
        if apply_url:
            apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>'

        st = j["apply_status"] or ""
        status_label = {
            "applied": ("Applied", "#10b981"),
            "success": ("Applied", "#10b981"),
            "interviewing": ("Interviewing", "#3b82f6"),
            "offer": ("Offer", "#22c55e"),
            "rejected": ("Rejected", "#ef4444"),
            "no_deal": ("No deal", "#64748b"),
            "failed": ("Failed", "#ef4444"),
            "not_available": ("Not Available", "#94a3b8"),
            "not_interested": ("Not Interested", "#64748b"),
            "expired": ("Expired", "#f97316"),
        }.get(st)
        if status_label:
            reason_tip = escape(j["apply_error"] or "")
            status_badge = (
                f'<span class="meta-tag status-badge" title="{reason_tip}" '
                f'style="background:{status_label[1]};'
                f'color:#0f172a;font-weight:bold;margin-left:auto">{status_label[0]}</span>'
            )
        else:
            status_badge = (
                '<span class="meta-tag status-badge" '
                'style="display:none;margin-left:auto"></span>'
            )

        # Generated documents (tailored resume + cover letter) as clickable links.
        job_dict = dict(j)
        files_html = _job_files_html(job_dict)

        # Generate Resume / Generate Cover Letter (on demand).
        combine_html = _job_actions_html(job_dict)

        # Inbox insights: only on applied jobs (or when already cached).
        inbox_html = _job_inbox_html(job_dict, inbox_summaries.get(j["url"]))

        job_sections += f"""
        <div class="job-card{focused_cls}{stale_cls}{applied_cls}{highlight_cls}" data-focused="{focused_flag}" data-highlighted="{highlight_flag}" data-stale="{stale_flag}" data-score="{score}" data-url="{escape(j['url'] or '')}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}" data-apply-status="{escape(j['apply_status'] or '')}" data-language="{('none' if not j['language_requirement'] else (j['language_requirement'] or '').lower())}" data-employment-type="{(j['employment_type'] or '').lower()}" data-country="{(j['country'] or '').lower()}" data-work-mode="{(j['work_mode'] or '').lower()}" data-company="{(j['company'] or '').lower()}">
          {stale_btn}
          <div class="card-header">
            <span class="score-pill" style="background:{'#64748b' if score == 0 else ('#10b981' if score >= 7 else ('#f59e0b' if score >= 5 else '#ef4444'))}">{'–' if score == 0 else score}</span>
            <div class="title-block">
              <a href="{url}" class="job-title" target="_blank">{title}</a>
              {f'<div class="company-line">{company_display}</div>' if company_display else ''}
            </div>
            <div class="card-actions">
              {status_badge}
              {highlight_btn}
              {focus_btn}
            </div>
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          <div class="concepts-slot">{concepts_html}</div>
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          <div class="files-slot">{files_html}</div>
          <div class="inbox-slot">{inbox_html}</div>
          <div class="mark-row">
            <button class="mark-btn view" onclick="openJobModal(this)">View</button>
            <button class="mark-btn pass" onclick="markJob(this,'applied')">Applied</button>
            <button class="mark-btn fail" onclick="markJob(this,'failed')">Failed</button>
            <button class="mark-btn na" onclick="markJob(this,'not_available')">Not Available</button>
            <button class="mark-btn ni" onclick="markJob(this,'not_interested')">Not Interested</button>
            <span class="outcome-btns">{outcome_btns}</span>
            <span class="actions-slot">{combine_html}</span>
          </div>
          <div class="lang-flag">{lang_flag_html}</div>
          <div class="job-note">
            <input class="note-input" placeholder="Add a note..." value="{job_note}" onchange="saveNote(this)">
          </div>
          <details class="study-details{study_marked}">
            <summary class="study-summary">Personal Note for Study{(' ✓' if study_marked else '')}</summary>
            <textarea class="study-input" placeholder="e.g. TypeScript, AWS (Serverless, SQS, SNS etc) is a nice-to-have - skills to learn, gaps, notes..." onchange="saveStudy(this)">{study_display}</textarea>
            <div class="study-actions">
              <button class="mark-btn study-save" onclick="saveStudy(this.closest('.study-details').querySelector('.study-input'), this)">Save</button>
              <span class="study-hint"></span>
            </div>
          </details>
          <div class="card-footer">{apply_html}</div>
          <div class="share-row">
            <span class="share-label">Share:</span>
            <a class="share-link email" href="{email_href}">Email</a>
            <a class="share-link whatsapp" href="{wa_href}" target="_blank" rel="noopener">WhatsApp</a>
          </div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    # Language dropdown
    lang_options = '<option value="any" selected>Any</option>'
    lang_options += '<option value="none">None required</option>'
    for lv in lang_values:
        lang_options += f'<option value="{lv.lower()}">{escape(lv)}</option>'

    type_buttons = '<button class="filter-btn type-btn active" onclick="filterType(\'any\', event)">Any</button>'
    for tv in type_values:
        tv_js = tv.lower().replace("'", "\\'")
        type_buttons += f'<button class="filter-btn type-btn" onclick="filterType(\'{tv_js}\', event)">{escape(tv)}</button>'

    country_options = '<option value="any" selected>Any country</option>'
    for cv in country_values:
        cv_js = cv.lower()
        country_options += f'<option value="{cv_js}">{escape(cv)}</option>'

    company_datalist = "".join(
        f'<option value="{escape(c)}"></option>' for c in company_values
    )

    try:
        _base_country = (load_profile().get("personal", {}) or {}).get("country", "")
    except Exception:  # noqa: BLE001 - profile missing is non-fatal for rendering
        _base_country = ""
    _default_variant_label = f"Default ({_base_country})" if _base_country else "Default"
    variant_options = f'<option value="">{escape(_default_variant_label)}</option>'
    for _v in list_resume_variants():
        variant_options += f'<option value="{escape(_v)}">{escape(_v)}</option>'

    mode_buttons = '<button class="filter-btn mode-btn active" onclick="filterWorkMode(\'any\', event)">Any</button>'
    for mv in mode_values:
        mv_js = mv.lower().replace("'", "\\'")
        mode_buttons += f'<button class="filter-btn mode-btn" onclick="filterWorkMode(\'{mv_js}\', event)">{escape(mv)}</button>'

    todos_html = _render_todos_html()
    study_html = _render_study_html()
    answers_html = _render_answers_html()

    # Pipeline outcome stats for the Stats tab (computed from the DB).
    try:
        sc = dict(
            get_connection().execute(
                "SELECT COALESCE(apply_status, 'not_applied'), COUNT(*) "
                "FROM jobs GROUP BY 1"
            ).fetchall()
        )
        _stale_count = get_connection().execute(
            "SELECT COUNT(*) FROM jobs WHERE stale = 1"
        ).fetchone()[0]
        _by_date = get_connection().execute(
            "SELECT date(applied_at) AS d, COUNT(*) FROM jobs "
            "WHERE applied_at IS NOT NULL AND applied_at != '' "
            "GROUP BY d ORDER BY d DESC LIMIT 90"
        ).fetchall()
    except Exception:
        sc, _stale_count, _by_date = {}, 0, []

    def _n(*keys):
        return sum(int(sc.get(k, 0) or 0) for k in keys)

    def _rate(n, d):
        return f"{(100.0 * n / d):.0f}%" if d else "–"

    _applied_only = _n("applied", "success")
    _applied_total = _n("applied", "success", "interviewing", "offer", "rejected", "no_deal")

    # Recent-activity counts to keep momentum visible.
    from datetime import date as _date, timedelta as _timedelta

    _today = _date.today()
    _today_iso = _today.isoformat()
    _week_iso = (_today - _timedelta(days=6)).isoformat()
    _month_iso = _today.replace(day=1).isoformat()
    _today_n = sum(int(c) for d, c in _by_date if str(d) == _today_iso)
    _week_n = sum(int(c) for d, c in _by_date if d and str(d) >= _week_iso)
    _month_n = sum(int(c) for d, c in _by_date if d and str(d) >= _month_iso)

    _state_rows = [
        ("Applied (awaiting response)", _applied_only, "100%" if _applied_only else "–"),
        ("Interviewing", _n("interviewing"), _rate(_n("interviewing"), _applied_total)),
        ("Offers", _n("offer"), _rate(_n("offer"), _applied_total)),
        ("Rejected", _n("rejected"), _rate(_n("rejected"), _applied_total)),
        ("No deal", _n("no_deal"), _rate(_n("no_deal"), _applied_total)),
        ("Applied (total, incl. outcomes)", _applied_total, "100%" if _applied_total else "–"),
        ("Failed", _n("failed"), "–"),
        ("Not available", _n("not_available"), "–"),
        ("Not interested", _n("not_interested"), "–"),
        ("Expired", _n("expired"), "–"),
        ("Not applied", _n("not_applied"), "–"),
        ("Stale", int(_stale_count or 0), "–"),
    ]
    stats_rows_html = "".join(
        f'<tr><td>{escape(str(label))}</td>'
        f'<td class="num">{count}</td><td class="num">{rate}</td></tr>'
        for label, count, rate in _state_rows
    )
    date_rows_html = "".join(
        f'<tr><td>{escape(str(d))}</td><td class="num">{c}</td></tr>'
        for d, c in _by_date
    ) or '<tr><td colspan="2" class="muted">No applications yet</td></tr>'
    stats_html = f"""
<div class="stats-panel">
  <div class="report-hero">
    <div class="report-hero-num">{_applied_total}</div>
    <div class="report-hero-label">applications sent</div>
    <div class="report-hero-sub">
      <span class="rh-chip"><b>{_today_n}</b>&nbsp;today</span>
      <span class="rh-chip"><b>{_week_n}</b>&nbsp;last 7 days</span>
      <span class="rh-chip"><b>{_month_n}</b>&nbsp;this month</span>
    </div>
  </div>
  <h2>Pipeline &amp; success ratio</h2>
  <div class="stats-grid">
    <table class="stats-table">
      <thead><tr><th>Stage</th><th class="num">Jobs</th>
        <th class="num">Rate (of applied)</th></tr></thead>
      <tbody>{stats_rows_html}</tbody>
    </table>
    <table class="stats-table">
      <thead><tr><th>Date applied</th><th class="num">Jobs</th></tr></thead>
      <tbody>{date_rows_html}</tbody>
    </table>
  </div>
  <p class="subtitle">Stale jobs are kept here and in exports so success-rate
    stays accurate.</p>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}

  /* Todo panel */
  .todo-panel {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; border-left: 3px solid #60a5fa; }}
  .todo-panel h3 {{ font-size: 1rem; margin-bottom: 0.75rem; color: #94a3b8; }}
  .todo-list {{ list-style: none; margin-bottom: 0.75rem; }}
  .todo-item {{ display: flex; align-items: center; gap: 0.5rem; padding: 0.3rem 0; font-size: 0.9rem; }}
  .todo-item input[type=checkbox] {{ cursor: pointer; width: 15px; height: 15px; }}
  .todo-item a {{ color: #93c5fd; text-decoration: none; }}
  .todo-item a:hover {{ text-decoration: underline; }}
  .todo-item.done a, .todo-item.done span {{ color: #64748b; text-decoration: line-through; }}
  .todo-main {{ display: flex; flex-direction: column; min-width: 0; }}
  .todo-tag {{ align-self: flex-start; font-size: 0.66rem; padding: 0.03rem 0.45rem;
    border-radius: 999px; background: #1e3a5f; color: #93c5fd; border: 1px solid #60a5fa55; margin-top: 0.15rem; }}
  .todo-filter-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }}
  .todo-filter-label {{ font-size: 0.75rem; color: #94a3b8; }}
  .todo-tag-input {{ max-width: 120px; }}
  .todo-empty {{ color: #64748b; font-size: 0.85rem; }}
  .todo-del {{ margin-left: auto; background: none; border: none; color: #64748b; cursor: pointer; font-size: 1.1rem; line-height: 1; }}
  .todo-del:hover {{ color: #ef4444; }}
  .todo-add {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
  .todo-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.7rem; border-radius: 6px; font-size: 0.8rem; }}
  .todo-input:first-of-type {{ flex: 1; min-width: 200px; }}
  .todo-add-btn {{ background: #60a5fa; border: none; color: #0f172a; font-weight: 600; padding: 0.4rem 0.9rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }}
  .todo-add-btn:hover {{ background: #93c5fd; }}

  /* Answer ideas */
  .answers-panel {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-top: 2.5rem; border-left: 3px solid #10b981; }}
  .answers-panel h3 {{ font-size: 1rem; margin-bottom: 0.75rem; color: #94a3b8; }}
  .answer-item {{ border-bottom: 1px solid #334155; padding: 0.4rem 0; position: relative; }}
  .answer-item summary {{ cursor: pointer; font-size: 0.9rem; color: #93c5fd; font-weight: 500; padding-right: 1.5rem; }}
  .answer-item summary:hover {{ color: #bfdbfe; }}
  .answer-text {{ font-size: 0.85rem; color: #cbd5e1; line-height: 1.65; padding: 0.6rem 0.2rem 0.5rem 0; }}
  .answer-del {{ position: absolute; top: 0.3rem; right: 0; background: none; border: none; color: #64748b; cursor: pointer; font-size: 1.1rem; line-height: 1; }}
  .answer-del:hover {{ color: #ef4444; }}
  .answer-add-wrap {{ margin-top: 0.75rem; }}
  .answer-add-summary {{ cursor: pointer; color: #60a5fa; font-size: 0.85rem; }}
  .answer-add {{ display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.75rem; }}
  .answer-textarea {{ min-height: 90px; resize: vertical; font-family: inherit; }}

  /* Personal Note for Study */
  .study-panel {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; border-left: 3px solid #f59e0b; }}
  .study-panel h3 {{ font-size: 1rem; margin-bottom: 0.75rem; color: #94a3b8; }}
  .study-item {{ padding: 0.4rem 0; border-bottom: 1px solid #334155; font-size: 0.9rem; }}
  .study-item a {{ color: #93c5fd; text-decoration: none; font-weight: 600; }}
  .study-co {{ color: #94a3b8; font-size: 0.8rem; }}
  .study-score {{ color: #10b981; font-size: 0.75rem; }}
  .study-note {{ font-size: 0.82rem; color: #cbd5e1; margin-top: 0.25rem; white-space: pre-wrap; }}
  .study-details {{ margin: 0.4rem 0; }}
  .study-summary {{ font-size: 0.78rem; color: #94a3b8; cursor: pointer; }}
  .study-details.in-study > summary {{ color: #f59e0b; }}
  .study-input {{ width: 100%; min-height: 60px; margin-top: 0.4rem; background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 0.4rem 0.6rem; border-radius: 6px; font-size: 0.78rem; font-family: inherit; resize: vertical; }}
  .study-actions {{ display: flex; align-items: center; gap: 0.5rem; margin-top: 0.35rem; }}
  .study-hint {{ font-size: 0.72rem; color: #10b981; }}
  .mark-btn.study-save {{ background: #1e3a5f; border-color: #2a7ab5; color: #93c5fd; }}
  .mark-btn.study-save:hover {{ background: #2a7ab5; border-color: #2a7ab5; color: #fff; }}

  /* Job note input */
  .job-note {{ margin: 0.4rem 0 0.2rem; }}
  .note-input {{ width: 100%; background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 0.35rem 0.6rem; border-radius: 6px; font-size: 0.78rem; }}
  .note-input::placeholder {{ color: #64748b; }}
  .note-input.saved {{ border-color: #10b981; }}

  /* Modal */
  .modal-overlay {{ display: none; position: fixed; inset: 0; background: #000000aa; z-index: 1000; align-items: flex-start; justify-content: center; padding: 3rem 1rem; overflow-y: auto; }}
  .modal-box {{ background: #1e293b; border-radius: 12px; max-width: 820px; width: 100%; padding: 1.5rem; position: relative; border: 1px solid #334155; }}
  .modal-close {{ position: absolute; top: 0.6rem; right: 0.8rem; background: none; border: none; color: #94a3b8; font-size: 1.5rem; cursor: pointer; line-height: 1; }}
  .modal-close:hover {{ color: #fff; }}
  .modal-title {{ font-size: 1.15rem; margin-bottom: 0.5rem; padding-right: 2rem; }}
  .modal-title a {{ color: #e2e8f0; text-decoration: none; }}
  .modal-title a:hover {{ color: #60a5fa; }}
  .modal-meta {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.75rem; }}
  .modal-reason {{ font-size: 0.85rem; color: #94a3b8; font-style: italic; margin-bottom: 0.75rem; }}
  .modal-desc {{ font-size: 0.82rem; color: #cbd5e1; line-height: 1.6; white-space: pre-wrap; word-break: break-word; background: #0f172a; border-radius: 8px; padding: 0.9rem; max-height: 60vh; overflow-y: auto; margin-top: 0.75rem; }}
  .reason-box {{ max-width: 520px; }}
  .reason-options {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; }}
  .reason-btn {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.45rem 0.9rem; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
  .reason-btn:hover {{ background: #475569; }}
  .reason-other-row {{ display: flex; gap: 0.5rem; margin-bottom: 0.75rem; }}
  .reason-other-row .todo-input {{ flex: 1; }}
  .reason-cancel {{ background: none; border: none; color: #94a3b8; cursor: pointer; font-size: 0.85rem; }}
  .reason-cancel:hover {{ color: #fff; }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; }}
  .clickable-stat {{ cursor: pointer; transition: transform 0.15s; }}
  .clickable-stat:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .stat-applied .stat-num {{ color: #10b981; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}

  /* Filters */
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: #64748b; }}
  .filter-select {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.6rem; border-radius: 6px; font-size: 0.8rem; cursor: pointer; }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist > summary, .sites-section > summary {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; cursor: pointer; font-weight: 600; list-style: none; display: flex; align-items: center; gap: 0.4rem; }}
  .score-dist > summary::-webkit-details-marker, .sites-section > summary::-webkit-details-marker {{ display: none; }}
  .score-dist > summary::before, .sites-section > summary::before {{ content: '▸'; display: inline-block; transition: transform 0.15s; }}
  .score-dist[open] > summary::before, .sites-section[open] > summary::before {{ transform: rotate(90deg); }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}

  /* Site bars */
  .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Clickable chart rows (score bars / sources act as filters) */
  .score-row.clickable, .site-row.clickable {{ cursor: pointer; border-radius: 6px; padding: 2px 4px; transition: background 0.15s; }}
  .score-row.clickable:hover, .site-row.clickable:hover {{ background: #33415555; }}
  .score-row.clickable.active, .site-row.clickable.active {{ background: #2a7ab5aa; box-shadow: inset 0 0 0 1px #60a5fa; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ position: relative; background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px #00000044; }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}
  .job-card[data-score="3"], .job-card[data-score="4"] {{ border-left-color: #ef4444; }}
  .job-card[data-score="1"], .job-card[data-score="2"] {{ border-left-color: #ef444455; }}
  .job-card[data-score="0"] {{ border-left-color: #475569; }}

  .card-header {{ display: flex; align-items: flex-start; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .card-actions {{ display: flex; flex-direction: column; align-items: flex-end; gap: 0.2rem;
    flex-shrink: 0; margin-left: auto; padding-top: 1.15rem; }}
  .card-actions > * {{ margin-left: 0; }}
  .card-actions .mark-btn, .card-actions .status-badge, .card-actions .stale-x {{
    font-size: 0.68rem; padding: 0.12rem 0.4rem; white-space: nowrap; }}
  .card-actions .stale-x {{ font-size: 1rem; padding: 0 4px; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}
  .title-block {{ display: flex; flex-direction: column; flex: 1; min-width: 0; }}
  .company-line {{ font-size: 0.75rem; color: #94a3b8; margin-top: 0.1rem; }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}
  .meta-tag.lang-tag {{ background: #7c2d12; color: #fdba74; }}
  .meta-tag.type-tag {{ background: #155e75; color: #67e8f9; }}
  .meta-tag.mode-tag {{ background: #3b1e5f; color: #c4b5fd; }}
  .meta-tag.country-tag {{ background: #1f3d24; color: #86efac; }}

  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; }}
  .share-row {{ display: flex; align-items: center; gap: 0.4rem; margin-top: 0.5rem; padding-top: 0.5rem; border-top: 1px solid #334155; }}
  .share-label {{ font-size: 0.72rem; color: #94a3b8; }}
  .share-link {{ font-size: 0.72rem; padding: 0.2rem 0.55rem; border-radius: 6px; text-decoration: none; font-weight: 500; }}
  .share-link.email {{ background: #1e3a5f; color: #93c5fd; }}
  .share-link.whatsapp {{ background: #064e3b; color: #6ee7b7; }}
  .share-link:hover {{ filter: brightness(1.25); }}
  .files-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; margin: 0.5rem 0; }}
  .files-label {{ font-size: 0.72rem; color: #94a3b8; }}
  .file-link {{ background: #1e3a5f; border: 1px solid #60a5fa55; color: #93c5fd; text-decoration: none; padding: 0.2rem 0.55rem; border-radius: 6px; font-size: 0.72rem; font-weight: 500; }}
  .file-link:hover {{ background: #60a5fa22; color: #bfdbfe; }}
  .file-item {{ display: inline-flex; align-items: stretch; overflow: hidden;
    background: #1e3a5f; border: 1px solid #60a5fa55; border-radius: 6px; }}
  .file-item .file-link {{ border: none; border-radius: 0; background: transparent; }}
  .file-x {{ background: #3f1d1d; border: none; border-left: 1px solid #7f1d1d;
    color: #fca5a5; font-size: 0.85rem; font-weight: 700; line-height: 1;
    cursor: pointer; padding: 0 5px; }}
  .file-x:hover {{ background: #b91c1c; color: #fff; }}
  .mark-row {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.6rem 0; padding-top: 0.6rem; border-top: 1px solid #334155; }}
  .mark-btn {{ background: #334155; border: 1px solid #475569; color: #cbd5e1; padding: 0.25rem 0.6rem; border-radius: 6px; cursor: pointer; font-size: 0.72rem; transition: all 0.15s; }}
  .mark-btn:hover {{ background: #475569; color: #fff; }}
  .mark-btn.pass:hover {{ background: #10b981; border-color: #10b981; color: #0f172a; }}
  .mark-btn.fail:hover {{ background: #ef4444; border-color: #ef4444; color: #0f172a; }}
  .mark-btn.na:hover {{ background: #94a3b8; border-color: #94a3b8; color: #0f172a; }}
  .mark-btn.reset {{ margin-left: auto; }}
  .mark-btn.combine {{ background: #1e3a5f; border-color: #2a7ab5; color: #93c5fd; }}
  .mark-btn.combine:hover {{ background: #2a7ab5; border-color: #2a7ab5; color: #fff; }}
  .mark-btn.combine-del {{ background: #3f1d1d; border-color: #7f1d1d; color: #fca5a5; }}
  .mark-btn.combine-del:hover {{ background: #b91c1c; border-color: #b91c1c; color: #fff; }}
  .mark-btn.cover-generate {{ background: #1f3a2e; border-color: #2f7d5b; color: #86efac; }}
  .mark-btn.cover-generate:hover {{ background: #2f7d5b; border-color: #2f7d5b; color: #fff; }}
  .mark-btn.focus.active {{ background: #f59e0b; border-color: #f59e0b; color: #0f172a; font-weight: bold; }}
  .job-card.focused {{ border: 2px solid #f59e0b; box-shadow: 0 0 14px rgba(245,158,11,0.35); }}
  .job-card.focused .job-title {{ font-weight: 800; }}
  .mark-btn.focus {{ margin-left: auto; }}

  /* Highlight (LLM key concepts) */
  .mark-btn.highlight {{ background: #134e4a; border-color: #0f766e; color: #5eead4; }}
  .mark-btn.highlight:hover {{ background: #0f766e; border-color: #0f766e; color: #fff; }}
  .mark-btn.highlight.active {{ background: #14b8a6; border-color: #14b8a6; color: #0f172a; font-weight: bold; }}
  .job-card.highlighted {{ border: 2px solid #14b8a6; box-shadow: 0 0 12px rgba(20,184,166,0.35); }}
  .job-card.highlighted .job-title {{ font-weight: 800; }}
  .highlight-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; margin: 0.35rem 0; }}
  .highlight-label {{ font-size: 0.7rem; color: #5eead4; font-weight: 600; }}
  .concept-chip {{ font-size: 0.7rem; padding: 0.12rem 0.5rem; border-radius: 999px;
    background: #134e4a; color: #5eead4; border: 1px solid #0f766e; }}
  .inbox-panel {{ margin: 6px 0; padding: 8px 10px; border-radius: 8px; background: #0f1b2e; border: 1px solid #1e3a5f; }}
  .inbox-head {{ display: flex; gap: 8px; align-items: center; margin-bottom: 4px; }}
  .inbox-title {{ font-size: 0.78rem; font-weight: 600; color: #93c5fd; }}
  .inbox-body {{ white-space: pre-wrap; font-family: inherit; font-size: 0.8rem; color: #cbd5e1; margin: 0; }}
  .mark-btn.inbox-scan {{ background: #1e3a5f; border-color: #2a7ab5; color: #93c5fd; }}
  .mark-btn.inbox-scan:hover {{ background: #2a7ab5; border-color: #2a7ab5; color: #fff; }}
  .focus-filter-btn.active {{ background: #f59e0b; border-color: #f59e0b; color: #0f172a; font-weight: bold; }}

  /* Post-application outcome buttons (hidden until the job is applied) */
  .outcome-btns {{ display: none; gap: 0.3rem; }}
  .job-card.is-applied .outcome-btns {{ display: inline-flex; }}
  .mark-btn.interview {{ background: #1e3a5f; border-color: #2a5fa5; color: #93c5fd; }}
  .mark-btn.interview:hover {{ background: #2a5fa5; border-color: #2a5fa5; color: #fff; }}
  .mark-btn.reject {{ background: #3f1d1d; border-color: #7f1d1d; color: #fca5a5; }}
  .mark-btn.reject:hover {{ background: #b91c1c; border-color: #b91c1c; color: #fff; }}
  .mark-btn.nodeal {{ background: #334155; border-color: #475569; color: #cbd5e1; }}
  .mark-btn.nodeal:hover {{ background: #475569; border-color: #475569; color: #fff; }}

  /* Manual stale (×) button + stale cards */
  .stale-x {{ position: absolute; top: 5px; right: 6px; margin-left: 0; z-index: 3;
    background: transparent; border: none; color: #64748b;
    font-size: 1.2rem; line-height: 1; cursor: pointer; padding: 0 3px; }}
  .stale-x:hover {{ color: #ef4444; }}
  .job-card.stale {{ opacity: 0.62; border-left-color: #64748b !important; }}

  /* Tabs */
  .tabs {{ display: flex; gap: 0.5rem; margin: 1.25rem 0 0.5rem; }}
  .topnav {{ position: sticky; top: 0; z-index: 60; display: flex; align-items: center;
    gap: 0.5rem; padding: 0.6rem 0.85rem; margin: 0.5rem 0 1rem;
    background: #0f172ae6; backdrop-filter: blur(6px);
    border: 1px solid #1e293b; border-radius: 10px; }}
  .topnav-brand {{ font-weight: 800; font-size: 1rem; color: #93c5fd; margin-right: auto;
    letter-spacing: 0.3px; }}
  .nav-variant {{ display: inline-flex; align-items: center; gap: 0.35rem;
    font-size: 0.75rem; color: #94a3b8; }}
  .nav-variant .filter-select {{ padding: 0.2rem 0.4rem; font-size: 0.75rem; }}
  .tab-btn {{ background: #1e293b; border: 1px solid #334155; color: #cbd5e1;
    padding: 0.45rem 1.1rem; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
  .tab-btn:hover {{ border-color: #2a7ab5; color: #fff; }}
  .tab-btn.active {{ background: #2a7ab5; border-color: #2a7ab5; color: #fff; font-weight: 600; }}

  /* Stats tab */
  .stats-panel {{ margin-top: 1rem; }}
  .stats-panel h2 {{ font-size: 1.1rem; margin-bottom: 0.75rem; }}
  .report-hero {{ text-align: center; padding: 1.25rem 1rem 1.5rem; margin-bottom: 1.25rem;
    background: radial-gradient(circle at 50% 0%, #10b98122, transparent 70%);
    border: 1px solid #1e293b; border-radius: 14px; }}
  .report-hero-num {{ font-size: 5rem; font-weight: 900; line-height: 1;
    color: #10b981; text-shadow: 0 0 32px #10b98155; }}
  .report-hero-label {{ font-size: 0.9rem; letter-spacing: 0.25em; text-transform: uppercase;
    color: #94a3b8; margin-top: 0.5rem; }}
  .report-hero-sub {{ display: flex; gap: 0.6rem; justify-content: center; flex-wrap: wrap;
    margin-top: 1rem; }}
  .rh-chip {{ font-size: 0.78rem; color: #cbd5e1; background: #1e293b;
    border: 1px solid #334155; border-radius: 999px; padding: 0.25rem 0.8rem; }}
  .rh-chip b {{ color: #10b981; }}
  .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
  @media (max-width: 760px) {{ .stats-grid {{ grid-template-columns: 1fr; }} }}
  .stats-table {{ width: 100%; border-collapse: collapse; background: #1e293b;
    border-radius: 10px; overflow: hidden; }}
  .stats-table th, .stats-table td {{ text-align: left; padding: 0.5rem 0.85rem;
    border-bottom: 1px solid #0f172a; font-size: 0.85rem; }}
  .stats-table th {{ color: #94a3b8; font-weight: 600; }}
  .stats-table td.num, .stats-table th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .stats-table .muted {{ color: #64748b; }}

  /* Celebration counter on each mark */
  .celebrate {{ position: fixed; inset: 0; display: flex; align-items: center;
    justify-content: center; pointer-events: none; opacity: 0;
    transition: opacity 0.25s; z-index: 9999; }}
  .celebrate.show {{ opacity: 1; }}
  .celebrate-card {{ background: #0f172af2; border: 2px solid #334155;
    border-radius: 20px; padding: 1.5rem 2.75rem; text-align: center;
    box-shadow: 0 20px 60px #000a; animation: popIn 0.25s ease-out; }}
  .celebrate-card.win {{ border-color: #10b981;
    box-shadow: 0 0 44px #10b98166, 0 20px 60px #000a; }}
  .celebrate-num {{ font-size: 4.75rem; font-weight: 900; line-height: 1;
    color: #10b981; }}
  .celebrate-label {{ font-size: 1rem; letter-spacing: 0.25em; text-transform: uppercase;
    color: #94a3b8; margin-top: 0.4rem; }}
  .celebrate-sub {{ font-size: 0.85rem; color: #64748b; margin-top: 0.5rem; }}
  @keyframes popIn {{ from {{ transform: scale(0.7); opacity: 0; }} to {{ transform: scale(1); opacity: 1; }} }}
  .confetti-piece {{ position: fixed; top: -20px; width: 9px; height: 14px;
    z-index: 10000; pointer-events: none; border-radius: 2px;
    animation: confettiFall linear forwards; }}
  @keyframes confettiFall {{ to {{ transform: translateY(105vh) rotate(720deg); opacity: 0; }} }}
  .lang-flag {{ margin: 0.5rem 0 0.1rem; }}
  .lang-flag-tag {{ font-size: 0.72rem; padding: 0.2rem 0.55rem; border-radius: 6px; font-weight: 600; display: inline-block; }}
  .lang-flag-tag.req {{ background: #7c2d12; color: #fdba74; }}
  .lang-flag-tag.none {{ background: #064e3b; color: #6ee7b7; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: #60a5fa22; }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: #93c5fd; }}
  .full-desc {{ font-size: 0.8rem; color: #cbd5e1; line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
</head>
<body>

<h1>ApplyPilot Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+)</p>

{todos_html}

{study_html}

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
  <div class="stat-card stat-applied clickable-stat" onclick="quickStatus('applied')" title="Show applied jobs">
    <div class="stat-num">{_applied_only}</div><div class="stat-label">Applied</div></div>
</div>

<nav class="topnav">
  <span class="topnav-brand">ApplyPilot</span>
  <button class="tab-btn active" data-tab="jobs" onclick="showTab('jobs', event)">Jobs</button>
  <button class="tab-btn" data-tab="stats" onclick="showTab('stats', event)">Report</button>
  <span class="nav-variant">
    <label for="resume-variant">Resume:</label>
    <select id="resume-variant" class="filter-select" onchange="saveVariant(this.value)">{variant_options}</select>
  </span>
</nav>

<div id="tab-jobs">
<div class="filters">
  <button class="filter-btn focus-filter-btn" onclick="filterFocused(event)" title="Show only focused jobs">★ Focus only</button>
  <span class="filter-label" style="margin-left:1rem">Score:</span>
  <button class="filter-btn score-btn active" onclick="filterScore(-1)">All</button>
  <button class="filter-btn score-btn" onclick="filterScore(5)">5+</button>
  <button class="filter-btn score-btn" onclick="filterScore(7)">7+ Strong</button>
  <button class="filter-btn score-btn" onclick="filterScore(8)">8+ Excellent</button>
  <button class="filter-btn score-btn" onclick="filterScore(9)">9+ Perfect</button>
  
  <span class="filter-label" style="margin-left:1rem">Status:</span>
  <button class="filter-btn status-btn active" onclick="filterStatus('not_applied', event)">Not Applied</button>
  <button class="filter-btn status-btn" onclick="filterStatus('applied', event)">Applied</button>
  <button class="filter-btn status-btn" onclick="filterStatus('interviewing', event)">Interviewing</button>
  <button class="filter-btn status-btn" onclick="filterStatus('rejected', event)">Rejected</button>
  <button class="filter-btn status-btn" onclick="filterStatus('no_deal', event)">No deal</button>
  <button class="filter-btn status-btn" onclick="filterStatus('failed', event)">Failed</button>
  <button class="filter-btn status-btn" onclick="filterStatus('not_available', event)">Not Available</button>
  <button class="filter-btn status-btn" onclick="filterStatus('not_interested', event)">Not Interested</button>
  <button class="filter-btn status-btn" onclick="filterStatus('expired', event)">Expired</button>
  <button class="filter-btn status-btn" onclick="filterStatus('stale', event)">Stale</button>
  <button class="filter-btn status-btn" onclick="filterStatus('all', event)">All</button>
  
  <span class="filter-label" style="margin-left:1rem">Language:</span>
  <select id="lang-select" class="filter-select" onchange="filterLang(this.value)">{lang_options}</select>
  
  <span class="filter-label" style="margin-left:1rem">Type:</span>
  {type_buttons}
  
  <span class="filter-label" style="margin-left:1rem">Work mode:</span>
  {mode_buttons}
  
  <span class="filter-label" style="margin-left:1rem">Country:</span>
  <select id="country-select" class="filter-select" onchange="filterCountry(this.value)">{country_options}</select>
  <button class="filter-btn" onclick="resetCountry()">Reset</button>
  
  <span class="filter-label" style="margin-left:1rem">Company:</span>
  <input id="company-search" class="filter-select" list="company-list" placeholder="Any company" autocomplete="off" oninput="filterCompany(this.value)">
  <datalist id="company-list">{company_datalist}</datalist>
  <button class="filter-btn" onclick="resetCompany()">Reset</button>
  
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" id="search-input" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
  <button class="filter-btn" style="margin-left:1rem" onclick="resetAllFilters()">Reset all filters</button>
</div>

<div class="score-section">
  <details class="score-dist">
    <summary>Score Distribution</summary>
    {score_bars}
  </details>
  <details class="sites-section">
    <summary>By Source</summary>
    {site_rows}
  </details>
</div>

<div id="job-count" class="job-count"></div>

{job_sections}
</div>

<div id="tab-stats" style="display:none">
{stats_html}
</div>

{answers_html}

<div id="job-modal" class="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal-box">
    <button class="modal-close" onclick="closeModal()">×</button>
    <div id="modal-body"></div>
  </div>
</div>

<div id="reason-modal" class="modal-overlay" onclick="if(event.target===this)closeReason()">
  <div class="modal-box reason-box">
    <h3 class="modal-title">Why not interested?</h3>
    <div class="reason-options">
      <button class="reason-btn" onclick="submitReason('Currency')">Currency</button>
      <button class="reason-btn" onclick="submitReason('Location not desired')">Location not desired</button>
      <button class="reason-btn" onclick="submitReason('Low salary')">Low salary</button>
      <button class="reason-btn" onclick="submitReason('Not a fit')">Not a fit</button>
      <button class="reason-btn" onclick="submitReason('Language required')">Language required</button>
      <button class="reason-btn" onclick="submitReason('No longer accepting applications')">No longer accepting applications</button>
    </div>
    <div class="reason-other-row">
      <input id="reason-other" class="todo-input" placeholder="Other reason (type and press Enter)..."
             onkeydown="if(event.key==='Enter')submitReason(this.value)">
      <button class="todo-add-btn" onclick="submitReason(document.getElementById('reason-other').value)">Save</button>
    </div>
    <button class="reason-cancel" onclick="closeReason()">Cancel</button>
  </div>
</div>

<script>
let minScore = -1;
let searchText = '';
let statusFilter = 'not_applied';
let langFilter = 'any';
let typeFilter = 'any';
let countryFilter = 'any';
let workModeFilter = 'any';
let companyFilter = '';
let focusFilter = false;
let exactScore = null;
let siteFilter = '';

// Persist the active filter set (and tab) so a reload -- e.g. right after
// generating a document -- keeps you exactly where you were.
const FILTER_KEY = 'applypilot.filters.v1';
const TAB_KEY = 'applypilot.tab.v1';
const VARIANT_KEY = 'applypilot.variant.v1';

function currentVariant() {{
  const el = document.getElementById('resume-variant');
  return el ? el.value : '';
}}
function saveVariant(v) {{
  try {{ localStorage.setItem(VARIANT_KEY, v || ''); }} catch (e) {{}}
}}

function saveFilters() {{
  try {{
    localStorage.setItem(FILTER_KEY, JSON.stringify({{
      minScore, searchText, statusFilter, langFilter, typeFilter,
      countryFilter, workModeFilter, companyFilter, focusFilter,
      exactScore, siteFilter,
    }}));
  }} catch (e) {{}}
}}

function restoreFilters() {{
  let s = null;
  try {{ s = JSON.parse(localStorage.getItem(FILTER_KEY) || 'null'); }} catch (e) {{}}
  if (!s) return;
  if (typeof s.minScore === 'number') minScore = s.minScore;
  if (typeof s.searchText === 'string') searchText = s.searchText;
  if (typeof s.statusFilter === 'string') statusFilter = s.statusFilter;
  if (typeof s.langFilter === 'string') langFilter = s.langFilter;
  if (typeof s.typeFilter === 'string') typeFilter = s.typeFilter;
  if (typeof s.countryFilter === 'string') countryFilter = s.countryFilter;
  if (typeof s.workModeFilter === 'string') workModeFilter = s.workModeFilter;
  if (typeof s.companyFilter === 'string') companyFilter = s.companyFilter;
  focusFilter = !!s.focusFilter;
  exactScore = (typeof s.exactScore === 'number') ? s.exactScore : null;
  if (typeof s.siteFilter === 'string') siteFilter = s.siteFilter;
}}

function restoreFilterUI() {{
  document.querySelectorAll('.score-btn').forEach(b =>
    b.classList.toggle('active', (b.getAttribute('onclick') || '').includes('filterScore(' + minScore + ')')));
  document.querySelectorAll('.status-btn').forEach(b =>
    b.classList.toggle('active', (b.getAttribute('onclick') || '').includes("filterStatus('" + statusFilter + "'")));
  document.querySelectorAll('.type-btn').forEach(b =>
    b.classList.toggle('active', (b.getAttribute('onclick') || '').includes("filterType('" + typeFilter + "'")));
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', (b.getAttribute('onclick') || '').includes("filterWorkMode('" + workModeFilter + "'")));
  document.querySelectorAll('.focus-filter-btn').forEach(b => b.classList.toggle('active', focusFilter));
  document.querySelectorAll('.score-row.clickable').forEach(r =>
    r.classList.toggle('active', exactScore !== null && parseInt(r.dataset.score, 10) === exactScore));
  document.querySelectorAll('.site-row.clickable').forEach(r =>
    r.classList.toggle('active', !!siteFilter && (r.dataset.site || '').toLowerCase() === siteFilter.toLowerCase()));
  const ls = document.getElementById('lang-select'); if (ls) ls.value = langFilter;
  const cs = document.getElementById('country-select'); if (cs) cs.value = countryFilter;
  const si = document.getElementById('search-input'); if (si) si.value = searchText;
  const comp = document.getElementById('company-search'); if (comp) comp.value = companyFilter;
}}

function filterScore(min) {{
  minScore = min;
  exactScore = null;
  document.querySelectorAll('.score-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.score-row.clickable').forEach(r => r.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterExactScore(val) {{
  const s = parseInt(val, 10);
  exactScore = (exactScore === s) ? null : s;
  minScore = -1;
  document.querySelectorAll('.score-btn').forEach(b => b.classList.toggle('active', b.textContent.trim() === 'All'));
  document.querySelectorAll('.score-row.clickable').forEach(r =>
    r.classList.toggle('active', exactScore !== null && parseInt(r.dataset.score, 10) === exactScore));
  applyFilters();
}}

function filterSite(val) {{
  const v = (val || '').trim();
  siteFilter = (siteFilter && siteFilter.toLowerCase() === v.toLowerCase()) ? '' : v;
  document.querySelectorAll('.site-row.clickable').forEach(r =>
    r.classList.toggle('active', !!siteFilter && (r.dataset.site || '').toLowerCase() === siteFilter.toLowerCase()));
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function filterStatus(val, event) {{
  statusFilter = val;
  document.querySelectorAll('.status-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterFocused(event) {{
  focusFilter = !focusFilter;
  if (event && event.target) event.target.classList.toggle('active', focusFilter);
  applyFilters();
}}

const STATUS_BADGES = {{
  applied: ['Applied', '#10b981'],
  success: ['Applied', '#10b981'],
  interviewing: ['Interviewing', '#3b82f6'],
  offer: ['Offer', '#22c55e'],
  rejected: ['Rejected', '#ef4444'],
  no_deal: ['No deal', '#64748b'],
  failed: ['Failed', '#ef4444'],
  not_available: ['Not Available', '#94a3b8'],
  not_interested: ['Not Interested', '#64748b'],
  expired: ['Expired', '#f97316'],
}};

function setStatusBadge(card, status) {{
  const badge = card.querySelector('.status-badge');
  if (!badge) return;
  const m = STATUS_BADGES[status];
  if (!m) {{ badge.style.display = 'none'; badge.textContent = ''; return; }}
  badge.textContent = m[0];
  badge.style.background = m[1];
  badge.style.color = '#0f172a';
  badge.style.fontWeight = 'bold';
  badge.style.display = '';
}}

async function postMark(card, status, reason) {{
  const url = card.dataset.url;
  if (!url) return false;
  try {{
    const res = await fetch('/mark', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: url, status: status, reason: reason || ''}})
    }});
    const data = await res.json();
    if (data.ok) {{
      const newStatus = (status === 'reset' ? '' : status);
      card.dataset.applyStatus = newStatus;
      const appliedNow = ['applied', 'success', 'interviewing', 'offer', 'rejected', 'no_deal'].includes(newStatus);
      card.classList.toggle('is-applied', appliedNow);
      // Applying (or logging an outcome) auto-clears Focus.
      if (appliedNow) {{
        card.dataset.focused = '0';
        card.classList.remove('focused');
        const fb = card.querySelector('.mark-btn.focus');
        if (fb) {{ fb.classList.remove('active'); fb.textContent = 'Focus'; }}
      }}
      setStatusBadge(card, newStatus);
      const badge = card.querySelector('.status-badge');
      if (badge) badge.title = reason || '';
      card.style.transition = 'box-shadow 0.3s';
      card.style.boxShadow = '0 0 0 2px #10b98188';
      setTimeout(() => {{ card.style.boxShadow = ''; }}, 700);
      // Re-apply the CURRENT filters (they stay selected) so the job moves out
      // of the view if it no longer matches, without a page reload.
      applyFilters();
      if (newStatus) celebrate(newStatus);
      return true;
    }}
    alert('Failed to update: ' + (data.error || 'unknown'));
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
  }}
  return false;
}}

function _countStatuses() {{
  // "Applied" counts only jobs you applied to and haven't progressed past;
  // outcomes (interviewing/offer/rejected/no-deal) count as "other".
  const appliedSet = ['applied', 'success'];
  let applied = 0, total = 0;
  document.querySelectorAll('.job-card').forEach(c => {{
    total++;
    if (appliedSet.includes(c.dataset.applyStatus || '')) applied++;
  }});
  return {{ applied: applied, other: total - applied }};
}}

function _confetti() {{
  const colors = ['#10b981', '#f59e0b', '#60a5fa', '#ef4444', '#a78bfa', '#34d399'];
  for (let i = 0; i < 44; i++) {{
    const p = document.createElement('div');
    p.className = 'confetti-piece';
    p.style.left = (Math.random() * 100) + 'vw';
    p.style.background = colors[Math.floor(Math.random() * colors.length)];
    p.style.animationDelay = (Math.random() * 0.4) + 's';
    p.style.animationDuration = (1.3 + Math.random() * 1.2) + 's';
    document.body.appendChild(p);
    setTimeout(() => p.remove(), 3200);
  }}
}}

function celebrate(kind) {{
  const c = _countStatuses();
  let host = document.getElementById('celebrate');
  if (!host) {{
    host = document.createElement('div');
    host.id = 'celebrate';
    host.className = 'celebrate';
    document.body.appendChild(host);
  }}
  const win = (kind === 'applied' || kind === 'success') ? ' win' : '';
  host.innerHTML = '<div class="celebrate-card' + win + '">' +
    '<div class="celebrate-num">' + c.applied + '</div>' +
    '<div class="celebrate-label">Applied</div>' +
    '<div class="celebrate-sub">vs ' + c.other + ' other' + (c.other === 1 ? '' : 's') + '</div>' +
    '</div>';
  host.classList.add('show');
  if (win) _confetti();
  clearTimeout(window._celebrateTimer);
  window._celebrateTimer = setTimeout(() => host.classList.remove('show'), 2200);
}}

async function markJob(btn, status) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  if (status === 'not_interested') {{ openReasonModal(card); return; }}
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';
  await postMark(card, status, '');
  btn.disabled = false;
  btn.textContent = original;
}}

async function combineResume(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const extra = window.prompt('Optional instructions to guide the resume generation (blank = none):', '') || '';
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating...';
  try {{
    const data = await postJSON('/combine', {{url: url, extra: extra, variant: currentVariant()}});
    if (data.ok) {{
      if (data.fallback) alert('No LaTeX base found - used the default PDF pipeline.');
      btn.textContent = 'Generated';
      refreshCard(card);
    }} else {{
      alert('Generate failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function deleteCombined(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  if (!window.confirm('Delete the generated resume (.tex and .pdf) for this job?')) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {{
    const data = await postJSON('/combine/delete', {{url: url}});
    if (data.ok) {{
      refreshCard(card);
    }} else {{
      alert('Delete failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function generateCoverLetter(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const extra = window.prompt('Optional instructions to guide this cover letter (blank = none):', '') || '';
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating...';
  try {{
    const data = await postJSON('/cover', {{url: url, extra: extra, variant: currentVariant()}});
    if (data.ok) {{
      btn.textContent = 'Generated';
      refreshCard(card);
    }} else {{
      alert('Cover letter failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function deleteCoverLetter(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  if (!window.confirm('Delete the generated cover letter for this job?')) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {{
    const data = await postJSON('/cover/delete', {{url: url}});
    if (data.ok) {{
      refreshCard(card);
    }} else {{
      alert('Delete failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function deleteJobFile(btn, kind, label) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  if (!window.confirm('Permanently delete "' + label + '"?\\n\\nThis removes the file from disk and CANNOT be undone.')) return;
  btn.disabled = true;
  try {{
    const data = await postJSON('/file/delete', {{url: url, kind: kind}});
    if (data.ok) {{
      refreshCard(card);
    }} else {{
      alert('Delete failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
  }}
}}

async function scanInbox(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {{
    const data = await postJSON('/inbox/scan', {{url: url}});
    if (data.ok) {{
      refreshCard(card);
    }} else {{
      alert('Inbox scan failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function freeInbox(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  if (!window.confirm('Clear the cached inbox insight for this job?')) return;
  try {{
    const data = await postJSON('/inbox/free', {{url: url}});
    if (data.ok) {{
      refreshCard(card);
    }} else {{
      alert('Failed: ' + (data.error || 'unknown'));
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
  }}
}}

async function toggleFocus(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const next = card.dataset.focused === '1' ? 0 : 1;
  try {{
    const data = await postJSON('/focus', {{url: url, focused: !!next}});
    if (data.ok) {{
      card.dataset.focused = next ? '1' : '0';
      card.classList.toggle('focused', !!next);
      btn.classList.toggle('active', !!next);
      btn.textContent = next ? 'Unfocus' : 'Focus';
      applyFilters();
    }}
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

async function toggleStale(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const next = card.dataset.stale === '1' ? 0 : 1;
  if (next && !window.confirm('Move this job to Stale? It stays in your data and exports, but leaves the main list.')) return;
  btn.disabled = true;
  try {{
    const data = await postJSON('/stale', {{url: url, stale: !!next}});
    if (data.ok) {{
      card.dataset.stale = next ? '1' : '0';
      card.classList.toggle('stale', !!next);
      btn.textContent = next ? '↩' : '×';
      btn.title = next ? 'Restore from Stale' : 'Move to Stale';
      applyFilters();
    }} else {{
      alert('Failed: ' + (data.error || 'unknown'));
    }}
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
  finally {{ btn.disabled = false; }}
}}

async function toggleHighlight(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  const next = card.dataset.highlighted === '1' ? 0 : 1;
  const original = btn.textContent;
  btn.disabled = true;
  if (next) btn.textContent = 'Analyzing...';
  try {{
    const data = await postJSON('/highlight', {{url: url, highlighted: !!next}});
    if (data.ok) {{
      if (next && data.concepts && data.concepts.length === 0) {{
        alert('No concepts extracted (no job description on file).');
      }}
      refreshCard(card);
    }} else {{
      alert('Highlight failed: ' + (data.error || 'unknown'));
      btn.disabled = false;
      btn.textContent = original;
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
    btn.disabled = false;
    btn.textContent = original;
  }}
}}

async function refreshCard(card) {{
  // Update just this card's fragments instead of reloading the whole dashboard.
  if (!card) return;
  const url = card.dataset.url;
  if (!url) return;
  try {{
    const res = await fetch('/card-state?url=' + encodeURIComponent(url));
    const d = await res.json();
    if (!d.ok) return;
    const fs = card.querySelector('.files-slot'); if (fs) fs.innerHTML = d.files_html;
    const cs = card.querySelector('.concepts-slot'); if (cs) cs.innerHTML = d.concepts_html;
    const as = card.querySelector('.actions-slot'); if (as) as.innerHTML = d.actions_html;
    const is = card.querySelector('.inbox-slot'); if (is) is.innerHTML = d.inbox_html;
    card.dataset.highlighted = d.highlighted ? '1' : '0';
    card.classList.toggle('highlighted', !!d.highlighted);
    const hb = card.querySelector('.mark-btn.highlight');
    if (hb) {{
      hb.classList.toggle('active', !!d.highlighted);
      hb.textContent = d.highlighted ? 'Remove Highlight' : 'Generate Highlight';
      hb.disabled = false;
    }}
  }} catch (e) {{}}
}}

function showTab(name, event) {{
  const jobs = document.getElementById('tab-jobs');
  const stats = document.getElementById('tab-stats');
  if (jobs) jobs.style.display = (name === 'jobs') ? '' : 'none';
  if (stats) stats.style.display = (name === 'stats') ? '' : 'none';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  try {{ localStorage.setItem(TAB_KEY, name); }} catch (e) {{}}
}}

function quickStatus(val) {{
  // Jump to the Jobs tab showing a single status (e.g. Applied card click).
  showTab('jobs');
  statusFilter = val;
  focusFilter = false; siteFilter = ''; exactScore = null; minScore = -1;
  document.querySelectorAll('.focus-filter-btn, .score-row.clickable, .site-row.clickable')
    .forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.score-btn').forEach(b =>
    b.classList.toggle('active', b.textContent.trim() === 'All'));
  document.querySelectorAll('.status-btn').forEach(b => {{
    const oc = b.getAttribute('onclick') || '';
    b.classList.toggle('active', oc.includes("filterStatus('" + val + "')"));
  }});
  applyFilters();
}}

let _reasonCard = null;
function openReasonModal(card) {{
  _reasonCard = card;
  const m = document.getElementById('reason-modal');
  m.style.display = 'flex';
  const inp = document.getElementById('reason-other');
  if (inp) {{ inp.value = ''; setTimeout(() => inp.focus(), 50); }}
}}
function closeReason() {{
  document.getElementById('reason-modal').style.display = 'none';
  _reasonCard = null;
}}
function submitReason(reason) {{
  const card = _reasonCard;
  closeReason();
  if (card) postMark(card, 'not_interested', (reason || '').trim() || 'Not interested');
}}

function escapeHtml(s) {{
  return (s || '').replace(/[&<>"']/g, c => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}})[c]);
}}

async function postJSON(path, payload) {{
  const res = await fetch(path, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});
  return await res.json();
}}

function _todoLi(item) {{
  const li = document.createElement('li');
  li.className = 'todo-item';
  li.dataset.id = item.id;
  li.dataset.tag = item.tag || '';
  const body = item.url
    ? '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noopener">' + escapeHtml(item.text) + '</a>'
    : '<span>' + escapeHtml(item.text) + '</span>';
  const tagHtml = item.tag ? '<span class="todo-tag">' + escapeHtml(item.tag) + '</span>' : '';
  li.innerHTML = '<input type="checkbox" onchange="toggleTodo(this)">' +
    '<div class="todo-main">' + body + tagHtml + '</div>' +
    '<button class="todo-del" title="Delete" onclick="deleteTodo(this)">×</button>';
  return li;
}}

function _addTodoTagOption(tag) {{
  const sel = document.getElementById('todo-tag-filter');
  if (sel && ![...sel.options].some(o => o.value === tag)) {{
    const o = document.createElement('option'); o.value = tag; o.textContent = tag; sel.appendChild(o);
  }}
  const dl = document.getElementById('todo-tag-list');
  if (dl && ![...dl.options].some(o => o.value === tag)) {{
    const o = document.createElement('option'); o.value = tag; dl.appendChild(o);
  }}
}}

function filterTodos(val) {{
  const want = val || '__all';
  document.querySelectorAll('.todo-item').forEach(li => {{
    const tag = li.dataset.tag || '';
    li.style.display = (want === '__all' || tag === want) ? '' : 'none';
  }});
}}

async function addTodo() {{
  const t = document.getElementById('todo-text');
  const g = document.getElementById('todo-tag');
  const u = document.getElementById('todo-url');
  const text = (t.value || '').trim();
  if (!text) return;
  const tag = (g ? g.value : '').trim();
  try {{
    const data = await postJSON('/todo/add', {{text: text, url: (u.value || '').trim(), tag: tag}});
    if (data.ok && data.item) {{
      document.querySelector('.todo-list').appendChild(_todoLi(data.item));
      if (data.item.tag) _addTodoTagOption(data.item.tag);
      t.value = ''; if (g) g.value = ''; u.value = '';
      const sel = document.getElementById('todo-tag-filter');
      filterTodos(sel ? sel.value : '__all');
    }} else {{ alert('Failed: ' + (data.error || 'unknown')); }}
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

async function toggleTodo(el) {{
  const li = el.closest('.todo-item');
  try {{
    const data = await postJSON('/todo/toggle', {{id: li.dataset.id}});
    if (data.ok) li.classList.toggle('done');
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

async function deleteTodo(el) {{
  const li = el.closest('.todo-item');
  try {{
    const data = await postJSON('/todo/delete', {{id: li.dataset.id}});
    if (data.ok) li.remove();
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

async function saveNote(input) {{
  const card = input.closest('.job-card');
  const url = card ? card.dataset.url : '';
  if (!url) return;
  try {{
    const res = await fetch('/note', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url: url, note: input.value}})
    }});
    const data = await res.json();
    if (data.ok) {{
      input.classList.add('saved');
      setTimeout(() => input.classList.remove('saved'), 800);
    }} else {{
      alert('Failed: ' + (data.error || 'unknown'));
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
  }}
}}

async function saveStudy(input, btn) {{
  const card = input.closest('.job-card');
  const url = card ? card.dataset.url : '';
  if (!url) return;
  const hint = input.closest('.study-details')
    ? input.closest('.study-details').querySelector('.study-hint') : null;
  if (btn) {{ btn.disabled = true; btn.textContent = 'Saving...'; }}
  try {{
    const data = await postJSON('/study', {{url: url, note: input.value}});
    if (data.ok) {{
      const det = input.closest('.study-details');
      if (det) det.classList.toggle('in-study', !!(input.value || '').trim());
      if (hint) {{ hint.textContent = 'Saved'; setTimeout(() => {{ hint.textContent = ''; }}, 1500); }}
    }} else {{
      alert('Failed: ' + (data.error || 'unknown'));
    }}
  }} catch (e) {{
    alert('Could not reach the dashboard server.\\nStart it with: applypilot dashboard');
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = 'Save'; }}
  }}
}}

function openJobModal(btn) {{
  const card = btn.closest('.job-card');
  if (!card) return;
  const titleEl = card.querySelector('.job-title');
  const meta = card.querySelector('.meta-row');
  const reason = card.querySelector('.reasoning-row');
  const files = card.querySelector('.files-row');
  const full = card.querySelector('.full-desc');
  const titleHtml = titleEl
    ? '<a href="' + titleEl.href + '" target="_blank">' + titleEl.textContent + '</a>'
    : 'Job';
  document.getElementById('modal-body').innerHTML =
    '<h3 class="modal-title">' + titleHtml + '</h3>' +
    (meta ? '<div class="modal-meta">' + meta.innerHTML + '</div>' : '') +
    (reason ? '<p class="modal-reason">' + reason.innerHTML + '</p>' : '') +
    (files ? files.outerHTML : '') +
    '<div class="modal-desc">' + (full ? full.innerHTML : 'No full description available.') + '</div>';
  document.getElementById('job-modal').style.display = 'flex';
}}

function closeModal() {{
  document.getElementById('job-modal').style.display = 'none';
}}

function _answerDetails(item) {{
  const d = document.createElement('details');
  d.className = 'answer-item';
  d.dataset.id = item.id;
  d.innerHTML = '<summary>' + escapeHtml(item.question) + '</summary>' +
    '<div class="answer-text">' + escapeHtml(item.answer).replace(/\\n/g, '<br>') + '</div>' +
    '<button class="answer-del" title="Delete" onclick="deleteAnswer(this)">×</button>';
  return d;
}}

async function addAnswer() {{
  const q = (document.getElementById('answer-q').value || '').trim();
  const a = (document.getElementById('answer-a').value || '').trim();
  if (!q || !a) {{ alert('Question and answer are required.'); return; }}
  try {{
    const data = await postJSON('/answer/add', {{question: q, answer: a}});
    if (data.ok && data.item) {{
      const wrap = document.querySelector('.answers-panel');
      const addWrap = wrap.querySelector('.answer-add-wrap');
      wrap.insertBefore(_answerDetails(data.item), addWrap);
      document.getElementById('answer-q').value = '';
      document.getElementById('answer-a').value = '';
    }} else {{ alert('Failed: ' + (data.error || 'unknown')); }}
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

async function deleteAnswer(el) {{
  const d = el.closest('.answer-item');
  try {{
    const data = await postJSON('/answer/delete', {{id: d.dataset.id}});
    if (data.ok) d.remove();
  }} catch (e) {{ alert('Could not reach the dashboard server.'); }}
}}

function filterLang(val) {{
  langFilter = val;
  applyFilters();
}}

function filterType(val, event) {{
  typeFilter = val;
  document.querySelectorAll('.type-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterWorkMode(val, event) {{
  workModeFilter = val;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterCountry(val) {{
  countryFilter = val;
  applyFilters();
}}

function resetCountry() {{
  countryFilter = 'any';
  const sel = document.getElementById('country-select');
  if (sel) sel.value = 'any';
  applyFilters();
}}

function filterCompany(val) {{
  companyFilter = (val || '').trim().toLowerCase();
  applyFilters();
}}

function resetCompany() {{
  companyFilter = '';
  const el = document.getElementById('company-search');
  if (el) el.value = '';
  applyFilters();
}}

function resetAllFilters() {{
  minScore = -1; searchText = ''; statusFilter = 'not_applied';
  langFilter = 'any'; typeFilter = 'any'; countryFilter = 'any'; workModeFilter = 'any'; companyFilter = ''; focusFilter = false;
  exactScore = null; siteFilter = '';
  document.querySelectorAll('.focus-filter-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.score-row.clickable, .site-row.clickable').forEach(r => r.classList.remove('active'));
  document.querySelectorAll('.score-btn').forEach(b => b.classList.toggle('active', b.textContent.trim() === 'All'));
  document.querySelectorAll('.status-btn').forEach(b => b.classList.toggle('active', b.textContent.trim() === 'Not Applied'));
  document.querySelectorAll('.type-btn').forEach(b => b.classList.toggle('active', b.textContent.trim() === 'Any'));
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.textContent.trim() === 'Any'));
  const ls = document.getElementById('lang-select'); if (ls) ls.value = 'any';
  const cs = document.getElementById('country-select'); if (cs) cs.value = 'any';
  const comp = document.getElementById('company-search'); if (comp) comp.value = '';
  const si = document.querySelector('.search-input'); if (si) si.value = '';
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const applyStatus = card.dataset.applyStatus;
    const lang = card.dataset.language || 'none';
    const type = card.dataset.employmentType || '';
    const country = card.dataset.country || '';
    const company = card.dataset.company || '';
    const mode = card.dataset.workMode || '';
    const scoreMatch = exactScore !== null ? (score === exactScore) : (minScore < 0 || score >= minScore);
    const textMatch = !searchText || text.includes(searchText);
    
    const stale = card.dataset.stale === '1';
    let statusMatch = true;
    const terminal = ['applied', 'success', 'interviewing', 'offer', 'rejected', 'no_deal', 'failed', 'not_available', 'not_interested', 'expired'];
    if (statusFilter === 'not_applied') {{
      statusMatch = !terminal.includes(applyStatus);
    }} else if (statusFilter === 'applied') {{
      statusMatch = (applyStatus === 'success' || applyStatus === 'applied');
    }} else if (statusFilter === 'interviewing') {{
      statusMatch = (applyStatus === 'interviewing');
    }} else if (statusFilter === 'rejected') {{
      statusMatch = (applyStatus === 'rejected');
    }} else if (statusFilter === 'no_deal') {{
      statusMatch = (applyStatus === 'no_deal');
    }} else if (statusFilter === 'failed') {{
      statusMatch = (applyStatus === 'failed');
    }} else if (statusFilter === 'not_available') {{
      statusMatch = (applyStatus === 'not_available');
    }} else if (statusFilter === 'not_interested') {{
      statusMatch = (applyStatus === 'not_interested');
    }} else if (statusFilter === 'expired') {{
      statusMatch = (applyStatus === 'expired');
    }} else if (statusFilter === 'stale') {{
      statusMatch = stale;
    }}
    // Stale jobs are hidden everywhere except the dedicated Stale filter.
    if (statusFilter !== 'stale' && stale) statusMatch = false;

    const langMatch = langFilter === 'any' || (langFilter === 'none' ? lang === 'none' : lang === langFilter);
    const typeMatch = typeFilter === 'any' || type === typeFilter;
    const countryMatch = countryFilter === 'any' || country === countryFilter;
    const modeMatch = workModeFilter === 'any' || mode === workModeFilter;
    const companyMatch = !companyFilter || company.includes(companyFilter);
    const focusMatch = !focusFilter || card.dataset.focused === '1';
    const site = (card.dataset.site || '').toLowerCase();
    const siteMatch = !siteFilter || site === siteFilter.toLowerCase();

    if (scoreMatch && textMatch && statusMatch && langMatch && typeMatch && countryMatch && modeMatch && companyMatch && focusMatch && siteMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
  saveFilters();
}}

restoreFilters();
restoreFilterUI();
try {{
  const _v = localStorage.getItem(VARIANT_KEY) || '';
  const _el = document.getElementById('resume-variant');
  if (_el && _v) _el.value = _v;
}} catch (e) {{}}
try {{ if (localStorage.getItem(TAB_KEY) === 'stats') showTab('stats'); }} catch (e) {{}}
applyFilters();
</script>

</body>
</html>"""

    return html


def generate_dashboard(output_path: str | None = None) -> str:
    """Write the HTML dashboard to disk and return its path.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"
    html = render_dashboard_html()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")


def serve_dashboard(port: int = 8765, open_browser: bool = True) -> None:
    """Serve the interactive dashboard on localhost.

    Unlike the static file, this runs a tiny HTTP server so the per-job
    "Applied / Failed / Not Available / Not Interested / Reset" buttons can
    write back to the database. Blocks until Ctrl+C.
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from applypilot.database import (
        MANUAL_STATUSES, set_job_status, set_job_note, set_job_study, set_job_focus,
        set_job_stale, set_job_highlight, get_connection,
    )

    allowed = set(MANUAL_STATUSES) | {"reset"}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal clean
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict) -> None:
            self._send(200, _json.dumps(obj).encode("utf-8"), "application/json")

        def _serve_job_file(self, query: str) -> None:
            params = parse_qs(query)
            job_url = (params.get("url") or [""])[0]
            kind = (params.get("kind") or [""])[0]
            if not job_url or kind not in (
                "resume_pdf", "resume_txt", "resume_tex", "cover_pdf", "cover_txt",
            ):
                self._send(400, b"Bad request", "text/plain")
                return
            try:
                row = get_connection().execute(
                    "SELECT tailored_resume_path, cover_letter_path, combined_tex_path "
                    "FROM jobs WHERE url = ?",
                    (job_url,),
                ).fetchone()
            except Exception as exc:  # pragma: no cover - defensive
                self._send(500, str(exc).encode("utf-8"), "text/plain")
                return
            if not row:
                self._send(404, b"Job not found", "text/plain")
                return
            if kind == "resume_tex":
                base = row[2]
            elif kind.startswith("resume"):
                base = row[0]
            else:
                base = row[1]
            if not base:
                self._send(404, b"No file for this job", "text/plain")
                return
            path = Path(base)
            if kind.endswith("_pdf"):
                path = path.with_suffix(".pdf")
            path = path.resolve()
            allowed_dirs = (TAILORED_DIR.resolve(), COVER_LETTER_DIR.resolve())
            if not any(str(path).startswith(str(d) + os.sep) for d in allowed_dirs):
                self._send(403, b"Forbidden", "text/plain")
                return
            if not path.exists():
                self._send(404, b"File missing on disk", "text/plain")
                return
            body = path.read_bytes()
            ctype = ("application/pdf" if path.suffix.lower() == ".pdf"
                     else "text/plain; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_card_state(self, query: str) -> None:
            """Return the per-job card fragments so the browser can update a card
            in place instead of reloading the whole (large) dashboard."""
            url = (parse_qs(query).get("url") or [""])[0]
            if not url:
                self._json({"ok": False, "error": "url required"})
                return
            row = get_connection().execute(
                "SELECT * FROM jobs WHERE url = ?", (url,)
            ).fetchone()
            if not row:
                self._json({"ok": False, "error": "job not found"})
                return
            job = dict(row)
            inbox = get_inbox_summaries().get(url)
            self._json({
                "ok": True,
                "files_html": _job_files_html(job),
                "actions_html": _job_actions_html(job),
                "concepts_html": _job_concepts_html(job),
                "inbox_html": _job_inbox_html(job, inbox),
                "highlighted": int(job.get("highlighted") or 0),
            })

        def _serve_download(self, query: str) -> None:
            """Force-download a job file, keeping a copy in the per-kind
            ``downloads`` folder (first attempt); fall back to the original so
            the download always succeeds."""
            params = parse_qs(query)
            job_url = (params.get("url") or [""])[0]
            kind = (params.get("kind") or [""])[0]
            valid = ("resume_pdf", "resume_txt", "resume_tex", "cover_pdf", "cover_txt")
            if not job_url or kind not in valid:
                self._send(400, b"Bad request", "text/plain")
                return
            src = _resolve_job_file_path(job_url, kind)
            allowed_dirs = (TAILORED_DIR.resolve(), COVER_LETTER_DIR.resolve())
            if not src or not any(
                str(src).startswith(str(d) + os.sep) for d in allowed_dirs
            ):
                self._send(404, b"No file for this job", "text/plain")
                return
            if not src.exists():
                self._send(404, b"File missing on disk", "text/plain")
                return
            serve_path = src
            try:
                ddir = _downloads_dir(kind)
                ddir.mkdir(parents=True, exist_ok=True)
                dest = ddir / src.name
                if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
                    shutil.copy2(src, dest)
                serve_path = dest
            except Exception:  # noqa: BLE001 - never block the download
                serve_path = src
            body = serve_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{src.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/job-file":
                self._serve_job_file(parsed.query)
                return
            if parsed.path == "/download":
                self._serve_download(parsed.query)
                return
            if parsed.path == "/card-state":
                self._serve_card_state(parsed.query)
                return
            if parsed.path not in ("/", "/index.html"):
                self._send(404, b"Not found", "text/plain")
                return
            try:
                html = render_dashboard_html()
            except Exception as exc:  # pragma: no cover - defensive
                self._send(500, str(exc).encode("utf-8"), "text/plain")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = _json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"ok": False, "error": "bad json"})
                return

            if path == "/mark":
                url = data.get("url")
                status = data.get("status")
                if not url or status not in allowed:
                    self._json({"ok": False, "error": "bad request"})
                    return
                try:
                    set_job_status(url, status, data.get("reason"))
                except Exception as exc:  # pragma: no cover - defensive
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True})
            elif path == "/todo/add":
                from applypilot.todos import add_todo
                text = (data.get("text") or "").strip()
                if not text:
                    self._json({"ok": False, "error": "text required"})
                    return
                todos = add_todo(text, data.get("url"), data.get("tag"))
                self._json({"ok": True, "item": todos[-1]})
            elif path == "/todo/toggle":
                from applypilot.todos import toggle_todo
                toggle_todo(data.get("id") or "")
                self._json({"ok": True})
            elif path == "/todo/delete":
                from applypilot.todos import delete_todo
                delete_todo(data.get("id") or "")
                self._json({"ok": True})
            elif path == "/note":
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                set_job_note(url, data.get("note") or "")
                self._json({"ok": True})
            elif path == "/study":
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                set_job_study(url, data.get("note") or "")
                self._json({"ok": True})
            elif path == "/combine":
                from applypilot.scoring.combine import combine_resume
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                row = get_connection().execute(
                    "SELECT url, title, site, company, location, full_description, "
                    "tailored_resume_path FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                try:
                    result = combine_resume(
                        dict(row),
                        extra=(data.get("extra") or ""),
                        variant=(data.get("variant") or None),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, **result})
            elif path == "/combine/delete":
                from applypilot.scoring.combine import delete_combined
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                row = get_connection().execute(
                    "SELECT url, combined_tex_path FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                removed = delete_combined(dict(row))
                self._json({"ok": True, "removed": removed})
            elif path == "/cover":
                from applypilot.scoring.cover_letter import generate_one_cover_letter
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                row = get_connection().execute(
                    "SELECT url, title, site, company, location, country, "
                    "full_description, tailored_resume_path FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                try:
                    result = generate_one_cover_letter(
                        dict(row),
                        extra=str(data.get("extra") or ""),
                        variant=(data.get("variant") or None),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, **result})
            elif path == "/cover/delete":
                from applypilot.scoring.cover_letter import delete_cover_letter
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                row = get_connection().execute(
                    "SELECT url, cover_letter_path FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                removed = delete_cover_letter(dict(row))
                self._json({"ok": True, "removed": removed})
            elif path == "/file/delete":
                url = data.get("url")
                kind = data.get("kind")
                valid_kinds = (
                    "resume_pdf", "resume_txt", "resume_tex", "cover_pdf", "cover_txt",
                )
                if not url or kind not in valid_kinds:
                    self._json({"ok": False, "error": "url and valid kind required"})
                    return
                row = get_connection().execute(
                    "SELECT tailored_resume_path, cover_letter_path, combined_tex_path "
                    "FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                if kind == "resume_tex":
                    base = row[2]
                elif kind.startswith("resume"):
                    base = row[0]
                else:
                    base = row[1]
                if not base:
                    self._json({"ok": False, "error": "no file for this job"})
                    return
                target = Path(base)
                if kind.endswith("_pdf"):
                    target = target.with_suffix(".pdf")
                target = target.resolve()
                allowed_dirs = (TAILORED_DIR.resolve(), COVER_LETTER_DIR.resolve())
                if not any(
                    str(target).startswith(str(d) + os.sep) for d in allowed_dirs
                ):
                    self._json({"ok": False, "error": "forbidden"})
                    return
                try:
                    if target.exists():
                        target.unlink()
                    # Keep the downloads copy in sync.
                    dcopy = _downloads_dir(kind) / target.name
                    if dcopy.exists():
                        dcopy.unlink()
                except OSError as exc:
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, "removed": str(target)})
            elif path == "/focus":
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                set_job_focus(url, bool(data.get("focused")))
                self._json({"ok": True})
            elif path == "/stale":
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                set_job_stale(url, bool(data.get("stale", True)))
                self._json({"ok": True})
            elif path == "/highlight":
                from applypilot.highlights import extract_key_concepts

                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                highlighted = bool(data.get("highlighted", True))
                concepts: list[str] = []
                if highlighted:
                    row = get_connection().execute(
                        "SELECT url, title, site, company, full_description, "
                        "highlight_concepts FROM jobs WHERE url = ?",
                        (url,),
                    ).fetchone()
                    if not row:
                        self._json({"ok": False, "error": "job not found"})
                        return
                    job = dict(row)
                    existing = []
                    try:
                        existing = _json.loads(job.get("highlight_concepts") or "[]")
                    except (ValueError, TypeError):
                        existing = []
                    if existing:
                        concepts = existing
                    else:
                        try:
                            concepts = extract_key_concepts(job)
                        except Exception as exc:  # noqa: BLE001 - report to UI
                            self._json({"ok": False, "error": str(exc)})
                            return
                set_job_highlight(url, highlighted, concepts if highlighted else None)
                self._json({"ok": True, "concepts": concepts})
            elif path == "/inbox/scan":
                from applypilot.inbox import GmailNotConfigured, scan_and_cache
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                row = get_connection().execute(
                    "SELECT url, title, site, company, location, application_url "
                    "FROM jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                if not row:
                    self._json({"ok": False, "error": "job not found"})
                    return
                try:
                    result = scan_and_cache(dict(row))
                except GmailNotConfigured as exc:
                    self._json({"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # pragma: no cover - defensive
                    self._json({"ok": False, "error": str(exc)})
                    return
                self._json({"ok": True, **result})
            elif path == "/inbox/free":
                from applypilot.inbox import free as free_inbox
                url = data.get("url")
                if not url:
                    self._json({"ok": False, "error": "url required"})
                    return
                free_inbox(url)
                self._json({"ok": True})
            elif path == "/answer/add":
                from applypilot.answers import add_answer
                q = (data.get("question") or "").strip()
                a = (data.get("answer") or "").strip()
                if not q or not a:
                    self._json({"ok": False, "error": "question and answer required"})
                    return
                answers = add_answer(q, a)
                self._json({"ok": True, "item": answers[-1]})
            elif path == "/answer/delete":
                from applypilot.answers import delete_answer
                delete_answer(data.get("id") or "")
                self._json({"ok": True})
            else:
                self._send(404, b"Not found", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[green]Dashboard served at {url}[/green]  [dim](Ctrl+C to stop)[/dim]")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard server stopped.[/dim]")
    finally:
        server.server_close()
