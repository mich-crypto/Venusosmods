"""A made-up Starlink dish: answers the gRPC-web status request the way the real one does."""

import http.server
import os
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import starlink  # noqa: E402
import test_starlink as ts  # noqa: E402

TRAILER = b'grpc-status: 0\r\n'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    requests = []
    history_calls = 0

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        Handler.requests.append((self.path, self.headers.get('Content-Type'), body))
        if self.path == '/__up':                      # a Cloudflare-style upload target
            return self.reply(b'', 'text/plain')
        if self.path != starlink.SERVICE_PATH or body not in (starlink.status_request(), starlink.history_request()):
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if body == starlink.history_request():
            Handler.history_calls += 1
            msg = ts.history_message(current=1000 + 60 * Handler.history_calls,
                                     outages=[(6, int((time.time() - 120) * 1e9), 9 * 10 ** 9)])
            return self.reply(starlink.frame(msg) + b'\x80' + struct.pack('>I', len(TRAILER)) + TRAILER)
        n = len(Handler.requests)
        reply = starlink.frame(ts.status_message(alerts=(7,), obstructed=0.012 + 0.001 * (n % 5), latency=28 + n % 9)) \
            + b'\x80' + struct.pack('>I', len(TRAILER)) + TRAILER
        self.reply(reply)

    def reply(self, data, ctype='application/grpc-web+proto'):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith('/cdn-cgi/trace'):
            return self.reply(b'fl=1\nip=203.0.113.7\ncolo=AMS\nloc=NL\n', 'text/plain')
        if self.path.startswith('/__down?bytes='):
            return self.reply(bytes(int(self.path.split('=')[1])), 'application/octet-stream')
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(port):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
