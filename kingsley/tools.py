"""Repo tools: allowlisted file access, read-only git, and approval-gated writes.

The model never writes to disk directly. It proposes content, that proposal is
stored as an approval, and only an explicit approve action applies it. Paths are
confined to the configured workspace root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from kingsley import config, store

_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".kingsley", "dist", "build", ".mypy_cache"}
_TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".toml", ".yml", ".yaml",
    ".css", ".html", ".sh", ".rs", ".cfg", ".ini", ".sql", ".env.example",
}
_GIT_ALLOWED = {"status", "diff", "log", "branch", "show", "remote"}
_MAX_READ = 200_000


class ToolError(RuntimeError):
    """Tool misuse: bad path, disallowed command, oversized payload."""


def root() -> Path:
    return config.workspace_root()


def resolve(path: str) -> Path:
    """Resolve ``path`` and refuse anything outside the workspace root."""
    candidate = (root() / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    base = root()
    if candidate != base and base not in candidate.parents:
        raise ToolError(f"{path} is outside the Kingsley workspace ({base})")
    return candidate


def tree(max_depth: int = 2, max_entries: int = 400) -> list[dict[str, Any]]:
    base = root()
    entries: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if len(relative.parts) > max_depth:
            continue
        if any(part in _IGNORED_DIRS for part in relative.parts):
            continue
        if len(entries) >= max_entries:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "type": "dir" if path.is_dir() else "file",
                "size": 0 if path.is_dir() else stat.st_size,
            }
        )
    return entries


def read(path: str) -> dict[str, Any]:
    target = resolve(path)
    if not target.is_file():
        raise ToolError(f"{path} is not a file")
    if target.stat().st_size > _MAX_READ:
        raise ToolError(f"{path} is larger than {_MAX_READ} bytes")
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "text": text, "bytes": len(text.encode("utf-8"))}


def write(path: str, content: str) -> dict[str, Any]:
    target = resolve(path)
    suffix = target.suffix.lower()
    if suffix and suffix not in _TEXT_SUFFIXES:
        raise ToolError(f"refusing to write non-text file type: {suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    store.add_event(f"Applied patch to {path}", source="repo", data={"path": path})
    return {"path": path, "bytes": len(content.encode("utf-8"))}


def diff_for(path: str, content: str) -> str:
    """Return a compact line diff between disk and the proposed content."""
    target = resolve(path)
    old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    if old == content:
        return "(no changes)"
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(), content.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2
    )
    rendered = "\n".join(diff)
    return rendered[:6000] + ("\n… truncated" if len(rendered) > 6000 else "")


def git(*args: str) -> dict[str, Any]:
    if not args or args[0] not in _GIT_ALLOWED:
        raise ToolError(f"git subcommand not allowed: {' '.join(args)}")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root()),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"git failed: {exc}") from exc
    return {
        "command": "git " + " ".join(args),
        "ok": result.returncode == 0,
        "output": (result.stdout + result.stderr)[:8000],
    }


def propose(path: str, content: str, summary: str = "") -> dict[str, Any]:
    """Queue a write for human approval; nothing touches disk yet."""
    resolve(path)  # validates the path early
    approval = store.add_approval(path, content, summary)
    approval["diff"] = diff_for(path, content)
    store.add_event(f"Patch proposed for {path}", source="repo", data={"approval": approval["id"]})
    return approval


def approve(approval_id: int) -> dict[str, Any]:
    approval = store.get_approval(approval_id)
    if not approval:
        raise ToolError(f"approval {approval_id} not found")
    if approval["status"] != "pending":
        raise ToolError(f"approval {approval_id} is already {approval['status']}")
    result = write(approval["path"], approval["content"])
    store.set_approval_status(approval_id, "approved")
    return result


def reject(approval_id: int) -> dict[str, Any]:
    approval = store.get_approval(approval_id)
    if not approval:
        raise ToolError(f"approval {approval_id} not found")
    store.set_approval_status(approval_id, "rejected")
    store.add_event(f"Patch rejected for {approval['path']}", level="warn", source="repo")
    return {"id": approval_id, "status": "rejected"}
