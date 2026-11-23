"""Offline urllib compatibility: real handlers, no TCP or TLS handshake."""
import base64
from io import BytesIO
import ssl
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import HTTPSHandler

from monitor_lab.acquire import UrllibTransport
from monitor_lab.tests.test_lab import FakeClock
from monitor_lab.tests.test_transport_deadline import WireSocket, native_transport, wire


class ResponseSocket(WireSocket):
    """Give CONNECT and the origin response their own closable file objects."""

    def __init__(self, *responses):
        super().__init__(None)
        self.responses = iter(responses)

    def makefile(self, mode):
        assert mode == 'rb'
        return BytesIO(next(self.responses))

    def setsockopt(self, *args):
        pass


def tls_settings(context):
    return {name: getattr(context, name) for name in (
        'protocol', 'verify_mode', 'check_hostname', 'verify_flags',
        'minimum_version', 'maximum_version', 'options', 'post_handshake_auth')}


class TransportCompatibilityTest(unittest.TestCase):
    def setUp(self):
        # Supply process-local discovery results; leave build_opener's default
        # ProxyHandler and its routing logic intact, independent of this PC.
        self.proxies = self.enterContext(patch('urllib.request.getproxies', return_value={}))
        self.bypass = self.enterContext(patch('urllib.request.proxy_bypass', return_value=False))
        self.dial = self.enterContext(patch(
            'socket.create_connection', side_effect=AssertionError('network forbidden')))
        # Keep native HTTPSConnection.__init__/connect and the real SSLContext.
        # Only the handshake boundary returns an in-memory socket.
        self.tls = self.enterContext(patch.object(
            ssl.SSLContext, 'wrap_socket', autospec=True,
            side_effect=lambda context, sock, **kwargs: sock))

    def transport(self):
        transport = UrllibTransport(FakeClock())
        self.addCleanup(transport.close)
        return transport

    def respond_with(self, *responses):
        sock = ResponseSocket(*responses)
        # A retry or second connection fails offline instead of sharing a socket.
        self.dial.reset_mock()
        self.dial.side_effect = [sock]
        return sock

    def test_redirect_status_and_location_return_without_following(self):
        for scheme in ('http', 'https'):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(scheme=scheme, status=status):
                    location = 'https://other.invalid/redirect-target'
                    data, _ = wire(b'moved', f'Location: {location}\r\nContent-Length: 5', status)
                    first = WireSocket(BytesIO(data))
                    unused = WireSocket(BytesIO(wire(b'followed')[0]))
                    with native_transport('urllib', FakeClock(), [first, unused]) as (transport, connections):
                        result = transport.get(f'{scheme}://origin.invalid/start', 5)
                    self.assertEqual(status, result.status)
                    self.assertEqual(location, result.headers['Location'])
                    self.assertEqual(b'moved', result.body)
                    self.assertEqual(1, len(connections))
                    self.assertTrue(b''.join(first.sent).startswith(b'GET /start HTTP/1.1\r\n'))
                    self.assertEqual([], unused.sent)
        self.dial.assert_not_called()

    def test_default_http_proxy_routes_absolute_uri_and_honors_bypass(self):
        self.proxies.return_value = {'http': 'http://proxy.invalid:3128'}
        url = 'http://origin.invalid:8080/items?q=ssd'
        for bypass in (False, True):
            with self.subTest(bypass=bypass):
                self.bypass.return_value = bypass
                self.bypass.reset_mock()
                sock = self.respond_with(wire(b'ok')[0])
                result = self.transport().get(url, 5)
                self.assertEqual((200, b'ok'), (result.status, result.body))
                self.dial.assert_called_once()
                self.assertEqual(('origin.invalid', 8080) if bypass else ('proxy.invalid', 3128),
                                 self.dial.call_args.args[0])
                self.bypass.assert_called_once_with('origin.invalid:8080')
                request = b''.join(sock.sent)
                target = b'/items?q=ssd' if bypass else url.encode('ascii')
                self.assertTrue(request.startswith(b'GET ' + target + b' HTTP/1.1\r\n'))
                self.assertIn(b'\r\nHost: origin.invalid:8080\r\n', request)
                self.assertNotIn(b'CONNECT ', request)
        self.tls.assert_not_called()

    def test_default_https_proxy_connects_then_wraps_origin_without_leaking_proxy_auth(self):
        # Deliberately synthetic credentials exercise urllib's CONNECT isolation.
        self.proxies.return_value = {'https': 'http://test-user:test-password@proxy.invalid:3128'}
        sock = self.respond_with(b'HTTP/1.1 200 Connection established\r\n\r\n', wire(b'ok')[0])

        def wrap_after_connect(context, connected, **kwargs):
            self.assertIs(sock, connected)
            self.assertEqual(1, len(sock.sent), 'CONNECT must precede TLS and the origin GET')
            return connected

        self.tls.side_effect = wrap_after_connect
        result = self.transport().get('https://origin.invalid:8443/items?q=ssd', 5)
        self.assertEqual((200, b'ok'), (result.status, result.body))
        self.dial.assert_called_once()
        self.assertEqual(('proxy.invalid', 3128), self.dial.call_args.args[0])
        self.assertEqual(2, len(sock.sent))
        tunnel, request = sock.sent
        self.assertTrue(tunnel.startswith(b'CONNECT origin.invalid:8443 HTTP/1.1\r\n'))
        self.assertIn(b'\r\nHost: origin.invalid:8443\r\n', tunnel)
        token = base64.b64encode(b'test-user:test-password')
        self.assertIn(b'\r\nProxy-Authorization: Basic ' + token + b'\r\n', tunnel)
        self.assertTrue(request.startswith(b'GET /items?q=ssd HTTP/1.1\r\n'))
        self.assertIn(b'\r\nHost: origin.invalid:8443\r\n', request)
        self.assertNotIn(b'proxy-authorization', request.lower())
        self.assertNotIn(token, request)
        self.tls.assert_called_once()
        self.assertEqual('origin.invalid', self.tls.call_args.kwargs['server_hostname'])

    def test_default_tls_context_reaches_native_connection_and_verification_errors_surface(self):
        expected = tls_settings(HTTPSHandler()._context)
        for reject_certificate in (False, True):
            with self.subTest(reject_certificate=reject_certificate):
                self.tls.reset_mock()
                transport = self.transport()
                contexts = [handler._context for handler in transport.opener.handlers
                            if isinstance(handler, HTTPSHandler)]
                self.assertEqual(1, len(contexts))
                context = contexts[0]
                self.assertEqual(expected, tls_settings(context))
                self.assertEqual(ssl.PROTOCOL_TLS_CLIENT, context.protocol)
                self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
                self.assertTrue(context.check_hostname)
                sock = self.respond_with(wire(b'ok')[0])
                if reject_certificate:
                    failure = ssl.SSLCertVerificationError(1, 'synthetic certificate rejection')
                    self.tls.side_effect = failure
                    with self.assertRaises(URLError) as raised:
                        transport.get('https://origin.invalid/items', 5)
                    self.assertIs(failure, raised.exception.reason)
                    self.assertEqual([], sock.sent)
                    self.assertTrue(sock.closed)
                else:
                    self.tls.side_effect = lambda context, connected, **kwargs: connected
                    result = transport.get('https://origin.invalid/items', 5)
                    self.assertEqual((200, b'ok'), (result.status, result.body))
                self.dial.assert_called_once()
                self.assertEqual(('origin.invalid', 443), self.dial.call_args.args[0])
                self.tls.assert_called_once()
                self.assertIs(context, self.tls.call_args.args[0])
                self.assertIs(sock, self.tls.call_args.args[1])
                self.assertEqual('origin.invalid', self.tls.call_args.kwargs['server_hostname'])
                self.assertEqual(expected, tls_settings(context))


if __name__ == '__main__':
    unittest.main()
