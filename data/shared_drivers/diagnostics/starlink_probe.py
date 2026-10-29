#!/usr/bin/env python3
"""
Does the Starlink dish answer, and do we understand the answer? Read-only.

  python3 /data/shared_drivers/diagnostics/starlink_probe.py [dish address]

Tries the same three ways as the settings page (gRPC-web on 9201, gRPC-web on
port 80, plain gRPC on 9200) and prints what came back. If every way fails it
shows the first bytes of the answer, which is what is needed to fix the decoder.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import starlink  # noqa: E402


def candidates(host):
    web = ['--http1.1', '-H', 'Content-Type: application/grpc-web+proto', '-H', 'X-Grpc-Web: 1',
           '-H', 'Accept: application/grpc-web+proto']
    native = ['--http2-prior-knowledge', '-H', 'Content-Type: application/grpc', '-H', 'TE: trailers']
    path = starlink.SERVICE_PATH
    return [('gRPC-web port 9201', web, 'http://%s:9201%s' % (host, path)),
            ('gRPC-web port 80', web, 'http://%s%s' % (host, path)),
            ('gRPC port 9200', native, 'http://%s:9200%s' % (host, path))]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else '192.168.100.1'
    worked = False
    for name, args, url in candidates(host):
        print('== %s  %s' % (name, url))
        try:
            p = subprocess.run(['curl', '-s', '-m', '5', '-X', 'POST'] + args + ['--data-binary', '@-', url],
                               input=starlink.status_request(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=8)
        except (OSError, subprocess.SubprocessError) as e:
            print('   could not run curl: %s' % e)
            continue
        if p.returncode != 0:
            print('   curl exit code %d %s' % (p.returncode, p.stderr.decode('utf-8', 'replace').strip()))
            continue
        try:
            st = starlink.decode_reply(p.stdout)
        except ValueError as e:
            print('   answer not understood: %s' % e)
            print('   %d bytes, first 120: %s' % (len(p.stdout), p.stdout[:120].hex()))
            continue
        worked = True
        print('   OK')
        for k in sorted(st):
            print('   %-22s %s' % (k, st[k]))
        break
    if not worked:
        print('\nNo way worked. Is the dish reachable? Try: curl -s -m 5 -o /dev/null -w "%%{http_code}" http://%s/' % host)
    return 0 if worked else 1


if __name__ == '__main__':
    sys.exit(main())
