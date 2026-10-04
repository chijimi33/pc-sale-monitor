from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Isolated PC sale architecture lab; production outputs are forbidden.")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--saved-pages", type=Path)
    compare = sub.add_parser("run")
    compare.add_argument("--architecture", choices=["A", "B", "C"], required=True)
    compare.add_argument("--input", type=Path, required=True)
    compare.add_argument("--snapshot", choices=["transport_failure", "tsukumo_recovery", "ark_repaired"], required=True)
    compare.add_argument("--mode", choices=["replay", "live"], required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--backend", choices=["json", "journal", "sqlite"])
    compare.add_argument("--transport", choices=["urllib", "pooled", "browser"], default="urllib")
    compare.add_argument("--budget", type=int, default=2100)
    compare.add_argument("--cycles", type=int, default=1)
    compare.add_argument("--max-tasks", type=int, default=20)
    compare.add_argument("--discovery-input", type=Path)
    compare.add_argument('--capture-input', type=Path, help='Replay a complete scoped capture without primary-page fixture fallback')
    compare.add_argument('--capture-method', choices=['urllib', 'pooled', 'browser'], default='urllib')
    compare.add_argument('--candidate-url', action='append', dest='candidate_urls', help='Explicit candidate product URL within the scoped capture')
    bench = sub.add_parser("benchmark")
    bench.add_argument("--source", type=Path, required=True)
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--repeats", type=int, default=3)
    bench.add_argument("--count", type=int, default=100)
    worker = sub.add_parser("storage-worker")
    worker.add_argument("--source", type=Path, required=True)
    worker.add_argument("--output", type=Path, required=True)
    worker.add_argument("--backend", choices=["json", "journal", "sqlite"], required=True)
    worker.add_argument("--count", type=int, default=100)
    network = sub.add_parser("transport-study")
    network.add_argument("--output", type=Path, required=True)
    network.add_argument("--methods", nargs="+", choices=["urllib", "pooled", "browser"], default=["urllib", "pooled"])
    network.add_argument('--plan', type=Path, help='Verified bounded URL plan; omitted uses the original six products')
    network.add_argument('--followup', type=Path, help='Prepared dependency follow-up; revalidates sources and inherits host waits')
    network.add_argument('--comparison', type=Path, help='Prepared candidate/comparator refresh with inherited discovery holds')
    network.add_argument('--budget', type=float, default=2100, help='One shared budget across all methods, up to 2100 seconds')
    followup = sub.add_parser('prepare-followup')
    followup.add_argument('--experiment', type=Path, required=True)
    followup.add_argument('--capture', type=Path, required=True)
    followup.add_argument('--output', type=Path, required=True)
    followup.add_argument('--task-id', action='append', dest='task_ids', required=True)
    apply = sub.add_parser('apply-followup')
    apply.add_argument('--intent', type=Path, required=True)
    apply.add_argument('--capture', type=Path, required=True)
    apply.add_argument('--output', type=Path, required=True)
    apply.add_argument('--method', choices=['urllib', 'pooled', 'browser'], default='urllib')
    apply.add_argument('--budget', type=float, default=120)
    apply.add_argument('--max-tasks', type=int, default=20)
    refresh = sub.add_parser('prepare-comparison')
    refresh.add_argument('--experiment', type=Path, required=True)
    refresh.add_argument('--capture', type=Path, required=True)
    refresh.add_argument('--output', type=Path, required=True)
    refresh.add_argument('--candidate-id', action='append', dest='candidate_ids', required=True)
    compare_apply = sub.add_parser('apply-comparison')
    compare_apply.add_argument('--intent', type=Path, required=True)
    compare_apply.add_argument('--capture', type=Path, required=True)
    compare_apply.add_argument('--output', type=Path, required=True)
    compare_apply.add_argument('--method', choices=['urllib', 'pooled', 'browser'], default='urllib')
    compare_apply.add_argument('--budget', type=float, default=120)
    compare_apply.add_argument('--max-tasks', type=int, default=20)
    queue = sub.add_parser("queue-study")
    queue.add_argument("--input", type=Path, required=True)
    queue.add_argument("--capture", type=Path, required=True)
    queue.add_argument("--snapshot", choices=["transport_failure", "tsukumo_recovery", "ark_repaired"], required=True)
    queue.add_argument("--output", type=Path, required=True)
    queue.add_argument("--architecture", choices=["A", "B", "C"], default="B")
    queue.add_argument("--method", choices=["urllib", "pooled"], default="urllib")
    queue.add_argument("--budget", type=int, default=120)
    queue.add_argument("--max-tasks", type=int, default=20)
    capture_check = sub.add_parser("verify-capture")
    capture_check.add_argument("--input", type=Path, required=True)
    capture_check.add_argument("--allow-partial", action="store_true")
    capture_replay = sub.add_parser("replay-capture")
    capture_replay.add_argument("--input", type=Path, required=True)
    capture_replay.add_argument("--output", type=Path, required=True)
    capture_replay.add_argument("--allow-partial", action="store_true")
    smoke = sub.add_parser("capture-smoke")
    smoke.add_argument("--output", type=Path, required=True)
    recovery = sub.add_parser("recover-capture")
    recovery.add_argument("--input", type=Path, required=True)
    recovery.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--export", type=Path, required=True)
    restore.add_argument("--backend", choices=["json", "journal", "sqlite"], required=True)
    restore.add_argument("--output", type=Path, required=True)
    legacy = sub.add_parser("export-legacy")
    legacy.add_argument("--experiment", type=Path, required=True)
    legacy.add_argument("--output", type=Path, required=True)
    legacy.add_argument("--project-observations", action="store_true")
    matrix = sub.add_parser("suite")
    matrix.add_argument("--input", type=Path, required=True)
    matrix.add_argument("--output", type=Path, required=True)
    matrix.add_argument("--repeats", type=int, default=2)
    timing = sub.add_parser("capture-timing")
    timing.add_argument("--input", type=Path, required=True)
    timing.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare-comparison':
        from .comparison import prepare_comparison
        result = prepare_comparison(args.experiment, args.capture, args.output, args.candidate_ids)
        print(json.dumps({'bundle_hash': result['bundle_hash'], 'resources': len(result['request_plan']['resources'])}))
    elif args.command == 'apply-comparison':
        from .comparison_apply import apply_comparison
        result = apply_comparison(args.intent, args.capture, args.output, method=args.method,
                                  budget=args.budget, max_tasks=args.max_tasks)
        print(json.dumps({k: result[k] for k in ('selected_statuses', 'new_phase_observations', 'http_requests')}))
    elif args.command == 'apply-followup':
        from .followup_apply import apply_followup
        result = apply_followup(args.intent, args.capture, args.output, method=args.method,
                                budget=args.budget, max_tasks=args.max_tasks)
        print(json.dumps({k: result[k] for k in ('selected_statuses', 'new_task_ids', 'new_phase_observations', 'http_requests')}))
    elif args.command == 'prepare-followup':
        from .followup import prepare_followup
        result = prepare_followup(args.experiment, args.capture, args.output, args.task_ids)
        print(json.dumps({'bundle_hash': result['bundle_hash'], 'resources': len(result['request_plan']['resources'])}))
    elif args.command == "queue-study":
        from .queue_study import queue_study
        result = queue_study(args.input, args.snapshot, args.capture, args.output, architecture=args.architecture,
                             method=args.method, budget=args.budget, max_tasks=args.max_tasks)
        print(json.dumps({key: result[key] for key in ("observations", "http_requests", "replayed_actual_attempts", "evidence_gaps")}))
    elif args.command == "recover-capture":
        from .capture import recover_capture
        print(json.dumps(recover_capture(args.input, args.output)))
    elif args.command == "verify-capture":
        from .capture import verify_capture
        result = verify_capture(args.input, allow_partial=args.allow_partial)
        print(json.dumps({"complete": result["manifest"]["complete"], "receipts": len(result["receipts"]),
                          "verified_bytes": result["verified_bytes"]}))
    elif args.command == "replay-capture":
        from .capture import replay_capture
        result = replay_capture(args.input, args.output, allow_partial=args.allow_partial)
        print(json.dumps({k: result[k] for k in ("mode", "http_requests", "parsed_pages", "source_complete")}))
    elif args.command == "capture-smoke":
        from .capture import smoke_capture
        print(json.dumps(smoke_capture(args.output)))
    elif args.command == "prepare":
        from .inputs import prepare
        result = prepare(args.output, args.saved_pages)
        print(json.dumps({"input_hash": result["input_hash"], "snapshots": list(result["snapshots"])}))
    elif args.command == "run":
        from .pipeline import run
        result = run(args.input, args.snapshot, args.architecture, args.mode, args.output, backend=args.backend,
                     transport=args.transport, budget=args.budget, cycles=args.cycles, max_tasks=args.max_tasks,
                     discovery_input=args.discovery_input, capture_input=args.capture_input,
                     capture_method=args.capture_method, candidate_urls=args.candidate_urls)
        print(json.dumps({k: result[k] for k in ("experiment_id", "observations", "decidable_candidates", "accepted_candidates", "wall_seconds")}, ensure_ascii=False))
    elif args.command == "capture-timing":
        from .operations import capture_timing
        result = capture_timing(args.input, args.output)
        print(json.dumps(result))
    elif args.command == "suite":
        from .suite import suite
        result = suite(args.input, args.output, args.repeats)
        print(json.dumps({"runs": len(result["results"]), "all_assertions_passed": result["all_assertions_passed"]}))
    elif args.command == "restore":
        from .migration import restore_export
        print(json.dumps(restore_export(args.export, args.backend, args.output)))
    elif args.command == "export-legacy":
        from .migration import export_legacy
        result = export_legacy(args.experiment, args.output, project_observations=args.project_observations)
        print(json.dumps({k: v for k, v in result.items() if k != "files"}))
    elif args.command == "transport-study":
        from .study import study
        result = study(args.output, args.methods, emit=lambda row: print("LAB_RECEIPT " + json.dumps(row, ensure_ascii=False), flush=True),
                       plan=args.plan, followup=args.followup, comparison=args.comparison, budget=args.budget)
        print("LAB_STUDY " + json.dumps({k: v for k, v in result.items() if k != "receipts"}, ensure_ascii=False))
    else:
        from .benchmark import benchmark, worker
        result = worker(args.backend, args.source, args.output, args.count) if args.command == "storage-worker" else benchmark(args.source, args.output, args.repeats, args.count)
        print(json.dumps(result.get("summary", {"roundtrip_equal": result.get("roundtrip_equal")}), ensure_ascii=False))


if __name__ == "__main__":
    main()
