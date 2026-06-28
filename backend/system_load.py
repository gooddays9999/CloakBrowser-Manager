"""Host system load metrics for ``/api/status`` (read-only, ``/proc``-based).

Exposes loadavg / CPU count / available memory so upstream (ims-api) can size
its pairing concurrency to real load instead of a static value. Deliberately
cheap: only ``os.getloadavg()`` + a single ``/proc/meminfo`` read, no psutil,
no subprocess, no locks — safe to poll every few seconds.

The Manager runs on host networking with browsers as same-host processes, so
``/proc`` here reflects the whole host (exactly what we want). If a deployment
virtualizes these via LXCFS, the values would become container quotas instead —
see the spec's note.

Best-effort by design: any unreadable part becomes ``None`` rather than failing
the whole call, and the caller treats a fully-unavailable snapshot as ``None``
so ``/api/status`` never errors over load sampling.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("cloakbrowser.manager.system")

MEMINFO_PATH = "/proc/meminfo"


def _read_meminfo_mb() -> tuple[int | None, int | None]:
    """Return ``(MemTotal, MemAvailable)`` in MB from ``/proc/meminfo``."""
    total: int | None = None
    avail: int | None = None
    with open(MEMINFO_PATH) as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
            if total is not None and avail is not None:
                break
    return total, avail


def _loadavg() -> tuple[float, float, float]:
    return os.getloadavg()


def system_load() -> dict[str, Any] | None:
    """Best-effort host load snapshot, or ``None`` if nothing could be read.

    Individual unavailable metrics are reported as ``None`` instead of raising,
    so a missing ``/proc/meminfo`` still yields loadavg (and vice versa).
    """
    cpu = os.cpu_count() or 1
    data: dict[str, Any] = {
        "cpu_count": cpu,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    got_metric = False

    try:
        l1, l5, l15 = _loadavg()
        data["load1"] = round(l1, 2)
        data["load5"] = round(l5, 2)
        data["load15"] = round(l15, 2)
        data["load1_per_core"] = round(l1 / cpu, 3)
        data["load5_per_core"] = round(l5 / cpu, 3)
        got_metric = True
    except (OSError, ValueError) as exc:
        logger.warning("loadavg unavailable: %s", exc)
        data["load1"] = data["load5"] = data["load15"] = None
        data["load1_per_core"] = data["load5_per_core"] = None

    try:
        total, avail = _read_meminfo_mb()
        data["mem_total_mb"] = total
        data["mem_available_mb"] = avail
        data["mem_used_percent"] = (
            round((1 - avail / total) * 100, 1)
            if total and avail is not None
            else None
        )
        if total is not None or avail is not None:
            got_metric = True
    except (OSError, ValueError) as exc:
        logger.warning("meminfo unavailable: %s", exc)
        data["mem_total_mb"] = data["mem_available_mb"] = data["mem_used_percent"] = None

    if not got_metric:
        return None
    return data
