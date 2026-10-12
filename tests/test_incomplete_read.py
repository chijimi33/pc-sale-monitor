from contextlib import ExitStack
from copy import deepcopy
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sale_monitor.http import Client, FetchError
from sale_monitor.models import Offer, iso
from sale_monitor.reporting import health
from sale_monitor.runner import Collector
from test_retrying import Clock, NOW, OLD


URL = 'https://www.ark-pc.co.jp/i/50287598/'
CFG = {'stores': {'ark': {'adapter': 'html', 'seed_urls': []}}}
BODY = b'<h1>Test product</h1><script type="application/ld+json">' \
    b'{"@type":"Product","name":"Test product","sku":"MODEL-1","offers":' \
    b'{"price":8500,"priceCurrency":"JPY","availability":"https://schema.org/InStock"}}</script>'


def response(body=BODY, partial=False):
    result = MagicMock()
    result.__enter__.return_value = result
    result.url = URL; result.status = 200; result.headers = {}
    if partial:
        result.read.side_effect = IncompleteRead(b'private-partial-body', 100)
    else:
        result.read.return_value = body
    return result


class IncompleteResponseTests(unittest.TestCase):
    def test_partial_body_is_discarded_and_each_attempt_is_accounted_for(self):
        client = Client(delay=0)
        with patch.object(client.opener, 'open', side_effect=[response(partial=True), response()]), \
                patch('sale_monitor.http.time.sleep'):
            page = client.get(URL)
        self.assertEqual(page.body, BODY)
        evidence = client.evidence.snapshot()
        self.assertEqual(client.count, 2)
        self.assertEqual(evidence['exception_counts']['http'], {'IncompleteRead': 1})
        self.assertEqual(evidence['response_status_counts']['http'], {'200': 1})
        self.assertNotIn('private', json.dumps(evidence))

    def test_exhaustion_is_bounded_without_claiming_an_http_success(self):
        client = Client(delay=0)
        with patch.object(client.opener, 'open', side_effect=[response(partial=True) for _ in range(3)]) as network, \
                patch('sale_monitor.http.time.sleep'):
            with self.assertRaisesRegex(FetchError, '^IncompleteRead$') as raised:
                client.get(URL)
        self.assertIsNone(raised.exception.page)
        self.assertEqual(network.call_count, 3)
        evidence = client.evidence.snapshot()
        self.assertEqual(client.count, 3)
        self.assertEqual(evidence['exception_counts']['http'], {'IncompleteRead': 3})
        self.assertEqual(evidence['response_status_counts']['http'], {})
        self.assertEqual(evidence['failure_observations'], 3)
        self.assertEqual(client.transport_retry_after, {})
        self.assertNotIn('private', json.dumps(evidence))

    def test_restart_keeps_age_history_and_retry_until_complete_observation(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            clock = Clock(); clock.patch(stack)
            root = Path(tmp); client = Client(delay=0)
            client.opener.open = MagicMock(side_effect=[response(partial=True) for _ in range(3)])
            collector = Collector(root, 'ark', CFG, 'r1', client)
            collector.seed = lambda: None
            previous = Offer('ark', '50287598', URL, price_yen=9000,
                             stock='in_stock', observed_at=iso(NOW), observed_run_id='older')
            collector.state['offers'][previous.key] = previous.to_dict()
            collector.state['journal'] = [{'observation_id': 'retained', 'run_id': 'older'}]
            journal = deepcopy(collector.state['journal'])
            task_id = collector.enqueue({'type': 'product', 'url': URL, 'created_at': OLD, 'attempts': 7})
            result = collector.collect(seconds=60)
            task = result['queue'][task_id]
            self.assertEqual((task['created_at'], task['attempts']), (OLD, 8))
            self.assertEqual(task['last_error'], 'IncompleteRead')
            deadline = task['retry_at_epoch_seconds']
            self.assertEqual(deadline, clock.now + 300)
            self.assertEqual(health(result, NOW)['current_offers'], 0)
            self.assertEqual(result['journal'], journal)
            self.assertEqual(result['offers'][previous.key]['price_yen'], 9000)
            self.assertEqual(client.count, 3)

            recovered_client = Client(delay=0)
            recovered_client.opener.open = MagicMock(return_value=response())
            resumed = Collector(root, 'ark', CFG, 'r1', recovered_client)
            resumed.seed = lambda: None
            clock.now = deadline - 100
            waiting = resumed.collect(seconds=60)
            self.assertEqual(waiting['queue'][task_id], task)
            self.assertEqual(waiting['journal'], journal)
            recovered_client.opener.open.assert_not_called()
            clock.now = deadline
            recovered = resumed.collect(seconds=60)
            self.assertEqual(recovered['queue'], {})
            self.assertIn(task_id, recovered['done'])
            self.assertEqual(recovered['journal'][:1], journal)
            self.assertEqual(len(recovered['journal']), 2)
            self.assertEqual(recovered['journal'][-1]['offer']['price_yen'], 8500)
            self.assertEqual(recovered_client.count, 1)
            self.assertEqual(recovered['retry_activity']['recovered_tasks'], 1)


if __name__ == '__main__':
    unittest.main()
