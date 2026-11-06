"""Kingsley HTTP surface + cyberpunk UI host."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kingsley import agents, config, diagnostics, google_api, store, tools
from kingsley.providers import ROUTER

STATIC_DIR = Path(__file__).resolve().parent / "static"
VERSION = "0.1.0"

app = FastAPI(title="Kingsley", version=VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json")

# The UI may be hosted on GitHub Pages while the backend runs locally, so the
# browser origin differs from the API origin. Kingsley is single-user and binds
# loopback by default; CORS is open, authentication is not simulated.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_SUBSCRIBERS: set[asyncio.Queue] = set()


def emit(message: str, *, level: str = "info", source: str = "kingsley", data: Any = None) -> dict[str, Any]:
    event = store.add_event(message, level=level, source=source, data=data)
    payload = json.dumps(event)
    for queue in list(_SUBSCRIBERS):
        with suppress(asyncio.QueueFull):
            queue.put_nowait(payload)
    return event


def _guard(callable_fn):
    """Run a job and turn integration errors into HTTP 400s with real reasons."""

    async def wrapper(*args, **kwargs):
        try:
            return await callable_fn(*args, **kwargs)
        except (google_api.GoogleError, tools.ToolError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return wrapper


# --- UI ---------------------------------------------------------------------
# The console is mounted at the root so its assets resolve with relative paths.
# That is what makes the same files deployable to GitHub Pages unchanged.


# --- state ------------------------------------------------------------------


@app.get("/api/state")
async def state() -> dict[str, Any]:
    health = await ROUTER.probe()
    return {
        "version": VERSION,
        "persona": "Kingsley",
        "providers": [{**provider, "health": health.get(provider["slug"], "unknown")} for provider in ROUTER.describe()],
        "routes": {task: list(chain) for task, chain in config.TASK_ROUTES.items()},
        "task_kinds": list(config.TASK_KINDS),
        "google": await google_api.status(),
        "workspace": str(config.workspace_root()),
        "data_home": str(config.HOME),
        "interests": config.interests(),
        "counters": store.counts(),
        "listening": {"mode": "browser-web-speech", "wake_words": ["kingsley", "hey kingsley"]},
    }


@app.get("/api/diagnostics")
async def diag() -> dict[str, Any]:
    payload = diagnostics.snapshot()
    payload["providers"] = await ROUTER.probe()
    return payload


@app.get("/api/events")
async def events(limit: int = 40) -> dict[str, Any]:
    return {"events": store.recent_events(limit)}


@app.get("/api/events/stream")
async def event_stream() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _SUBSCRIBERS.add(queue)
    backlog = list(reversed(store.recent_events(20)))

    async def generator():
        try:
            yield ": connected\n\n"
            for event in backlog:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            _SUBSCRIBERS.discard(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# --- chat -------------------------------------------------------------------


@app.post("/api/chat")
async def chat(payload: dict[str, Any]) -> dict[str, Any]:
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    task = str(payload.get("task", "default"))
    provider = payload.get("provider") or None
    result = await agents.chat(message, task, provider)
    emit(
        f"{result.get('provider_label') or 'router'} answered ({task})" if result.get("text") else "router had no available provider",
        level="info" if result.get("text") else "warn",
        source="router",
    )
    return result


@app.get("/api/chat")
async def chat_history(limit: int = 40) -> dict[str, Any]:
    return {"messages": store.recent_messages(limit)}


# --- listening --------------------------------------------------------------


@app.post("/api/listen")
async def listen(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    kind = str(payload.get("kind", "speech"))
    store.add_transcript(text, kind)
    lowered = text.lower()
    wake = next((word for word in ("hey kingsley", "kingsley") if word in lowered), "")
    created = None
    if wake:
        command = lowered.split(wake, 1)[1].strip(" ,.:;-")
        if command.startswith(("add task", "task", "todo")):
            title = command.split(" ", 2)[-1] if command.startswith("add task") else command
            title = title.replace("task", "", 1).strip(" :") or command
            if title:
                created = store.add_task(title[:300], source="voice")
        emit(f"Heard wake word: {command[:120] or text[:120]}", level="warn", source="listen")
        await agents.chat(text, "fast")
    else:
        emit(f"Transcript captured ({len(text)} chars)", level="debug", source="listen")
    return {"stored": True, "wake": bool(wake), "task": created}


@app.get("/api/transcripts")
async def transcripts(limit: int = 60) -> dict[str, Any]:
    return {"transcripts": store.recent_transcripts(limit)}


# --- screen / vision --------------------------------------------------------


@app.post("/api/vision")
async def vision(payload: dict[str, Any]) -> dict[str, Any]:
    image = str(payload.get("image", "")).strip()
    if not image:
        raise HTTPException(status_code=400, detail="image data URL is required")
    question = str(payload.get("question") or "Describe this screen. What is the user working on, and what should they do next? Be specific and brief.")
    result = await ROUTER.chat(
        "vision",
        [{"role": "user", "content": question}],
        system=agents.PERSONA,
        images=[image],
        max_tokens=900,
    )
    if result.get("text"):
        emit(f"Screen analysed via {result.get('provider_label', 'vision model')}", source="vision")
    else:
        emit("Vision request failed: no provider with image support available", level="error", source="vision")
    return result


# --- tasks ------------------------------------------------------------------


@app.get("/api/tasks")
async def get_tasks(status: str | None = None) -> dict[str, Any]:
    return {"tasks": store.list_tasks(status)}


@app.post("/api/tasks")
async def create_task(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        task = store.add_task(
            str(payload.get("title", "")),
            detail=str(payload.get("detail", "")),
            priority=str(payload.get("priority", "normal")),
            due=str(payload.get("due", "")),
            source="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    emit(f"Task added: {task.get('title', '')}", source="tasks")
    return task


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    task = store.update_task(task_id, **payload)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.delete("/api/tasks/{task_id}")
async def remove_task(task_id: int) -> dict[str, Any]:
    store.delete_task(task_id)
    return {"deleted": task_id}


@app.post("/api/tasks/organize")
async def organize() -> dict[str, Any]:
    return await _guard(agents.organize_tasks)()


# --- google -----------------------------------------------------------------


@app.get("/api/google/status")
async def google_status() -> dict[str, Any]:
    return await google_api.status()


@app.get("/api/google/connect")
async def google_connect() -> dict[str, Any]:
    try:
        return {"url": google_api.auth_url()}
    except google_api.GoogleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/google/callback", include_in_schema=False)
async def google_callback(code: str = "", error: str = "") -> HTMLResponse:
    if error or not code:
        return HTMLResponse(
            f"<html><body style='background:#05070d;color:#ff2bd6;font-family:monospace;padding:40px'>"
            f"Google authorization failed: {error or 'no code returned'}</body></html>",
            status_code=400,
        )
    try:
        info = await google_api.exchange_code(code)
    except google_api.GoogleError as exc:
        return HTMLResponse(
            f"<html><body style='background:#05070d;color:#ff2bd6;font-family:monospace;padding:40px'>"
            f"{exc}</body></html>",
            status_code=400,
        )
    emit(f"Google connected: {info.get('email', 'account')}", source="google")
    return HTMLResponse(
        "<html><body style='background:#05070d;color:#00f0ff;font-family:monospace;padding:40px'>"
        f"<h2>KINGSLEY // GOOGLE LINKED</h2><p>{info.get('email', '')}</p>"
        "<p>You can close this tab and return to the console.</p></body></html>"
    )


@app.post("/api/gmail/triage")
async def gmail_triage(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    limit = int((payload or {}).get("limit", 12))
    return await _guard(agents.gmail_triage)(limit)


@app.get("/api/gmail/recent")
async def gmail_recent(limit: int = 8) -> dict[str, Any]:
    return await _guard(google_api.gmail_recent)(limit=limit)


@app.get("/api/calendar/upcoming")
async def calendar_upcoming(limit: int = 10) -> dict[str, Any]:
    return await _guard(google_api.calendar_upcoming)(limit)


@app.post("/api/calendar/sync")
async def calendar_sync() -> dict[str, Any]:
    return await _guard(agents.calendar_sync)()


# --- intel ------------------------------------------------------------------


@app.post("/api/scout")
async def scout(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    topics = (payload or {}).get("topics") or None
    if isinstance(topics, str):
        topics = [item.strip() for item in topics.split(",") if item.strip()]
    return await _guard(agents.scout)(topics)


@app.get("/api/intel")
async def intel(limit: int = 8) -> dict[str, Any]:
    return {"intel": store.recent_intel(limit)}


@app.post("/api/homework")
async def homework(payload: dict[str, Any]) -> dict[str, Any]:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    return await ROUTER.chat(
        "homework",
        [{"role": "user", "content": question}],
        system=agents.PERSONA,
        max_tokens=1800,
    )


# --- repo -------------------------------------------------------------------


@app.get("/api/repo/tree")
async def repo_tree(depth: int = 2) -> dict[str, Any]:
    return {"root": str(tools.root()), "entries": tools.tree(max_depth=depth)}


@app.get("/api/repo/file")
async def repo_file(path: str) -> dict[str, Any]:
    try:
        return tools.read(path)
    except tools.ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/repo/git")
async def repo_git(cmd: str = "status") -> dict[str, Any]:
    args = {"status": ("status", "--short", "--branch"), "diff": ("diff", "--stat"), "log": ("log", "--oneline", "-10")}
    try:
        return tools.git(*args.get(cmd, ("status", "--short")))
    except tools.ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/repo/agent")
async def repo_agent(payload: dict[str, Any]) -> dict[str, Any]:
    instruction = str(payload.get("instruction", "")).strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    return await _guard(agents.repo_agent)(instruction)


@app.get("/api/approvals")
async def approvals(status: str | None = "pending") -> dict[str, Any]:
    items = store.list_approvals(status)
    for item in items:
        try:
            item["diff"] = tools.diff_for(item["path"], item["content"])
        except tools.ToolError as exc:
            item["diff"] = f"(cannot diff: {exc})"
    return {"approvals": items}


@app.post("/api/approvals/{approval_id}/approve")
async def approve(approval_id: int) -> dict[str, Any]:
    try:
        result = tools.approve(approval_id)
    except tools.ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    emit(f"Approved patch: {result['path']}", source="repo")
    return result


@app.post("/api/approvals/{approval_id}/reject")
async def reject(approval_id: int) -> dict[str, Any]:
    try:
        return tools.reject(approval_id)
    except tools.ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": VERSION}


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# Registered last: API routes above always win, everything else is the console.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")
