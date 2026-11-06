"""Higher-level Kingsley jobs built on the router, Google API, store and repo tools.

Every job is written so that a missing key or an unreachable provider produces a
clear, actionable error instead of a stack trace in the UI.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from kingsley import config, google_api, store, tools
from kingsley.providers import ROUTER

PERSONA = (
    "You are Kingsley, a local-first cyberpunk productivity operator running on the user's own "
    "machine. You are terse, technical and concrete: no filler, no flattery. Prefer short "
    "paragraphs and tight bullet lists. When you produce tasks, deadlines, or code, be exact. "
    "You have access to the user's Gmail (read), Google Calendar (write), their repository, live "
    "system diagnostics, and transcripts of what they said out loud. Never invent data you were "
    "not given; if something is missing, say which integration needs configuring."
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>[\[{].*?[\]}])\s*```", re.S)


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model response."""
    if not text:
        return None
    fenced = _JSON_FENCE.search(text)
    candidate = fenced.group("body") if fenced else text
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _parse_when(value: str) -> datetime | None:
    """Parse a due string into an aware datetime (UTC) when possible."""
    value = (value or "").strip()
    if not value:
        return None
    for parser in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        lambda v: datetime.strptime(v, "%Y-%m-%d"),
        lambda v: datetime.strptime(v, "%Y/%m/%d"),
        lambda v: datetime.strptime(v, "%d %b %Y"),
    ):
        try:
            parsed = parser(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # Date-only deadlines default to 09:00 UTC so a calendar block exists.
            parsed = parsed.replace(hour=9, minute=0, tzinfo=timezone.utc)
        return parsed
    return None


async def chat(message: str, task: str = "default", provider: str | None = None) -> dict[str, Any]:
    """Conversational turn with short-term history from the store."""
    history = store.recent_messages(limit=12)
    messages: list[dict[str, str]] = []
    for row in history:
        role = "assistant" if row["role"] == "assistant" else "user"
        messages.append({"role": role, "content": row["text"]})
    messages.append({"role": "user", "content": message})
    result = await ROUTER.chat(task, messages, system=PERSONA, override=provider)
    if result.get("text"):
        store.add_message("user", message, task, result.get("provider", ""))
        store.add_message("assistant", result["text"], task, result.get("provider", ""))
    return result


async def gmail_triage(limit: int = 12) -> dict[str, Any]:
    """Read recent unread mail, then turn it into ranked tasks."""
    if not google_api.credentials_configured():
        raise google_api.GoogleError(
            "Gmail is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, then connect at /api/google/connect."
        )
    messages = await google_api.gmail_recent(limit=limit)
    if not messages:
        return {"emails": 0, "created": [], "summary": "No unread mail in the last 14 days."}

    corpus = "\n\n".join(
        f"[{index + 1}] From: {item['from']}\nSubject: {item['subject']}\nDate: {item['date']}\n"
        f"Snippet: {item['snippet']}\nBody: {item['body'][:900]}"
        for index, item in enumerate(messages)
    )
    prompt = (
        "Here are the user's unread emails.\n\n"
        f"{corpus}\n\n"
        "Extract only items that require the user to act. Reply with a JSON array (no prose) of "
        'objects: {"title": short imperative task, "priority": "high"|"normal"|"low", '
        '"due": "YYYY-MM-DD" or "", "why": one short clause, "email": index number}. '
        "At most 8 items. Do not invent deadlines; leave 'due' empty when unknown."
    )
    result = await ROUTER.chat("google", [{"role": "user", "content": prompt}], system=PERSONA)
    parsed = _extract_json(result.get("text", "")) or []
    created = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            task = store.add_task(
                str(item["title"]),
                detail=str(item.get("why", "")),
                priority=str(item.get("priority", "normal")),
                due=str(item.get("due", "")),
                source="gmail",
            )
            if task:
                created.append(task)
    store.add_intel(
        f"Gmail triage ({len(messages)} emails)",
        result.get("text", "")[:4000],
    )
    return {
        "emails": len(messages),
        "created": created,
        "provider": result.get("provider", ""),
        "model": result.get("model", ""),
        "raw": result.get("text", "")[:2000],
    }


async def calendar_sync(limit: int = 10) -> dict[str, Any]:
    """Push open tasks that have a due date into Google Calendar."""
    if not google_api.credentials_configured():
        raise google_api.GoogleError("Google is not configured; set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.")
    block = timedelta(minutes=config.ideal_minutes_per_task())
    synced, skipped = [], []
    for task in store.list_tasks("open")[:limit]:
        if task.get("calendar_event_id"):
            skipped.append({"id": task["id"], "reason": "already scheduled"})
            continue
        when = _parse_when(task.get("due", ""))
        if when is None:
            skipped.append({"id": task["id"], "reason": "no due date"})
            continue
        event = await google_api.calendar_insert(
            task["title"],
            description=task.get("detail", "") or "Scheduled by Kingsley",
            start_iso=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_iso=(when + block).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        store.update_task(task["id"], calendar_event_id=event["id"])
        synced.append({**event, "task_id": task["id"]})
    store.add_event(f"Calendar sync: {len(synced)} events created", source="google", data={"synced": len(synced)})
    return {"synced": synced, "skipped": skipped}


async def organize_tasks() -> dict[str, Any]:
    """Ask the model to rank and schedule the open task list."""
    tasks = store.list_tasks("open")
    if not tasks:
        return {"updated": [], "summary": "No open tasks to organise."}
    listing = "\n".join(
        f"- id={task['id']} | {task['title']} | priority={task['priority']} | due={task['due'] or 'none'} | {task.get('detail', '')[:120]}"
        for task in tasks[:40]
    )
    prompt = (
        "Organise these tasks for maximum throughput.\n\n"
        f"{listing}\n\n"
        "Reply with a JSON array only: "
        '[{"id": <existing id>, "priority": "high"|"normal"|"low", "due": "YYYY-MM-DD" or "", '
        '"rationale": "max 12 words"}]. Keep every id exactly once.'
    )
    result = await ROUTER.chat("ideas", [{"role": "user", "content": prompt}], system=PERSONA)
    parsed = _extract_json(result.get("text", "")) or []
    updated = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                task_id = int(item["id"])
            except (TypeError, ValueError):
                continue
            fields: dict[str, Any] = {}
            if item.get("priority") in {"high", "normal", "low"}:
                fields["priority"] = item["priority"]
            if item.get("due"):
                fields["due"] = str(item["due"])
            if fields:
                updated.append(store.update_task(task_id, **fields))
    store.add_event(f"Task board reorganised ({len(updated)} updated)", source="tasks")
    return {"updated": updated, "provider": result.get("provider", ""), "raw": result.get("text", "")[:1200]}


async def scout(topics: list[str] | None = None, per_topic: int = 4) -> dict[str, Any]:
    """Search the user's interests and produce a briefing with calendar hooks."""
    topics = [topic for topic in (topics or config.interests()) if topic]
    if not topics:
        raise ValueError("No interests configured. Set KINGSLEY_INTERESTS or pass topics.")

    def _search(topic: str) -> list[dict[str, str]]:
        try:
            from ddgs import DDGS

            with DDGS() as client:
                return [
                    {"title": hit.get("title", ""), "url": hit.get("href", ""), "snippet": hit.get("body", "")}
                    for hit in client.text(f"{topic} news", max_results=per_topic)
                ]
        except Exception as exc:  # network / provider failure
            return [{"title": f"search failed: {exc}", "url": "", "snippet": ""}]

    hits: dict[str, list[dict[str, str]]] = {}
    for topic in topics[:5]:
        hits[topic] = await asyncio.to_thread(_search, topic)

    corpus = "\n\n".join(
        f"# {topic}\n" + "\n".join(f"- {hit['title']} — {hit['snippet'][:200]} ({hit['url']})" for hit in results)
        for topic, results in hits.items()
    )
    prompt = (
        f"Fresh search results for the user's interests:\n\n{corpus}\n\n"
        "Write a briefing: for each topic, 2-3 bullets on what actually matters. Then a final "
        "section 'FOCUS' with up to 3 concrete things worth doing this week. "
        "Reply JSON only: "
        '{"briefing": "markdown text", "focus": [{"title": "...", "priority": "high"|"normal"|"low", "due": "YYYY-MM-DD" or ""}]}'
    )
    result = await ROUTER.chat("news", [{"role": "user", "content": prompt}], system=PERSONA)
    parsed = _extract_json(result.get("text", "")) or {}
    briefing = parsed.get("briefing") if isinstance(parsed, dict) else None
    briefing = briefing or result.get("text", "")
    sources = [hit for results in hits.values() for hit in results if hit.get("url")]
    store.add_intel(", ".join(topics), briefing, sources)
    created = []
    focus = parsed.get("focus") if isinstance(parsed, dict) else None
    if isinstance(focus, list):
        for item in focus:
            if isinstance(item, dict) and item.get("title"):
                created.append(
                    store.add_task(
                        str(item["title"]),
                        detail="From interest scout",
                        priority=str(item.get("priority", "normal")),
                        due=str(item.get("due", "")),
                        source="scout",
                    )
                )
    return {
        "topics": topics,
        "briefing": briefing,
        "sources": sources[:12],
        "created": created,
        "provider": result.get("provider", ""),
    }


async def homework(question: str, subject: str = "") -> dict[str, Any]:
    """Explain, don't just answer: worked steps with a check at the end."""
    prompt = (
        f"Subject: {subject or 'general'}\nQuestion: {question}\n\n"
        "Teach this: restate what is being asked, give the method, work it step by step with the "
        "arithmetic or reasoning shown, state the final answer, then add one line on how to verify "
        "it. If the question is ambiguous, state the assumption you made."
    )
    return await ROUTER.chat("homework", [{"role": "user", "content": prompt}], system=PERSONA, max_tokens=1800)


async def repo_agent(instruction: str) -> dict[str, Any]:
    """Plan a repo change, then queue each file edit for human approval."""
    entries = tools.tree(max_depth=2, max_entries=250)
    listing = "\n".join(f"{entry['type']}: {entry['path']}" for entry in entries[:180])
    prompt = (
        f"We are working in {tools.root()}.\n\nRepository listing (depth 2):\n{listing}\n\n"
        f"Request: {instruction}\n\n"
        "Produce the smallest correct change. Reply JSON only: "
        '{"summary": "one paragraph", "edits": [{"path": "relative/path", "content": "FULL new file content"}]}. '
        "Use at most 3 edits. Never delete files. If the request needs information you lack, "
        'return {"summary": "why", "edits": []}.'
    )
    result = await ROUTER.chat("code", [{"role": "user", "content": prompt}], system=PERSONA, max_tokens=3200)
    parsed = _extract_json(result.get("text", "")) or {}
    summary = parsed.get("summary", result.get("text", "")[:600]) if isinstance(parsed, dict) else ""
    approvals = []
    edits = parsed.get("edits") if isinstance(parsed, dict) else None
    for edit in edits or []:
        if not isinstance(edit, dict) or not edit.get("path") or not isinstance(edit.get("content"), str):
            continue
        try:
            approvals.append(tools.propose(str(edit["path"]), edit["content"], summary))
        except tools.ToolError as exc:
            approvals.append({"path": edit["path"], "error": str(exc)})
    store.add_event(
        f"Repo agent proposed {len([a for a in approvals if a.get('id')])} edit(s)",
        source="repo",
        data={"instruction": instruction[:200]},
    )
    return {
        "summary": summary,
        "approvals": approvals,
        "provider": result.get("provider", ""),
        "model": result.get("model", ""),
    }
