"""A made-up Starlink dish: answers the gRPC-web status request the way the real one does."""

import http.server
import os
import struct
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'data', 'shared_drivers'))

import starlink  # noqa: E402
import test_starlink as ts  # noqa: E402

TRAILER = b'grpc-status: 0\r\n'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    requests = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        Handler.requests.append((self.path, self.headers.get('Content-Type'), body))
        if self.path != starlink.SERVICE_PATH or body != starlink.status_request():
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        n = len(Handler.requests)
        reply = starlink.frame(ts.status_message(alerts=(7,), obstructed=0.012 + 0.001 * (n % 5), latency=28 + n % 9)) \
            + b'\x80' + struct.pack('>I', len(TRAILER)) + TRAILER
        self.send_response(200)
        self.send_header('Content-Type', 'application/grpc-web+proto')
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *a):
        pass


def serve(port):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
