#!/usr/bin/env python3
"""Development server for the HamClock WebAssembly build.

  python3 devserver.py [--port 8000] [--bind 127.0.0.1]
                       [--backend-url URL | --backend-dir DIR]
                       [--xhttp-allow HOST ...] [--no-xhttp] [--allow-host NAME:PORT ...]

Serves this directory with the cross-origin isolation headers that SharedArrayBuffer (and so
HamClock's threads) require, plus two same-origin routes for the page:

/ohb/...    HamClock's backend. --backend-url proxies to a real backend (default
            http://ohb.hamclock.app), turning X-HamClock-UA back into User-Agent so the backend's
            statistics still work. --backend-dir serves static files instead (a mock or local tree).

/xhttp/HOST/PATH
            https://HOST/PATH for third-party APIs HamClock fetches that don't allow cross-origin
            requests: by default only hams.at and meshtastic.liamcottle.net (add more with
            --xhttp-allow). The Authorization header, which carries a hams.at API key, is passed
            on; X-HamClock-UA becomes User-Agent. Only this page can use it: requests must carry
            X-HamClock-Proxy, which other websites can't send without the browser asking first,
            and their Host must be this server (--allow-host adds names), which defeats DNS
            rebinding. Nothing about the request is logged except host, path and status.

Listens on 127.0.0.1 only unless --bind says otherwise. Browsers only run the page from
localhost or over HTTPS anyway, so other machines can't use a plain-HTTP server like this one.
"""
import argparse, http.server, os, sys, urllib.request, urllib.error

ap = argparse.ArgumentParser(description='HamClock WebAssembly development server')
ap.add_argument('--port', type=int, default=8000)
ap.add_argument('--bind', default='127.0.0.1', help='address to listen on (default 127.0.0.1)')
ap.add_argument('--backend-dir', help='serve /ohb from this directory instead of proxying')
ap.add_argument('--backend-url', help='backend to proxy /ohb to (default http://ohb.hamclock.app)')
ap.add_argument('--xhttp-allow', action='append', default=[], metavar='HOST',
                help='also allow /xhttp/HOST/... (default: hams.at, meshtastic.liamcottle.net)')
ap.add_argument('--no-xhttp', action='store_true', help='turn the /xhttp/ proxy off')
ap.add_argument('--xhttp-upstream', action='append', default=[], metavar='HOST=URL',
                help='send /xhttp/HOST/... to URL instead of https://HOST (testing, mirrors)')
ap.add_argument('--allow-host', action='append', default=[], metavar='NAME:PORT',
                help='extra Host header values to accept on /xhttp/, eg when using --bind')
args = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
XHTTP_HOSTS = set() if args.no_xhttp else \
    {'hams.at', 'meshtastic.liamcottle.net'} | {h.lower() for h in args.xhttp_allow}
XHTTP_UPSTREAM = dict(u.split('=', 1) for u in args.xhttp_upstream)
OUR_HOSTS = {f'{n}:{args.port}' for n in ('localhost', '127.0.0.1', '[::1]')} | set(args.allow_host)


def fetch(url, headers):
    """GET url, return (status, headers, body); upstream HTTP errors are passed through."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def end_headers(self):
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def guess_type(self, path):
        return 'application/wasm' if path.endswith('.wasm') else super().guess_type(path)

    def do_OPTIONS(self):
        # a preflight means another website is trying to use the proxies: never allow it
        self.send_error(403, 'cross-origin requests are not allowed')

    def do_GET(self):
        if self.path.startswith('/ohb/'):
            return self.backend(self.path[len('/ohb'):])
        if self.path.startswith('/xhttp/'):
            return self.xhttp(self.path[len('/xhttp/'):])
        return super().do_GET()

    def reply(self, status, hdrs, data):
        self.send_response(status)
        for k in ('Content-Type', 'Last-Modified'):
            if hdrs is not None and hdrs.get(k):
                self.send_header(k, hdrs[k])
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def backend(self, sub):
        ua = self.headers.get('X-HamClock-UA', '')
        sys.stderr.write(f'BACKEND {sub}  ua={"yes" if ua else "no"}\n')
        if args.backend_dir:
            fn = os.path.normpath(os.path.join(args.backend_dir, sub.split('?')[0].lstrip('/')))
            if fn.startswith(os.path.abspath(args.backend_dir)) and os.path.isfile(fn):
                with open(fn, 'rb') as f:
                    return self.reply(200, {'Content-Type': 'application/octet-stream'}, f.read())
            return self.send_error(404)
        headers = {'User-Agent': ua or 'HamClock-wasm'}
        if self.headers.get('If-Modified-Since'):
            headers['If-Modified-Since'] = self.headers['If-Modified-Since']
        try:
            self.reply(*fetch((args.backend_url or 'http://ohb.hamclock.app').rstrip('/') + sub, headers))
        except Exception as e:
            self.send_error(502, str(e))

    def xhttp(self, hostpath):
        # only this page may use the proxy
        why = None
        if not self.headers.get('X-HamClock-Proxy'):
            why = 'missing X-HamClock-Proxy'
        elif self.headers.get('Host', '') not in OUR_HOSTS:
            why = f'Host {self.headers.get("Host")!r} is not this server (see --allow-host)'
        elif self.headers.get('Origin') and self.headers['Origin'] != 'http://' + self.headers['Host']:
            why = 'cross-origin request'
        elif self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
            why = 'cross-site request'
        host, _, rest = hostpath.partition('/')
        host = host.lower()
        if not why and host not in XHTTP_HOSTS:
            why = f'{host} is not allowed (see --xhttp-allow)'
        if why:
            sys.stderr.write(f'XHTTP refused {host or "?"}: {why}\n')
            return self.send_error(403, why)

        headers = {'User-Agent': self.headers.get('X-HamClock-UA') or 'HamClock-wasm',
                   'Accept': self.headers.get('Accept', '*/*')}
        if self.headers.get('Authorization'):
            headers['Authorization'] = self.headers['Authorization']
        url = XHTTP_UPSTREAM.get(host, 'https://' + host).rstrip('/') + '/' + rest
        try:
            status, hdrs, data = fetch(url, headers)
        except Exception as e:
            sys.stderr.write(f'XHTTP {host}/{rest.split("?")[0]} failed: {e}\n')
            return self.send_error(502, str(e))
        sys.stderr.write(f'XHTTP {host}/{rest.split("?")[0]} -> {status}, {len(data)} bytes'
                         f'{", with Authorization" if "Authorization" in headers else ""}\n')
        self.reply(status, hdrs, data)


print(f'HamClock: http://localhost:{args.port}/   (listening on {args.bind}:{args.port})', flush=True)
http.server.ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
