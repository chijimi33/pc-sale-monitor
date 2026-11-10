"""Bound a CI collector process while keeping its last durable checkpoint.

The collector's own budget is cooperative: one blocking request can exceed it.
This outer deadline leaves time for the workflow's unconditional export/upload.
On POSIX, only the newly created child's process group is terminated. Windows
support is for a leaf worker; CI uses Linux to include browser descendants.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .models import STORES, iso
from .storage import atomic_json, read_json


def stop_owned_process(process, grace=5):
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # A descendant may ignore TERM after the group leader has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=5)


def mark_interrupted(root, store, run_id, timeout, elapsed, exit_code):
    record = {'run_id': run_id, 'reason': 'collector_wall_clock_timeout',
              'observed_at': iso(), 'deadline_seconds': timeout,
              'elapsed_seconds': round(elapsed, 3), 'child_exit_code': exit_code,
              'scope': 'last_durable_checkpoint; final_request_total_unknown'}
    path = Path(root) / 'stores' / (store + '.json')
    try:
        state = read_json(path, None)
    except (OSError, ValueError):
        return {**record, 'checkpoint_status': 'unreadable'}
    if not isinstance(state, dict) or state.get('store') != store or state.get('run_id') != run_id:
        return {**record, 'checkpoint_status': 'no_matching_current_checkpoint'}
    if not isinstance(state.get('queue'), dict) or not isinstance(state.get('offers'), dict) or not isinstance(state.get('journal'), list):
        return {**record, 'checkpoint_status': 'invalid_containers'}
    record.update(checkpoint_status='preserved', last_checkpoint_at=state.get('checkpoint_at'),
                  prior_status=state.get('status'), prior_cycle_complete=state.get('cycle_complete'))
    # Do not update observation/checkpoint time, queue ages, attempts, HTTP
    # totals, journals or done receipts. The interruption is a separate fact.
    state.update(status='interrupted', collection_deadline=record)
    atomic_json(path, state)
    return record


def supervise(command, root, store, run_id, timeout, stop_grace=5):
    started = time.monotonic()
    process = subprocess.Popen(command, start_new_session=os.name == 'posix')
    try:
        return process.wait(timeout=timeout), None
    except subprocess.TimeoutExpired:
        stop_owned_process(process, stop_grace)
        record = mark_interrupted(root, store, run_id, timeout,
                                  time.monotonic() - started, process.returncode)
        return 124, record
    except BaseException:
        stop_owned_process(process, stop_grace)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, choices=STORES)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--config', type=Path, default=Path('config/sources.json'))
    parser.add_argument('--seconds', type=int, default=2100)
    parser.add_argument('--deadline-seconds', type=int, default=2220)
    parser.add_argument('--transport-diagnostic', action='store_true')
    args = parser.parse_args(argv)
    if args.seconds < 0 or args.deadline_seconds <= args.seconds:
        parser.error('deadline-seconds must exceed the nonnegative collection seconds')
    if args.transport_diagnostic and args.store != 'tsukumo':
        parser.error('transport diagnostic requires tsukumo')
    command = [sys.executable, '-m', 'sale_monitor.cli', 'collect', '--store', args.store,
               '--state', str(args.state), '--run-id', args.run_id, '--config', str(args.config),
               '--seconds', str(args.seconds)]
    if args.transport_diagnostic:
        command.append('--transport-diagnostic')
    previous = None
    if os.name == 'posix':
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        code, record = supervise(command, args.state, args.store, args.run_id, args.deadline_seconds)
        if record is not None:
            print('PCSM_COLLECTION_DEADLINE ' + json.dumps(record), flush=True)
        return code
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
