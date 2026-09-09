"""
Periodic resource monitor.

Port of logger.py's get_system_resources / log_system_resources / ResourceMonitor
(interval 300s). Legacy's version is a QThread/threading.Thread; this is an
asyncio loop instead, matching sync_worker.py's style, since there is no Qt
event loop here. Diagnostic only — never touches the conveyor, the camera, or
any scan state.

Started from the app lifespan in app/main.py.
"""

import logging
import os

from app.core.config import settings

logger = logging.getLogger("ResourceMonitor")

try:
    import psutil
    _psutil_available = True
except ImportError:
    _psutil_available = False
    logger.warning("psutil not available - resource monitoring disabled")


def get_system_resources() -> dict | None:
    """One snapshot of process/system CPU, memory, swap, and disk usage."""
    if not _psutil_available:
        return None

    try:
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        try:
            cpu_percent = process.cpu_percent(interval=0.1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cpu_percent = 0.0
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        logger.debug("Error getting process info: %s", exc)
        return None

    try:
        sys_mem = psutil.virtual_memory()
        sys_swap = psutil.swap_memory()
    except (OSError, RuntimeError) as exc:
        logger.debug("Error getting system memory: %s", exc)
        sys_mem = None
        sys_swap = None

    try:
        disk = psutil.disk_usage("/")
    except (OSError, PermissionError) as exc:
        logger.debug("Error getting disk usage: %s", exc)
        disk = None

    result: dict = {}
    try:
        result["process_memory_mb"] = mem_info.rss / (1024 * 1024)
        result["process_memory_percent"] = process.memory_percent()
        result["process_cpu_percent"] = cpu_percent
    except Exception as exc:
        logger.debug("Error calculating process metrics: %s", exc)
        return None

    if sys_mem:
        result["system_memory_total_gb"] = sys_mem.total / (1024 ** 3)
        result["system_memory_used_gb"] = sys_mem.used / (1024 ** 3)
        result["system_memory_percent"] = sys_mem.percent
        result["system_memory_available_gb"] = sys_mem.available / (1024 ** 3)

    result["swap_used_gb"] = (sys_swap.used / (1024 ** 3)) if sys_swap else 0.0
    result["swap_percent"] = sys_swap.percent if sys_swap else 0.0

    if disk:
        result["disk_used_gb"] = disk.used / (1024 ** 3)
        result["disk_free_gb"] = disk.free / (1024 ** 3)
        result["disk_percent"] = (disk.used / disk.total) * 100

    return result or None


def log_system_resources() -> None:
    """Log one snapshot. Legacy escalates to WARNING above 85% system memory
    (logger.py:322) — a device with no operator watching it should still make
    a slow leak visible in the log, not just in a metrics dict nobody reads."""
    resources = get_system_resources()
    if resources is None:
        return

    process_info = (
        f"Process: {resources.get('process_memory_mb', 0):.1f}MB "
        f"({resources.get('process_memory_percent', 0):.1f}%), "
        f"CPU: {resources.get('process_cpu_percent', 0):.1f}%"
    )
    if "system_memory_used_gb" in resources:
        mem_info = (
            f"System Memory: {resources['system_memory_used_gb']:.2f}GB/"
            f"{resources['system_memory_total_gb']:.2f}GB "
            f"({resources.get('system_memory_percent', 0):.1f}%), "
            f"Available: {resources.get('system_memory_available_gb', 0):.2f}GB"
        )
    else:
        mem_info = "System Memory: N/A"
    swap_info = (
        f"Swap: {resources.get('swap_used_gb', 0):.2f}GB "
        f"({resources.get('swap_percent', 0):.1f}%)"
    )
    if "disk_used_gb" in resources:
        disk_info = (
            f"Disk: {resources['disk_used_gb']:.1f}GB used, "
            f"{resources['disk_free_gb']:.1f}GB free "
            f"({resources.get('disk_percent', 0):.1f}% used)"
        )
    else:
        disk_info = "Disk: N/A"

    msg = f"System Resources - {process_info} | {mem_info} | {swap_info} | {disk_info}"
    if resources.get("system_memory_percent", 0) > 85:
        logger.warning(msg)
    else:
        logger.info(msg)


async def resource_monitor_worker():
    """Loop forever, logging resource usage on the configured interval."""
    if not _psutil_available:
        logger.warning("Resource monitor not started: psutil unavailable.")
        return

    import asyncio

    interval = max(1, settings.RESOURCE_MONITOR_INTERVAL_SECONDS)
    logger.info("Resource monitor started (interval: %ss)", interval)
    try:
        while True:
            try:
                await asyncio.to_thread(log_system_resources)
            except Exception as exc:
                logger.error("Error in resource monitor: %s", exc)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Resource monitor stopped.")
        raise
