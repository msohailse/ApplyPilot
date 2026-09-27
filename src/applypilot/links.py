"""Dashboard links: named shortcuts with an optional tag.

Stored as JSON in the ApplyPilot app dir so links survive dashboard
regeneration.
"""

import json
import uuid
from datetime import datetime, timezone

from applypilot.config import APP_DIR

LINKS_PATH = APP_DIR / "links.json"

DEFAULT_LINKS: list[dict] = []


def load_links() -> list[dict]:
    """Return the saved links list."""
    if not LINKS_PATH.exists():
        return [dict(l) for l in DEFAULT_LINKS]
    try:
        data = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_links(links: list[dict]) -> None:
    LINKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINKS_PATH.write_text(json.dumps(links, indent=2), encoding="utf-8")


def add_link(name: str, url: str | None = None, tag: str | None = None) -> list[dict]:
    links = load_links()
    links.append({
        "id": uuid.uuid4().hex[:8],
        "name": (name or "").strip(),
        "url": (url or "").strip(),
        "tag": (tag or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_links(links)
    return links


def delete_link(link_id: str) -> list[dict]:
    links = [l for l in load_links() if l.get("id") != link_id]
    save_links(links)
    return links
