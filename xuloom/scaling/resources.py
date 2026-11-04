"""Conservative host observations, not a claim of OS resource isolation."""
import os
from pathlib import Path


def memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Status(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page", "free_page", "virtual", "free_virtual", "extended")]
        result = Status(); result.length = ctypes.sizeof(result)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(result)):
            raise OSError("Could not inspect host memory")
        return result.total, result.available
    if Path("/proc/meminfo").exists():
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1); info[key] = int(value.strip().split()[0]) * 1024
        total, available = info["MemTotal"], info.get("MemAvailable", info["MemFree"])
        try:
            root = Path("/sys/fs/cgroup")
            cap, used = (root / "memory.max").read_text().strip(), int((root / "memory.current").read_text())
            if cap != "max":
                total, available = min(total, int(cap)), min(available, max(0, int(cap) - used))
        except (OSError, ValueError):
            pass
        return total, available
    # macOS's conservative available-memory observation; no shell invocation.
    import subprocess
    total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    output = subprocess.check_output(["vm_stat"], text=True)
    free = sum(int(line.split(":")[1].strip().rstrip(".")) for line in output.splitlines()
               if line.startswith(("Pages free:", "Pages inactive:")))
    page = int(output.split("page size of ")[1].split(" bytes")[0])
    return total, min(total, free * page)


def snapshot(reserve_percent=25):
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    try:
        quota = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota[0] != "max":
            cpus = min(cpus, max(1, int(quota[0]) // int(quota[1])))
    except (OSError, ValueError):
        pass
    try:
        total, available = memory()
        spare = max(0, available - total * reserve_percent // 100)
        # Initial per-worker headroom, refined by observed pressure. Browsers,
        # local model servers and builds still require their own hard limits.
        suggested = max(0, min(spare // (256 * 1024 * 1024), cpus * 8))
        return {"cpus": cpus, "total_memory": total, "available_memory": available,
                "suggested": suggested, "pressure": spare == 0, "observed": True}
    except (OSError, ValueError, IndexError, KeyError):
        return {"cpus": cpus, "suggested": 1, "observed": False, "pressure": False,
                "warning": "Memory probe unavailable; automatic capacity stays at one"}
