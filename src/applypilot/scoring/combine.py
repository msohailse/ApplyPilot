"""Combine Resume: render an existing tailored resume into the LaTeX master.

Reuses the tailored resume text already produced by the ``tailor`` stage (so no
new job analysis / token spend), and asks the configured LLM to inject that
content into the user's base LaTeX template. The base template is never
modified -- the result is written as a brand new ``.tex`` (plus a compiled
``.pdf``) inside the tailored-resumes directory.

This keeps the styling of the master LaTeX resume while reflecting the
job-specific optimizations ApplyPilot already paid for.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import (
    TAILORED_DIR, get_base_resume_tex, load_env, output_prefix,
)
from applypilot.database import get_connection
from applypilot.llm import get_client

log = logging.getLogger(__name__)

# Guard against runaway generation (roughly a dozen pages of LaTeX).
MAX_TEX_CHARS = 120_000

# Common install locations for a TeX engine when it is not on PATH.
_PDFLATEX_FALLBACKS = (
    "/Library/TeX/texbin/pdflatex",
    "/usr/local/bin/pdflatex",
    "/opt/homebrew/bin/pdflatex",
    "/usr/bin/pdflatex",
)


def _safe_prefix(job: dict, variant: str | None = None) -> str:
    """Build the ``resume_<name>[_variant]_Title`` prefix for combined outputs."""
    return output_prefix("resume", job, variant)


def _extract_latex(raw: str) -> str:
    """Pull a complete LaTeX document out of an LLM response.

    Handles markdown code fences and stray prose around the document.
    """
    text = (raw or "").strip()
    fence = re.search(r"```(?:latex|tex)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("\\documentclass")
    end = text.rfind("\\end{document}")
    if start != -1 and end != -1:
        text = text[start:end + len("\\end{document}")]
    return text.strip()


def _find_pdflatex() -> str | None:
    """Locate a pdflatex binary on PATH or in common install locations."""
    found = shutil.which("pdflatex")
    if found:
        return found
    for candidate in _PDFLATEX_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return None


def compile_tex(tex_path: Path, base_dir: Path) -> Path | None:
    """Compile a ``.tex`` file to PDF with pdflatex.

    ``base_dir`` is the directory holding the base template's class files
    (e.g. ``res.cls``); it is added to TEXINPUTS so the new document resolves
    the same packages/classes as the master.

    Returns the PDF path on success, or None if no engine is available or
    compilation fails.
    """
    engine = _find_pdflatex()
    if not engine:
        log.warning("pdflatex not found; generated .tex but skipped the PDF compile.")
        return None

    env = os.environ.copy()
    existing = env.get("TEXINPUTS", "")
    env["TEXINPUTS"] = f"{base_dir}{os.sep}{os.pathsep}{existing}"

    for _ in range(2):  # twice so hyperref/refs settle
        proc = subprocess.run(
            [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={tex_path.parent}",
                tex_path.name,
            ],
            cwd=str(tex_path.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            log.error("pdflatex failed for %s:\n%s", tex_path.name, (proc.stdout or "")[-1500:])
            return None

    pdf_path = tex_path.with_suffix(".pdf")
    return pdf_path if pdf_path.exists() else None


def _build_system_prompt() -> str:
    return (
        "You are an expert LaTeX resume editor and ATS optimization specialist.\n\n"
        "You will receive:\n"
        "1. BASE TEMPLATE -- the user's master LaTeX resume (structure, packages, "
        "visual style, and the only ground-truth facts about their career).\n"
        "2. TAILORED CONTENT -- a resume already tailored for a specific job. "
        "Treat it as the authoritative source for what to emphasize; it may reorder, "
        "reword, and add ATS keywords drawn from the job description.\n"
        "3. JOB DESCRIPTION -- the target role.\n\n"
        "Produce a COMPLETE, compilable LaTeX document that renders the TAILORED "
        "CONTENT using the BASE TEMPLATE's structure, preamble, packages, and style.\n\n"
        "HARD RULES:\n"
        "- Output ONLY the LaTeX document, from \\documentclass to \\end{document}. "
        "No markdown, no code fences, no commentary.\n"
        "- Preserve the base template's preamble, packages, custom commands, and "
        "section ordering/style as closely as possible.\n"
        "- NEVER invent employers, job titles, dates, degrees, certifications, "
        "metrics, or skills. Use ONLY facts present in the base template or the "
        "tailored content.\n"
        "- Reorganize and reword to surface the job's keywords, but do not fabricate.\n"
        "- Keep the result to a similar length (about 1-2 pages) and keep every "
        "LaTeX special character (%, &, #, _, $, {, }) correctly escaped.\n"
    )


def _build_user_prompt(base_tex: str, tailored_txt: str, job: dict, extra: str) -> str:
    job_text = (
        f"TITLE: {job.get('title') or 'N/A'}\n"
        f"COMPANY: {job.get('company') or job.get('site') or 'N/A'}\n"
        f"LOCATION: {job.get('location') or 'N/A'}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )
    parts = [
        "=== BASE TEMPLATE (master resume.tex) ===",
        base_tex,
        "",
        "=== TAILORED CONTENT (already optimized for this job) ===",
        tailored_txt,
        "",
        "=== JOB DESCRIPTION ===",
        job_text,
    ]
    if extra.strip():
        parts += ["", "=== ADDITIONAL USER INSTRUCTIONS ===", extra.strip()]
    parts += ["", "Return the complete tailored LaTeX document."]
    return "\n".join(parts)


def combine_resume(job: dict, extra: str = "", variant: str | None = None) -> dict:
    """Render a job's existing tailored resume into the base LaTeX template.

    Args:
        job: Job row (dict) with ``url``, ``title``, ``site``, ``company``,
            ``location``, ``full_description`` and ``tailored_resume_path``.
        extra: Optional extra instructions to guide the LLM (the "chat" nudge).

    Returns:
        {"tex_path": str | None, "pdf_path": str | None, "prefix": str,
         "fallback": bool}

        ``tex_path`` is None and ``fallback`` is True when no LaTeX master could
        be located; in that case the tailored text is rendered to PDF with
        ApplyPilot's default pipeline instead.

    Raises:
        ValueError: if the LLM response is not a complete LaTeX document.
    """
    load_env()

    tailored_path = job.get("tailored_resume_path")
    if not tailored_path or not Path(tailored_path).exists():
        # On-demand tailoring: no batch `tailor` stage required. Generate this
        # single job's tailored text now, then reuse it for the LaTeX combine.
        from applypilot.scoring.tailor import tailor_one_job

        log.info("No tailored resume for %r yet; tailoring on demand...", job.get("title"))
        tailored_path = tailor_one_job(
            job, validation_mode="normal", variant=variant, extra=extra,
        )["path"]

    base = get_base_resume_tex(variant)
    if base is None:
        # No LaTeX master available: fall back to ApplyPilot's standard
        # tailored-text -> PDF rendering so the action still succeeds.
        from applypilot.scoring.pdf import convert_to_pdf

        pdf = convert_to_pdf(Path(tailored_path))
        log.warning(
            "No base LaTeX resume found; rendered the tailored resume to PDF via the "
            "default pipeline. Set APPLYPILOT_BASE_RESUME_TEX to enable LaTeX combine."
        )
        return {
            "tex_path": None,
            "pdf_path": str(pdf),
            "prefix": _safe_prefix(job, variant),
            "fallback": True,
        }

    base_tex = base.read_text(encoding="utf-8")
    tailored_txt = Path(tailored_path).read_text(encoding="utf-8")

    client = get_client("tailor")
    raw = client.chat(
        [
            {"role": "system", "content": _build_system_prompt()},
            {
                "role": "user",
                "content": _build_user_prompt(base_tex, tailored_txt, job, extra),
            },
        ],
        max_tokens=8000,
        temperature=0.2,
    )

    latex = _extract_latex(raw)
    if "\\documentclass" not in latex or "\\end{document}" not in latex:
        raise ValueError("The LLM did not return a complete LaTeX document.")
    if len(latex) > MAX_TEX_CHARS:
        raise ValueError("Generated LaTeX is unexpectedly large; aborting.")

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    prefix = _safe_prefix(job, variant)
    tex_path = TAILORED_DIR / f"{prefix}.tex"
    tex_path.write_text(latex, encoding="utf-8")

    pdf_path = compile_tex(tex_path, base.parent)

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET combined_tex_path=?, combined_at=? WHERE url=?",
        (str(tex_path), datetime.now(timezone.utc).isoformat(), job["url"]),
    )
    conn.commit()

    log.info("Combined resume written: %s (pdf=%s)", tex_path, pdf_path)
    return {
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "prefix": prefix,
        "fallback": False,
    }


def delete_combined(job: dict) -> list[str]:
    """Delete the combined ``.tex``/``.pdf`` for a job and clear the DB fields.

    The tailored ``.txt`` (and its report) are preserved; only the combined
    artifacts are removed.

    Returns:
        List of file paths that were deleted.
    """
    removed: list[str] = []
    tex = job.get("combined_tex_path")
    if tex:
        tex_path = Path(tex)
        for path in (tex_path, tex_path.with_suffix(".pdf")):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("Could not delete %s: %s", path, exc)

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET combined_tex_path=NULL, combined_at=NULL WHERE url=?",
        (job["url"],),
    )
    conn.commit()
    return removed
