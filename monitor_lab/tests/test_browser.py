"""Browser response provenance, with no external network or browser install."""
from dataclasses import asdict
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from monitor_lab.acquire import MAX_BODY, BrowserTransport, Coordinator
from monitor_lab.capture import CHECKPOINT_FORMAT, Capture, replay_capture, verify_capture
from monitor_lab.evidence import normalize
from monitor_lab.safety import allowed_root, digest, read, write

URL = 'https://www.pc-koubou.jp/products/detail.php?product_id=1'
RAW = '<html><h1>未加工</h1></html>'.encode('shift_jis')
DOM = '<html><h1>表示後の商品</h1><input id="priceIncTax" value="10980"></html>'


class Response:
    def __init__(self, url=URL, status=200, body=RAW, headers=None, navigation=True):
        self.url, self.status, self._body = url, status, body
        self.headers = headers or {'content-type': 'text/html; charset=shift_jis'}
        self.request = SimpleNamespace(url=url, frame=None, method='GET', resource_type='document' if navigation else 'fetch',
                                       is_navigation_request=lambda: navigation)

    def body(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def all_headers(self):
        return self.headers


class FakePage:
    def __init__(self, root, auxiliary=(), fail=None, unfinished=(), dom=DOM):
        self.root, self.auxiliary, self.fail, self.unfinished, self.dom = root, auxiliary, fail, unfinished, dom
        self.main_frame = object()
        self.url, self.events = URL, {}
        for response in [root, *auxiliary]:
            if response:
                response.request.frame = self.main_frame

    def on(self, name, callback):
        self.events[name] = callback

    def goto(self, *args, **kwargs):
        for response in [self.root, *self.auxiliary]:
            if response:
                self.events.get('response', lambda r: None)(response)
                if response not in self.unfinished:
                    self.events.get('requestfinished', lambda r: None)(response.request)
        if self.fail:
            raise self.fail
        return self.root

    def content(self):
        if isinstance(self.dom, Exception):
            raise self.dom
        return self.dom


class FakeContext:
    def __init__(self, page):
        self.page, self.closed = page, False
    def route(self, pattern, callback):
        self.route_request = callback
    def route_web_socket(self, pattern, callback):
        self.route_socket = callback
    def new_page(self):
        return self.page
    def close(self):
        self.closed = True


class BrowserTest(unittest.TestCase):
    def setUp(self):
        self.directory = allowed_root() / 'tests' / ('browser-' + uuid.uuid4().hex)

    def client(self, page, capture=None):
        self.context = FakeContext(page)
        self.options = {}
        def new_context(**kwargs):
            self.options.update(kwargs)
            return self.context
        transport = BrowserTransport()
        transport.worker = object()
        transport.browser = SimpleNamespace(new_context=new_context)
        return Coordinator(transport, delay=0, capture=capture.body if capture else None)

    def test_auxiliary_403_does_not_replace_main_response(self):
        aux = Response(URL + '&aux=1', 403, b'actual auxiliary denial', {}, False)
        client = self.client(FakePage(Response(), [aux]))
        page, receipt = client.fetch(URL)
        self.assertEqual(200, receipt.status)
        self.assertEqual(hashlib.sha256(RAW).hexdigest(), receipt.body_sha256)
        self.assertIsNone(page)
        self.assertTrue(client.hosts['www.pc-koubou.jp']['blocked'])

    def test_http_hash_is_not_the_rendered_dom_hash(self):
        page, receipt = self.client(FakePage(Response())).fetch(URL)
        self.assertIsNotNone(page)
        self.assertEqual(hashlib.sha256(RAW).hexdigest(), receipt.body_sha256)
        self.assertEqual(DOM.encode(), page.body)
        self.assertEqual('browser_response_body', receipt.body_kind)

    def test_rendered_dom_uses_utf8_instead_of_http_charset(self):
        page, receipt = self.client(FakePage(Response())).fetch(URL)
        self.assertEqual(DOM, page.text)

    def record(self, page):
        capture = Capture(self.directory / 'capture', {'experiment_id': 'lab-browser', 'mode': 'fixture'},
                          {'stores': {'koubou': {}}})
        client = self.client(page, capture)
        parsed, receipt = client.fetch(URL)
        capture.append({'store': 'koubou', **asdict(receipt)}, client.hosts)
        capture.checkpoint(result={'experiment_id': 'lab-browser', 'mode': 'fixture', 'receipts': capture.receipts})
        return capture, client, parsed, receipt

    def test_distinct_bodies_survive_capture_replay_and_field_citations(self):
        capture, client, page, receipt = self.record(FakePage(Response()))
        verified = verify_capture(capture.root)
        self.assertEqual(RAW, (capture.root / receipt.body_file).read_bytes())
        self.assertEqual(DOM.encode(), (capture.root / receipt.rendered_dom['body_file']).read_bytes())
        self.assertEqual(2, len(list((capture.root / 'bodies').glob('*.body'))))
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No HTTP replay')):
            replay = replay_capture(capture.root, self.directory / 'replay')
        self.assertEqual(1, replay['parsed_pages'])
        self.assertEqual(0, replay['http_requests'])
        replay_observation = replay['observations'][0]['observation']
        live_observation = normalize('koubou', page, {}, 'lab-browser', asdict(receipt)).to_dict()
        for name, field in live_observation['fields'].items():
            self.assertEqual(field['selected_value'], replay_observation['fields'][name]['selected_value'])
        self.assertEqual('表示後の商品', replay_observation['offer']['title'])
        for field in replay_observation['fields'].values():
            source = field['sources'][0]
            self.assertEqual('rendered_dom', source['body_kind'])
            self.assertEqual(hashlib.sha256(DOM.encode()).hexdigest(), source['body_sha256'])

    def test_auxiliary_body_and_gate_source_survive_without_overwriting_root(self):
        aux = Response(URL + '&aux=1', 403, b'actual denial', {'set-cookie': 'never store', 'content-type': 'text/plain'}, False)
        capture, client, page, receipt = self.record(FakePage(Response(), [aux]))
        row = verify_capture(capture.root)['receipts'][0]
        self.assertEqual(200, row['status'])
        self.assertEqual(b'actual denial', (capture.root / row['auxiliary_responses'][0]['body_file']).read_bytes())
        self.assertNotIn('set-cookie', row['auxiliary_responses'][0]['headers'])
        self.assertEqual(aux.url, client.hosts['www.pc-koubou.jp']['source_url'])
        _, skipped = client.fetch(URL + '&next=1')
        self.assertEqual('shared_host_wait', skipped.error)
        self.assertEqual(1, client.requests)
        self.assertEqual(0, replay_capture(capture.root, self.directory / 'replay')['parsed_pages'])

    def test_auxiliary_retry_uses_longest_wait_and_preserves_each_response(self):
        responses = [Response(URL + '&aux=' + str(i), status, b'wait', {'retry-after': retry}, False)
                     for i, (status, retry) in enumerate(((200, '0'), (429, '120'), (200, '60')))]
        client = self.client(FakePage(Response(), responses))
        page, receipt = client.fetch(URL)
        self.assertIsNone(page)
        self.assertEqual(200, receipt.status)
        self.assertEqual('retry_after', receipt.error)
        self.assertEqual('120', client.hosts['www.pc-koubou.jp']['retry_after'])
        self.assertEqual(3, len(receipt.auxiliary_responses))

    def test_unfinished_auxiliary_response_retains_headers_without_fake_body(self):
        aux = Response(URL + '&aux=1', 429, AssertionError('Must not read unfinished body'), {'retry-after': '60'}, False)
        capture, client, page, receipt = self.record(FakePage(Response(), [aux], unfinished=[aux]))
        row = verify_capture(capture.root)['receipts'][0]['auxiliary_responses'][0]
        self.assertEqual('response_not_finished', row['body_unavailable'])
        self.assertIsNone(row['body_sha256'])
        self.assertIsNone(row['body_file'])
        self.assertEqual(200, receipt.status)

    def test_navigation_timeout_retains_received_headers_and_missing_body_reason(self):
        root = Response(body=AssertionError('Must not read unfinished body'))
        capture, client, page, receipt = self.record(FakePage(root, fail=TimeoutError(), unfinished=[root]))
        self.assertIsNone(page)
        self.assertEqual('browser_navigation:TimeoutError', receipt.error)
        self.assertEqual(200, receipt.status)
        self.assertIsNone(receipt.body_sha256)
        self.assertEqual('response_not_finished', receipt.body_unavailable)
        self.assertTrue(verify_capture(capture.root)['manifest']['complete'])
        self.assertEqual(0, replay_capture(capture.root, self.directory / 'replay')['parsed_pages'])

    def test_browser_body_and_dom_failures_are_explicit_without_http_fabrication(self):
        for index, page in enumerate([FakePage(Response(body=RuntimeError())), FakePage(Response(), dom=RuntimeError()), FakePage(None)]):
            client = self.client(page)
            result, receipt = client.fetch(URL)
            self.assertIsNone(result)
            self.assertTrue(receipt.error.startswith('browser_'))
            self.assertNotIn('transport:', receipt.error)
            self.assertTrue(self.context.closed)
            if index != 1:
                self.assertIsNone(receipt.body_sha256)

    def test_oversized_dom_is_retained_as_prefix_and_never_parsed(self):
        capture, client, page, receipt = self.record(FakePage(Response(), dom='x' * (MAX_BODY + 100)))
        self.assertIsNone(page)
        self.assertEqual('browser_dom_limit_exceeded', receipt.error)
        self.assertTrue(receipt.rendered_dom['body_incomplete'])
        self.assertEqual(MAX_BODY + 1, receipt.rendered_dom['body_bytes'])
        self.assertEqual(0, replay_capture(capture.root, self.directory / 'replay')['parsed_pages'])

    def test_redirected_document_cannot_be_claimed_as_original_product(self):
        page, receipt = self.client(FakePage(Response(url=URL + '&redirected=1'))).fetch(URL)
        self.assertIsNone(page)
        self.assertEqual('browser_response_url_mismatch', receipt.error)
        self.assertEqual(URL + '&redirected=1', receipt.response_url)

    def test_capture_failure_for_dom_remains_a_local_io_failure(self):
        client = self.client(FakePage(Response()))
        def store(record, body):
            if record.body_kind == 'rendered_dom':
                raise OSError('DOM disk failure')
            return 'raw.body'
        client.capture = store
        with self.assertRaisesRegex(OSError, 'DOM disk failure'):
            client.fetch(URL)
        self.assertEqual({}, client.hosts)

    def test_navigation_routing_and_sockets_cannot_bypass_same_host_policy(self):
        page = FakePage(Response())
        client = self.client(page)
        client.fetch(URL)
        self.assertEqual('block', self.options['service_workers'])
        outcomes = []
        for url, navigation, method, frame in [(URL, True, 'GET', page.main_frame), (URL, True, 'GET', page.main_frame),
                ('https://outside.invalid/', False, 'GET', page.main_frame), (URL, False, 'POST', page.main_frame),
                (URL, False, 'GET', page.main_frame), (URL, True, 'GET', object())]:
            request = SimpleNamespace(url=url, method=method, frame=frame, resource_type='document',
                                      is_navigation_request=lambda value=navigation: value)
            self.context.route_request(SimpleNamespace(request=request, continue_=lambda: outcomes.append('continue'),
                                                        abort=lambda: outcomes.append('abort')))
        self.assertEqual(['continue', 'abort', 'abort', 'abort', 'continue', 'abort'], outcomes)
        self.context.route_socket(SimpleNamespace(url='wss://outside.invalid/', close=lambda: outcomes.append('socket-closed')))
        self.assertEqual('socket-closed', outcomes[-1])

    def test_legacy_browser_bytes_verify_but_are_not_relabelled_as_http_or_dom(self):
        capture, client, page, receipt = self.record(FakePage(Response()))
        manifest = read(capture.root / 'capture-manifest.json')
        manifest['format'] = CHECKPOINT_FORMAT
        write(capture.root / 'capture-manifest.json', manifest)
        self.assertEqual(1, len(verify_capture(capture.root)['receipts']))
        result = replay_capture(capture.root, self.directory / 'replay')
        self.assertEqual(0, result['parsed_pages'])
        self.assertEqual('legacy_browser_representation_ambiguous', result['observations'][0]['skip_reason'])

    def test_missing_dom_file_fails_verification_before_replay(self):
        capture, client, page, receipt = self.record(FakePage(Response()))
        (capture.root / receipt.rendered_dom['body_file']).unlink()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            replay_capture(capture.root, self.directory / 'replay')
        self.assertFalse((self.directory / 'replay').exists())

    def test_browser_startup_failure_is_archivable_without_http_response(self):
        capture = Capture(self.directory / 'capture', {'experiment_id': 'lab-browser', 'mode': 'fixture'}, {})
        client = self.client(FakePage(Response()), capture)
        client.transport.browser.new_context = lambda **kw: (_ for _ in ()).throw(RuntimeError('Unavailable browser'))
        page, receipt = client.fetch(URL)
        self.assertEqual('browser_runtime:RuntimeError', receipt.error)
        self.assertIsNone(receipt.status)
        self.assertIsNone(receipt.body_sha256)
        capture.append({'store': 'koubou', **asdict(receipt)}, client.hosts)
        capture.checkpoint(result={'experiment_id': 'lab-browser', 'mode': 'fixture', 'receipts': capture.receipts})
        self.assertTrue(verify_capture(capture.root)['manifest']['complete'])

    def test_denial_arriving_during_dom_serialization_is_still_a_gate(self):
        page = FakePage(Response())
        aux = Response(URL + '&late=1', 403, b'late denial', {}, False)
        def content():
            page.events['response'](aux)
            page.events['requestfinished'](aux.request)
            return DOM
        page.content = content
        result, receipt = self.client(page).fetch(URL)
        self.assertIsNone(result)
        self.assertEqual('authentication_or_challenge', receipt.error)
        self.assertEqual(aux.url, receipt.auxiliary_responses[0]['url'])

    def test_recomputed_index_cannot_relabel_browser_api_bytes_as_original_http(self):
        capture, client, page, receipt = self.record(FakePage(Response()))
        manifest = read(capture.root / 'capture-manifest.json')
        for key in ('receipts', 'result'):
            file = capture.root / manifest['records'][key]
            content = read(file)
            rows = content if key == 'receipts' else content['receipts']
            rows[0]['body_kind'] = 'http_response'
            write(file, content)
        for entry in manifest['files']:
            body = (capture.root / entry['path']).read_bytes()
            entry.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        manifest['files_sha256'] = digest(manifest['files'])
        write(capture.root / 'capture-manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'Invalid browser representation'):
            verify_capture(capture.root)

    def test_evidence_limit_cannot_publish_an_unreadable_checkpoint(self):
        capture = Capture(self.directory / 'capture', {'experiment_id': 'lab-browser', 'mode': 'fixture'}, {})
        original = (capture.root / 'capture-manifest.json').read_bytes()
        client = self.client(FakePage(Response()), capture)
        _, receipt = client.fetch(URL)
        with patch('monitor_lab.capture.MAX_FILES', 5):
            with self.assertRaisesRegex(ValueError, 'previous checkpoint is preserved'):
                capture.append({'store': 'koubou', **asdict(receipt)}, client.hosts)
        self.assertEqual(original, (capture.root / 'capture-manifest.json').read_bytes())
        self.assertEqual([], verify_capture(capture.root, allow_partial=True)['receipts'])


if __name__ == '__main__':
    unittest.main()
