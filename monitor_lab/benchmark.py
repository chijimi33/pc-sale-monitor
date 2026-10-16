from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from .safety import digest, environment, guard, read, write
from .stores import BACKENDS, SQLite


def process_counters():
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [(n, ctypes.c_size_t) for n in
                         ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                          "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                          "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel.GetCurrentProcess()
        memory, io = Memory(), IO()
        memory.cb = ctypes.sizeof(memory)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
        kernel.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(IO)]
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb) or not kernel.GetProcessIoCounters(handle, ctypes.byref(io)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"peak_rss_bytes": memory.PeakWorkingSetSize, "process_write_bytes": io.WriteTransferCount,
                "process_write_calls": io.WriteOperationCount}
    import resource
    result = {"peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    p = Path("/proc/self/io")
    if p.exists():
        values = dict(line.split(": ") for line in p.read_text().strip().splitlines())
        result.update(process_write_bytes=int(values["write_bytes"]), process_write_calls=int(values["syscw"]))
    return result


def worker(backend, source, output, count=100):
    output = guard(Path(output))
    source = Path(source)
    shard = read(source)
    # A real store shard plus actual product observations, equal mutation order.
    offers = sorted(shard["offers"].items())
    if not offers:
        raise ValueError("Benchmark requires real observations")
    baseline = [("source_shard", "ark", shard)]
    before = process_counters()
    begun = time.perf_counter()
    with BACKENDS[backend](output / "store") as store:
        store.commit("seed", baseline)
        seed_seconds = time.perf_counter() - begun
        measurements = []
        seed_bytes = store.emitted_bytes
        for i in range(count):
            key, offer = offers[i % len(offers)]
            changes = [("observations", key, offer), ("tasks", str(i), {"status": "complete", "original_created_at": offer["observed_at"]}),
                       ("events", str(i), {"offer_key": key, "delivery_status": "not_sent_lab_only"})]
            start = time.perf_counter()
            store.commit("tx-" + str(i), changes)
            measurements.append(time.perf_counter() - start)
        commit_counters = process_counters()
        expected = store.snapshot()
        state_hash = digest(expected)
        logical_bytes, emitted_bytes = store.logical_bytes, store.emitted_bytes
        start = time.perf_counter()
        store.export(output / "export.json")
        export_seconds = time.perf_counter() - start
        backup_seconds = None
        if isinstance(store, SQLite):
            start = time.perf_counter()
            store.backup(output / "backup.sqlite3")
            backup_seconds = time.perf_counter() - start
    start = time.perf_counter()
    with BACKENDS[backend](output / "store") as store:
        assert digest(store.snapshot()) == state_hash
    reload_seconds = time.perf_counter() - start
    assert digest(read(output / "export.json")) == state_hash
    result = {"backend": backend, "source": str(source), "source_json_hash": digest(shard), "count": count,
              "seed_seconds": seed_seconds, "commit_total_seconds": sum(measurements),
              "commit_median_seconds": statistics.median(measurements),
              "commit_p95_seconds": sorted(measurements)[min(count - 1, int(count * .95))],
              "export_seconds": export_seconds, "backup_seconds": backup_seconds, "reload_seconds": reload_seconds,
              "seed_emitted_bytes": seed_bytes, "emitted_storage_bytes": emitted_bytes,
              "logical_mutation_bytes": logical_bytes, "peak_rss_bytes": commit_counters["peak_rss_bytes"],
              "process_write_bytes_delta": commit_counters.get("process_write_bytes", 0) - before.get("process_write_bytes", 0),
              "process_write_calls_delta": commit_counters.get("process_write_calls", 0) - before.get("process_write_calls", 0),
              "state_hash": state_hash, "roundtrip_equal": True, "environment": environment(),
              "measurement_scope": "Actual fsync/transaction IO, single fresh process, warm OS cache, no power-loss claim. SQLite emitted bytes are WAL growth; OS process IO differs by platform."}
    write(output / "result.json", result)
    return result


def benchmark(source, output, repeats=3, count=100):
    output = guard(Path(output))
    results = []
    for repeat in range(repeats):
        # Rotate order to reduce order/thermal/cache bias without changing inputs.
        order = list(BACKENDS)
        order = order[repeat % 3:] + order[:repeat % 3]
        for backend in order:
            root = output / f"{repeat + 1}-{backend}"
            result = subprocess.run([sys.executable, "-m", "monitor_lab", "storage-worker", "--backend", backend,
                                     "--source", str(source), "--output", str(root), "--count", str(count)],
                                    capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            if result.returncode:
                raise RuntimeError(result.stderr)
            results.append(read(root / "result.json"))
    hashes = {r["state_hash"] for r in results}
    if len(hashes) != 1:
        raise AssertionError("Storage backends produced different states")
    summary = {"repeats": repeats, "count_per_repeat": count, "all_states_equal": True, "results": results, "summary": {}}
    for backend in BACKENDS:
        rows = [r for r in results if r["backend"] == backend]
        summary["summary"][backend] = {k: {"median": statistics.median(r[k] for r in rows),
                                           "min": min(r[k] for r in rows), "max": max(r[k] for r in rows)}
                                       for k in ("commit_total_seconds", "peak_rss_bytes", "process_write_bytes_delta", "emitted_storage_bytes", "reload_seconds")}
    write(output / "comparison.json", summary)
    return summary
