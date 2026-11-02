#!/usr/bin/env python3
"""Development server for the HamClock WebAssembly build.

Serves this directory with the cross-origin isolation headers that SharedArrayBuffer (and so
pthreads) require, and mounts a backend under /ohb so requests are same-origin:

  python3 devserver.py [--port 8000] [--backend-dir DIR | --backend-url URL]

--backend-url proxies to a real backend (default http://ohb.hamclock.app) and forwards
X-HamClock-UA as User-Agent, so the backend's statistics still work. --backend-dir serves static
files instead (a mock or a local OHB tree).
"""
import argparse, http.server, os, sys, urllib.request, urllib.error

ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, default=8000)
ap.add_argument('--backend-dir')
ap.add_argument('--backend-url')
args = ap.parse_args()
HERE = os.path.dirname(os.path.abspath(__file__))


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

    def do_GET(self):
        if not self.path.startswith('/ohb/'):
            return super().do_GET()
        sub = self.path[len('/ohb'):]
        ua = self.headers.get('X-HamClock-UA', '')
        sys.stderr.write(f'BACKEND {sub}  ua={"yes" if ua else "no"}\n')
        if not args.backend_dir:
            return self.proxy(sub, ua)
        else:
            fn = os.path.normpath(os.path.join(args.backend_dir, sub.split('?')[0].lstrip('/')))
            if fn.startswith(os.path.abspath(args.backend_dir)) and os.path.isfile(fn):
                with open(fn, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404)

    def proxy(self, sub, ua):
        req = urllib.request.Request((args.backend_url or 'http://ohb.hamclock.app').rstrip('/') + sub,
                                     headers={'User-Agent': ua or 'HamClock-wasm'})
        ims = self.headers.get('If-Modified-Since')
        if ims:
            req.add_header('If-Modified-Since', ims)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, hdrs, data = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, data = e.code, e.headers, e.read()
        except Exception as e:
            return self.send_error(502, str(e))
        self.send_response(status)
        for k in ('Content-Type', 'Last-Modified'):
            if hdrs.get(k):
                self.send_header(k, hdrs[k])
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


http.server.ThreadingHTTPServer(('', args.port), Handler).serve_forever()
