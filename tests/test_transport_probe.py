import unittest

from sale_monitor.http import FetchError
from tools.transport_probe import describe, probe, split_curl_headers


class TransportProbeTests(unittest.TestCase):
    def test_retry_after_on_success_is_kept_separate_from_product_body(self):
        body = b'<html>product</html>'
        headers, parsed = split_curl_headers(b'HTTP/1.1 200 OK\r\nRetry-After: 900\r\n\r\n' + body)
        self.assertEqual(headers['retry-after'], '900')
        self.assertEqual(parsed, body)

    def test_connect_and_informational_headers_do_not_hide_final_response(self):
        data = (b'HTTP/1.1 200 Connection established\r\n\r\n'
                b'HTTP/1.1 100 Continue\r\n\r\n'
                b'HTTP/2 429 Too Many Requests\r\nRetry-After: 600\r\n\r\nlimited')
        headers, body = split_curl_headers(data)
        self.assertEqual(headers['retry-after'], '600')
        self.assertEqual(body, b'limited')

    def test_ordinary_captcha_script_is_not_a_visible_challenge(self):
        ordinary = b'<script>verify you are human; recaptcha</script><h1>Product</h1>'
        self.assertFalse(describe(200, ordinary, 'text/html', 'https://example.com')['captcha_or_challenge_text'])
        self.assertTrue(describe(200, b'<h1>Verify you are human</h1>', 'text/html', 'https://example.com')['captcha_or_challenge_text'])

    def test_collector_error_records_actual_attempts_and_host_deadline(self):
        class Client:
            count = 7
            retry_after = {}
            transport_retry_after = {}

            def get(self, url):
                self.count += 3
                self.transport_retry_after['shop.tsukumo.co.jp'] = {'until': 9999999999}
                raise FetchError('RemoteDisconnected')

        result = probe('https://shop.tsukumo.co.jp/', 'collector_client', Client())
        self.assertEqual(result['attempts'], 3)
        self.assertEqual(result['error'], 'RemoteDisconnected')
        self.assertTrue(result['host_deferred'])


if __name__ == '__main__':
    unittest.main()
