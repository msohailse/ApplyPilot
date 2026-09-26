"""Dashboard todo list: persistent notes/tasks with optional links.

Stored as JSON in the ApplyPilot app dir so items survive dashboard
regeneration. Seed items are added on first use.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import APP_DIR

TODOS_PATH = APP_DIR / "todos.json"

# Items created the first time the todo list is used.
DEFAULT_TODOS: list[dict] = [
    {
        "id": "seed-nettowork-wizard",
        "text": "Complete Nettowork candidate profile wizard",
        "url": "https://app.nettowork.it/candidato/wizard",
        "done": False,
        "created_at": "",
    },
]


def load_todos() -> list[dict]:
    """Return the todo list, seeding defaults on first use."""
    if not TODOS_PATH.exists():
        save_todos(list(DEFAULT_TODOS))
        return [dict(t) for t in DEFAULT_TODOS]
    try:
        data = json.loads(TODOS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_todos(todos: list[dict]) -> None:
    TODOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TODOS_PATH.write_text(json.dumps(todos, indent=2), encoding="utf-8")


def add_todo(text: str, url: str | None = None) -> list[dict]:
    todos = load_todos()
    todos.append({
        "id": uuid.uuid4().hex[:8],
        "text": text.strip(),
        "url": (url or "").strip(),
        "done": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_todos(todos)
    return todos


def toggle_todo(todo_id: str) -> list[dict]:
    todos = load_todos()
    for t in todos:
        if t.get("id") == todo_id:
            t["done"] = not t.get("done", False)
    save_todos(todos)
    return todos


def delete_todo(todo_id: str) -> list[dict]:
    todos = [t for t in load_todos() if t.get("id") != todo_id]
    save_todos(todos)
    return todos
