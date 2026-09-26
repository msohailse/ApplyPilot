"""Dashboard answer ideas: reusable application-question answers.

Stored as JSON in the ApplyPilot app dir so they survive regeneration.
Click to expand on the dashboard.
"""

import json
import uuid
from datetime import datetime, timezone

from applypilot.config import APP_DIR

ANSWERS_PATH = APP_DIR / "answers.json"

DEFAULT_ANSWERS: list[dict] = [
    {
        "id": "seed-backend-system",
        "question": "Describe a backend system you designed (stack, scale, contribution).",
        "answer": (
            "SWAM, a scalable event-driven incident management system. Stack: a Quarkus "
            "(Jakarta EE/JAX-RS/CDI/JPA) REST API with a hexagonal architecture and a "
            "lightweight CQRS read path, PostgreSQL, Kafka for events, Angular frontend, "
            "Docker + Kubernetes. Flow: creating an incident publishes an event to Kafka; an "
            "independent analyzer service consumes it, runs fuzzy text-similarity matching, and "
            "reports back on a second topic, so duplicate detection never blocks the write path. "
            "I owned the full design and build: domain model, RBAC (Reporter/Dept Admin/Super "
            "Admin with department-scoped visibility), transactional writes, and the Kafka flow. "
            "For scale the API autoscales horizontally (1-5 replicas, 50% CPU target), independent "
            "of the analyzer; I verified the event flow with Testcontainers integration tests."
        ),
    },
    {
        "id": "seed-why-you",
        "question": "Why should we consider you for this role?",
        "answer": (
            "7+ years building and owning backend + cloud systems end to end: Node.js/TypeScript "
            "and Python services on AWS/GCP, Docker/Kubernetes, CI/CD, backed by PostgreSQL, "
            "MongoDB and Elasticsearch. I own reliability and cost, not just delivery: I cut "
            "iRide's MongoDB hosting 80% and reduced a cluster config to save ~$12k/year while "
            "keeping throughput stable. I've built event-driven, high-volume systems (a document "
            "pipeline at 50k images/day and a RAG AI agent). AWS-certified, based in Tromso, ready "
            "to work hybrid and take real ownership from API to production."
        ),
    },
    {
        "id": "seed-cost-performance",
        "question": "Tell us about a time you improved performance or reduced cost.",
        "answer": (
            "At iRide I reduced the MongoDB hosting bill 80% ($1,500 to $300/mo) via Redis "
            "caching, index optimization and query tuning; at Trident Marketing I tuned a cluster "
            "config to save ~$12k/year while holding performance. Method: profile the real "
            "bottleneck, cache the hot path, fix indexes, then re-measure."
        ),
    },
    {
        "id": "seed-distributed",
        "question": "How do you handle distributed systems and consistency?",
        "answer": (
            "In a multi-instance Go/RabbitMQ document vault I used AMQP fan-out to keep state "
            "consistent across instances (instant access revocation on deletion), AES-256 per-user "
            "keys and time-limited links. In SWAM, Kafka topics decouple the write path from an "
            "asynchronous analyzer service."
        ),
    },
    {
        "id": "seed-cloud",
        "question": "What is your cloud / DevOps experience?",
        "answer": (
            "AWS (SNS/SQS, S3; Certified Cloud Practitioner) and GCP (Compute Engine, networking), "
            "Docker + Kubernetes with autoscaling deployments, CI/CD via GitHub Actions and Jenkins, "
            "plus observability and production operational ownership."
        ),
    },
    {
        "id": "seed-authorization",
        "question": "What is your work authorization / availability?",
        "answer": (
            "Based in Tromso, Norway. I can work part-time immediately (20h/week). Once I have a "
            "signed contract my work/visa status converts, typically within two months."
        ),
    },
]


def load_answers() -> list[dict]:
    if not ANSWERS_PATH.exists():
        save_answers(list(DEFAULT_ANSWERS))
        return [dict(a) for a in DEFAULT_ANSWERS]
    try:
        data = json.loads(ANSWERS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_answers(answers: list[dict]) -> None:
    ANSWERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS_PATH.write_text(json.dumps(answers, indent=2), encoding="utf-8")


def add_answer(question: str, answer: str) -> list[dict]:
    answers = load_answers()
    answers.append({
        "id": uuid.uuid4().hex[:8],
        "question": question.strip(),
        "answer": answer.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    save_answers(answers)
    return answers


def delete_answer(answer_id: str) -> list[dict]:
    answers = [a for a in load_answers() if a.get("id") != answer_id]
    save_answers(answers)
    return answers
