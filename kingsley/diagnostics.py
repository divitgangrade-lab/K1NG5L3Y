"""Live diagnostics for the Kingsley UI (real numbers, no placeholders)."""

from __future__ import annotations

import os
import platform
import shutil
import time
from typing import Any

import psutil

from kingsley import config, store

_STARTED = time.time()
psutil.cpu_percent(interval=None)  # prime the sampler so the first read is sane


def _safe(call, default):
    try:
        return call()
    except Exception:
        return default


def snapshot() -> dict[str, Any]:
    memory = _safe(psutil.virtual_memory, None)
    disk = _safe(lambda: psutil.disk_usage(str(config.HOME.parent)), None)
    load = _safe(lambda: os.getloadavg(), (0.0, 0.0, 0.0))
    net = _safe(psutil.net_io_counters, None)
    process = psutil.Process(os.getpid())
    cpu_count = psutil.cpu_count(logical=True) or 1
    per_core = _safe(lambda: psutil.cpu_percent(interval=None, percpu=True), [])

    return {
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "uptime_seconds": round(time.time() - _STARTED, 1),
        "cpu": {
            "percent": round(psutil.cpu_percent(interval=None), 1),
            "cores": cpu_count,
            "per_core": [round(value, 1) for value in per_core][:16],
            "load_1m": round(load[0], 2),
        },
        "memory": {
            "percent": round(memory.percent, 1) if memory else 0.0,
            "used_gb": round(memory.used / 1e9, 2) if memory else 0.0,
            "total_gb": round(memory.total / 1e9, 2) if memory else 0.0,
        },
        "disk": {
            "percent": round(disk.percent, 1) if disk else 0.0,
            "free_gb": round(disk.free / 1e9, 2) if disk else 0.0,
            "total_gb": round(disk.total / 1e9, 2) if disk else 0.0,
        },
        "network": {
            "sent_mb": round(net.bytes_sent / 1e6, 1) if net else 0.0,
            "recv_mb": round(net.bytes_recv / 1e6, 1) if net else 0.0,
        },
        "process": {
            "pid": process.pid,
            "cpu_percent": round(_safe(lambda: process.cpu_percent(interval=None), 0.0), 1),
            "rss_mb": round(_safe(lambda: process.memory_info().rss, 0) / 1e6, 1),
            "threads": _safe(lambda: process.num_threads(), 0),
        },
        "workspace": str(config.workspace_root()),
        "data_home": str(config.HOME),
        "disk_free_hint": _safe(lambda: shutil.disk_usage("/").free, 0),
        "counters": store.counts(),
        "generated_at": time.time(),
    }
