from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sale_monitor.http import FetchError, Page
from tools.sofmap_search_probe import CaptureClient, HOST

URL = HOST+'/search_result.aspx?keyword=4711289500124'


class ProbeBounds(unittest.TestCase):
    def test_access_denial_stops_shared_host_without_trying_second_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CaptureClient(Path(tmp))
            with patch.object(client.inner, 'get', side_effect=FetchError('http_403')) as get:
                with self.assertRaisesRegex(FetchError,'http_403'):
                    client.get(URL)
                with self.assertRaisesRegex(FetchError,'shared_access_stop'):
                    client.get(HOST+'/search_result.aspx?keyword=0195553309745')
                self.assertEqual(1,get.call_count)
                self.assertEqual(1,len(client.receipts))

    def test_scope_deadline_and_resource_limit_prevent_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CaptureClient(Path(tmp))
            with patch.object(client.inner,'get') as get:
                with self.assertRaises(FetchError):
                    client.get(HOST+'/search_result.aspx?keyword=other')
                client.deadline = 0
                with self.assertRaisesRegex(FetchError,'budget_exhausted'):
                    client.get(URL)
                client.receipts = [{}]*8
                with self.assertRaisesRegex(FetchError,'outside_scope'):
                    client.get(URL)
                get.assert_not_called()

    def test_retained_body_is_exact_and_attempt_count_is_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = CaptureClient(Path(tmp))
            response = Page(URL,b'actual bytes','2026-10-04T00:00:00+00:00')
            def get(url):
                client.inner.count += 2
                return response
            with patch.object(client.inner,'get',side_effect=get):
                self.assertIs(response,client.get(URL))
            record = client.receipts[0]
            self.assertEqual(2,record['http_attempts'])
            self.assertEqual(response.body,(Path(tmp)/record['body_file']).read_bytes())


if __name__ == '__main__':
    unittest.main()
