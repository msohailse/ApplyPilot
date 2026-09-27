"""Dashboard roles: reference job roles with a URL, tag, and extracted skills.

Stored as JSON in the ApplyPilot app dir so they survive dashboard
regeneration. Each role can have its skills pulled out of the posting URL by
the LLM.
"""

import json
import re
import urllib.request
import uuid
from datetime import datetime, timezone

from applypilot.config import APP_DIR

ROLES_PATH = APP_DIR / "roles.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Seeded the first time the Roles section is used.
DEFAULT_ROLES: list[dict] = [
    {
        "id": "seed-reaktor-ai-developer",
        "name": "AI Developer - LLM Applications (Reaktor)",
        "url": "https://www.reaktor.com/careers/ai-developer",
        "tag": "ai-engineer",
        "skills": [],
        "created_at": "",
    },
]


def load_roles() -> list[dict]:
    """Return the roles list, seeding the default example on first use."""
    if not ROLES_PATH.exists():
        save_roles([dict(r) for r in DEFAULT_ROLES])
        return [dict(r) for r in DEFAULT_ROLES]
    try:
        data = json.loads(ROLES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_roles(roles: list[dict]) -> None:
    ROLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROLES_PATH.write_text(json.dumps(roles, indent=2), encoding="utf-8")


def add_role(name: str, url: str | None = None, tag: str | None = None) -> list[dict]:
    roles = load_roles()
    roles.append({
        "id": uuid.uuid4().hex[:8],
        "name": (name or "").strip(),
        "url": (url or "").strip(),
        "tag": (tag or "").strip(),
        "skills": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_roles(roles)
    return roles


def delete_role(role_id: str) -> list[dict]:
    roles = [r for r in load_roles() if r.get("id") != role_id]
    save_roles(roles)
    return roles


def set_role_skills(role_id: str, skills: list[str]) -> list[dict]:
    roles = load_roles()
    for r in roles:
        if r.get("id") == role_id:
            r["skills"] = skills
            r["skills_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_roles(roles)
    return roles


def _fetch_text(url: str) -> str:
    """Fetch a page and return its visible text (trimmed)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        html = resp.read().decode("utf-8", "ignore")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())[:14000]


def extract_role_skills(url: str, name: str = "") -> list[str]:
    """Fetch the role posting and ask the LLM for its required skills."""
    if not url:
        raise ValueError("This role has no URL to read.")
    text = _fetch_text(url)

    from applypilot.llm import get_client

    prompt = (
        "Extract the concrete skills, technologies and competencies required by "
        "this job posting. Return ONLY a JSON array of short strings, most "
        "important first, at most 30 items.\n\n"
        f"Role: {name or '(unknown)'}\n\nPosting:\n{text}"
    )
    raw = get_client("highlight").ask(prompt, temperature=0.0, max_tokens=900)

    # Parse the first JSON array in the response.
    match = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not match:
        raise ValueError("The model did not return a skill list.")
    data = json.loads(match.group(0))
    skills: list[str] = []
    for item in data:
        s = str(item).strip()
        if s and s not in skills:
            skills.append(s)
    return skills[:30]
