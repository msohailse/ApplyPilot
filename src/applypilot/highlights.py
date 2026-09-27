"""LLM-extracted key concepts ("highlights") for a job description.

Given a job posting, ask the configured LLM for the handful of concepts a
candidate must understand for the role: core skills, tools, domain knowledge,
and responsibilities. Results are short strings, stored on the job row as JSON.
"""

import json
import logging
import re

from applypilot.llm import get_client

log = logging.getLogger(__name__)

MAX_CONCEPTS = 8

_PROMPT = (
    "You extract the key concepts a candidate must understand for a job.\n\n"
    "Read the job description and return the "
    f"{MAX_CONCEPTS} most important concepts: the core skills, technologies, "
    "domain knowledge, and responsibilities that define the role. Prefer "
    "concrete, checkable items (e.g. \"Kubernetes\", \"event-driven architecture\", "
    "\"REST API design\", \"warehouse management systems\"). Avoid soft filler "
    "like \"team player\" or \"fast-paced environment\".\n\n"
    "Return ONLY a JSON array of short strings. No prose. No code fences.\n"
    'Example: ["Kubernetes", "event-driven architecture", "REST API design"]'
)


def _parse(text: str) -> list[str]:
    """Parse the model output into a de-duplicated list of concept strings."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    items: list[str] = []
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                items = [str(i).strip() for i in parsed]
        except json.JSONDecodeError:
            items = []
    if not items:
        # Fallback: treat each non-empty line as one concept.
        items = [re.sub(r"^[-*\d.\s]+", "", ln).strip() for ln in text.splitlines()]

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item[:60])
        if len(out) >= MAX_CONCEPTS:
            break
    return out


def extract_key_concepts(job: dict) -> list[str]:
    """Return the key concepts for a job (empty list if no description)."""
    desc = (job.get("full_description") or "").strip()
    if not desc:
        return []

    client = get_client("highlight")
    company = job.get("company") or job.get("site") or ""
    user = (
        f"JOB TITLE: {job.get('title') or ''}\n"
        f"COMPANY: {company}\n\n"
        f"DESCRIPTION:\n{desc[:6000]}\n\n"
        "Return the JSON array."
    )
    text = client.chat(
        [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=400,
    )
    concepts = _parse(text)
    log.info("Extracted %d key concepts for %s", len(concepts), job.get("title"))
    return concepts
