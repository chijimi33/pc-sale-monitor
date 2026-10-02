"""Durable resource scheduling shared by the prototype and offline trace study."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
from urllib.parse import urlsplit

from sale_monitor.models import allowed_url
from .queueing import select_resource


def collect(store, client, run_id, allowed_urls, on_page, *, architecture, cycles, max_tasks,
            budget, hook=lambda stage: None):
    """Reserve bounded work before acquisition; checkpoint outcomes atomically.

    A reservation whose outcome was not committed remains explicitly unknown on
    restart. It still consumes a work slot, so repeated crashes cannot reset the
    request cap. The wall-clock deadline also survives a process restart.
    """
    if architecture not in {'A', 'B', 'C'} or cycles not in (1, 2) or not 1 <= max_tasks <= 20 or not 0 < budget <= 2100:
        raise ValueError('Experiment limit exceeded')
    options = {'architecture': architecture, 'cycles': cycles, 'max_tasks': max_tasks, 'budget_seconds': budget}
    records = store.snapshot()['records']
    tasks = records['tasks']
    control = deepcopy(records.get('scheduler', {}).get('collection'))

    def commit(txid, changes):
        store.commit(txid, changes)
        for namespace, key, value in changes:
            records.setdefault(namespace, {})[key] = deepcopy(value)

    if control is None:
        now = client.clock()
        control = {'schema': 1, 'options': options, 'started_at_epoch': now, 'deadline_epoch': now + budget,
                   'budget_seconds': budget, 'cycle': 0, 'cursor': 0, 'reserved_resources': 0,
                   'active': None, 'confirmed_http_requests': 0, 'last_by_host': {}, 'request_sequence': [], 'wait_count': 0}
        commit('schedule:init:' + run_id, [('scheduler', 'collection', control)])
    if (control.get('schema') != 1 or control.get('options') != options
            or not all(math.isfinite(control[key]) for key in ('started_at_epoch', 'deadline_epoch'))
            or abs(control['deadline_epoch'] - control['started_at_epoch'] - budget) > 0.00001):
        raise ValueError('Invalid persistent collection budget')
    if client.clock() < control['started_at_epoch']:
        raise ValueError('Wall clock moved before the original collection start')
    client.deadline = client.monotonic() + max(0, min(budget, control['deadline_epoch'] - client.clock()))
    client.last = deepcopy(control['last_by_host'])
    client.sequence = list(control['request_sequence'])
    client.hosts.update(deepcopy(records.get('hosts', {})))

    if control['active'] is not None:
        identity = control['active']
        interrupted = deepcopy(records['dispatches'][identity])
        if interrupted['state'] != 'reserved':
            raise ValueError('Active reservation has an inconsistent outcome')
        interrupted.update(state='interrupted_unknown', outcome='request_or_response_not_committed')
        host = urlsplit(interrupted['url']).hostname
        gate = deepcopy(client.hosts.get(host, {}))
        # This is a local restart precaution, not a claimed server response.
        if not gate.get('blocked'):
            gate.update(until=max(gate.get('until', 0), client.clock() + 60),
                        reason='interrupted_acquisition_unconfirmed', local_policy=True)
        client.hosts[host] = gate
        changes = [('hosts', host, gate), ('dispatches', identity, interrupted)]
        for key in interrupted['task_ids']:
            task = deepcopy(tasks[key])
            task.setdefault('lab_interruptions', []).append({'dispatch': identity, 'outcome': interrupted['outcome']})
            task['lab_status'] = 'waiting'
            changes.append(('tasks', key, task))
        control['active'] = None
        changes.append(('scheduler', 'collection', control))
        commit('schedule:interrupted:' + identity, changes)

    receipts, completed_times, dispatches = [], {}, []
    started = client.monotonic()
    stop_reason = 'cycles_complete'

    def selected_tasks():
        return {key: task for key, task in tasks.items() if task.get('lab_selected')
                and task.get('lab_kind') == 'product'
                and task.get('lab_status') not in {'complete', 'external_wait', 'evidence_wait'}
                and task.get('lab_ready_cycle', 0) <= control['cycle']}

    while control['cycle'] < cycles:
        if client.monotonic() >= client.deadline or client.clock() >= control['deadline_epoch']:
            stop_reason = 'budget_exhausted'
            break
        if control['reserved_resources'] >= max_tasks:
            stop_reason = 'resource_limit'
            break
        eligible = selected_tasks()
        if not eligible:
            control['cycle'] += 1
            commit(f"schedule:cycle:{run_id}:{control['cycle']}", [('scheduler', 'collection', control)])
            continue
        selection = select_resource(eligible, client.hosts, client.clock(), control['cursor'],
                                    policy='cyclic' if architecture == 'A' else 'dependent')
        if selection['resource'] is None:
            deadlines = [client.hosts[host]['until'] for host in selection['waiting_by_host']
                         if not client.hosts[host].get('blocked') and client.hosts[host].get('until', 0) > client.clock()]
            if not deadlines:
                stop_reason = 'blocked_hosts'
                break
            until = min(deadlines)
            remaining = client.deadline - client.monotonic()
            if until >= control['deadline_epoch'] or until - client.clock() >= remaining:
                stop_reason = 'wait_exceeds_remaining_budget'
                break
            wait = {'reason': 'shared_host_wait', 'started_at_epoch': client.clock(), 'until_epoch': until,
                    'waiting_by_host': selection['waiting_by_host'], 'waiting_tasks': selection['waiting_task_count']}
            control['wait_count'] += 1
            wait_id = f"{run_id}:{control['wait_count']}"
            wait['state'] = 'planned'
            commit('schedule:wait:' + wait_id, [('scheduler_waits', wait_id, wait), ('scheduler', 'collection', control)])
            client.sleep(max(0, until - client.clock()))
            wait.update(state='completed', finished_at_epoch=client.clock())
            commit('schedule:waited:' + wait_id, [('scheduler_waits', wait_id, wait)])
            continue
        keys = selection['task_ids']
        url = tasks[keys[0]]['url']
        if url not in allowed_urls or not allowed_url(url):
            raise ValueError('Acquisition outside the fixed allowlist')
        slot = control['reserved_resources'] + 1
        identity = f'{run_id}:{slot}'
        dispatch = {'state': 'reserved', 'slot': slot, 'cycle': control['cycle'], 'url': url, 'task_ids': keys,
                    'resource': selection['resource'], 'cursor_before': control['cursor'],
                    'cursor_after': selection['next_cursor'], 'started_at_epoch': client.clock()}
        control.update(reserved_resources=slot, cursor=selection['next_cursor'], active=identity)
        commit('reserve:' + identity, [('dispatches', identity, dispatch), ('scheduler', 'collection', control)])
        hook('after_reservation')
        previous_requests = client.requests
        page, receipt = client.fetch(url)
        hook('after_fetch')
        receipt_dict = asdict(receipt)
        receipt_dict['task_ids'] = keys
        receipts.append(receipt_dict)
        changes = []
        processing_error = None
        for key in keys:
            task = deepcopy(tasks[key])
            history = 'lab_evidence_gaps' if receipt.error == 'evidence_exhausted' else 'lab_attempts'
            task.setdefault(history, []).append(deepcopy(receipt_dict))
            task['lab_last_error'] = receipt.error
            if page is not None:
                try:
                    added = on_page(task, page, receipt_dict, tasks, control['cycle'], records)
                except Exception as error:
                    processing_error = error
                    task.update(lab_status='waiting', lab_last_error='normalization:' + type(error).__name__,
                                lab_ready_cycle=cycles)
                else:
                    changes.extend(added)
                    for namespace, child_key, value in added:
                        if namespace == 'tasks':
                            tasks[child_key] = deepcopy(value)
                    task['lab_status'] = 'complete'
                    completed_times[key] = client.monotonic() - started
            elif receipt.error == 'evidence_exhausted':
                task['lab_status'] = 'evidence_wait'
            else:
                task['lab_status'] = 'waiting'
                # Only a known finite host wait is retried in this collection.
                # 404 and other permanent errors remain pending, without a loop.
                gate = client.hosts.get(urlsplit(url).hostname, {})
                retryable = receipt.error in {'retry_after', 'server_error', 'shared_host_wait'} or (receipt.error or '').startswith('transport:')
                if not gate.get('blocked') and (not retryable or 'until' not in gate):
                    task['lab_ready_cycle'] = cycles
            tasks[key] = task
            changes.append(('tasks', key, task))
        dispatch.update(state='committed', receipt=receipt_dict, finished_at_epoch=client.clock(),
                        hosts_after=deepcopy(client.hosts),
                        processing_error=type(processing_error).__name__ if processing_error else None)
        control.update(active=None, confirmed_http_requests=control['confirmed_http_requests'] + client.requests - previous_requests,
                       last_by_host=deepcopy(client.last), request_sequence=list(client.sequence))
        changes += [('hosts', host, gate) for host, gate in client.hosts.items()]
        changes += [('dispatches', identity, dispatch), ('scheduler', 'collection', control)]
        # Every task sharing this resource, observations, wait state and cursor
        # become visible together. Failed normalizations never complete a task.
        commit('acquire:' + identity, changes)
        dispatches.append(dispatch)
        hook('after_checkpoint')
        if processing_error is not None:
            raise processing_error

    final_selection = select_resource(selected_tasks(), client.hosts, client.clock(), control['cursor'])
    return {'receipts': receipts, 'completed_task_seconds': completed_times,
            'scheduling': {'control': deepcopy(control), 'stop_reason': stop_reason,
                           'waits': deepcopy(list(records.get('scheduler_waits', {}).values())),
                           'dispatches_this_invocation': dispatches,
                           'waiting_by_host': final_selection['waiting_by_host'],
                           'waiting_task_count': final_selection['waiting_task_count']}}
