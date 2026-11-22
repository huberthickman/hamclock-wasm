#!/usr/bin/env python3
"""Development server for the HamClock WebAssembly build.

  python3 devserver.py [--port 8000] [--bind 127.0.0.1]
                       [--backend-url URL | --backend-dir DIR]
                       [--xhttp-allow HOST ...] [--no-xhttp] [--allow-host NAME:PORT ...]
                       [--tcp-allow HOST:PORT ...] [--no-tcp]

Serves this directory with the cross-origin isolation headers that SharedArrayBuffer (and so
HamClock's threads) require, plus five same-origin routes for the page:

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

/tcp/...    a relay for the TCP connections browsers can't make: HamAlert, DX clusters and
            APRS-IS. The page opens a connection with POST /tcp/open, writes with POST /tcp/send,
            and one GET /tcp/poll at a time collects what has arrived on all its connections.
            Connections go to public addresses on port 23 or 1024 and up; --tcp-allow adds others,
            such as rigctld on this machine. Same page-only checks as /xhttp. Connections close
            when the page stops polling for a minute (it was closed or reloaded). Only host, port,
            byte counts and why a connection ended are logged, never what was sent, so logins
            and passwords stay out of the log.

/events     a Server-Sent Events stream each open page keeps connected, so the page can tell
            when this server has stopped and say so. On Ctrl-C or SIGTERM the server sends each
            page a "stopped" event before it exits; if it vanishes without one, the page notices
            the connection drop instead. The log notes pages connecting and disconnecting. On
            connecting, a page is told which hamclock.wasm this server has (so a tab left open
            across a size change reloads itself) and whether to send HamClock's output to /log.

/log        with --show-hamclock-log, the page sends what HamClock prints and it appears here
            prefixed "HC: ", as the native HamClock's output would. The launcher uses this.

Listens on 127.0.0.1 only unless --bind says otherwise. Browsers only run the page from
localhost or over HTTPS anyway, so other machines can't use a plain-HTTP server like this one.
"""
import argparse, http.server, ipaddress, json, os, re, select, signal, socket, sys, threading, time, \
    urllib.parse, urllib.request, urllib.error

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
                help='extra Host header values to accept on /xhttp/ and /tcp/, eg when using --bind')
ap.add_argument('--tcp-allow', action='append', default=[], metavar='HOST:PORT',
                help='also let /tcp/ reach HOST:PORT, even a private address or port under 1024')
ap.add_argument('--no-tcp', action='store_true', help='turn the /tcp/ relay off')
ap.add_argument('--tcp-upstream', action='append', default=[], metavar='HOST:PORT=ADDR:PORT',
                help='connect /tcp/ requests for HOST:PORT to ADDR:PORT instead (testing)')
ap.add_argument('--show-hamclock-log', action='store_true',
                help="print HamClock's own output, sent by the page, prefixed HC:")
ap.add_argument('--exit-on-stdin-close', action='store_true',
                help='stop cleanly when standard input closes (for a launcher running this)')
args = ap.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
XHTTP_HOSTS = set() if args.no_xhttp else \
    {'hams.at', 'meshtastic.liamcottle.net'} | {h.lower() for h in args.xhttp_allow}
XHTTP_UPSTREAM = dict(u.split('=', 1) for u in args.xhttp_upstream)
OUR_HOSTS = {f'{n}:{args.port}' for n in ('localhost', '127.0.0.1', '[::1]')} | set(args.allow_host)
TCP_ALLOW = {a.lower() for a in args.tcp_allow}
TCP_UPSTREAM = {k.lower(): v for k, v in (u.split('=', 1) for u in args.tcp_upstream)}


def fetch(url, headers):
    """GET url, return (status, headers, body); upstream HTTP errors are passed through."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# ------------------------------------------------------------------------------------------------
# TCP relay

TCP_MAX_PER_PAGE = 8                # open connections one page may have
TCP_MAX_UNREAD = 1 << 20            # bytes held for a page before giving up on the connection
TCP_PAGE_GONE = 60                  # seconds without a poll before a page's connections close
TCP_POLL_MAX = 25                   # longest a poll waits for data, seconds

tcp_cond = threading.Condition()    # guards everything below; notified when data or EOF arrives
tcp_conns = {}                      # id -> TCPConn
tcp_seen = {}                       # page session id -> time of its last poll or open
tcp_next_id = 1


def ago(t0):
    s = int(time.time() - t0)
    return f'{s // 3600}h{s // 60 % 60:02d}m' if s >= 3600 else f'{s // 60}m{s % 60:02d}s'


class TCPConn:
    def __init__(self, cid, sid, sock, name):
        self.id, self.sid, self.sock, self.name = cid, sid, sock, name
        self.buf = bytearray()
        self.eof = False            # no more data will arrive
        self.done = False           # logged and forgotten
        self.t0 = time.time()
        self.n_in = self.n_out = 0

    def finish(self, why):
        """close and log once; caller holds tcp_cond"""
        if self.done:
            return
        self.done = self.eof = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        sys.stderr.write(f'TCP #{self.id} {self.name} {why} after {ago(self.t0)}, '
                         f'{self.n_in} bytes in, {self.n_out} out\n')
        tcp_cond.notify_all()

    def reader(self):
        why = 'closed by server'
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError as e:
                why = f'lost: {e.strerror or e}'
                data = b''
            with tcp_cond:
                if self.done:
                    return
                if not data:
                    break
                self.buf += data
                self.n_in += len(data)
                if len(self.buf) > TCP_MAX_UNREAD:
                    why = 'closed: page stopped reading'
                    break
                tcp_cond.notify_all()
        with tcp_cond:
            self.finish(why)    # the page's next poll gets what's left in buf, then the EOF


def tcp_reaper():
    while True:
        time.sleep(10)
        now = time.time()
        with tcp_cond:
            for c in list(tcp_conns.values()):
                if now - tcp_seen.get(c.sid, c.t0) > TCP_PAGE_GONE:
                    c.finish('closed: page went away')
                    del tcp_conns[c.id]


def tcp_destination(host, port):
    """return ((family, sockaddr), None) where host:port may go, else (None, why)"""
    key = f'{host}:{port}'
    if key in TCP_UPSTREAM:
        a, _, p = TCP_UPSTREAM[key].rpartition(':')
        return (socket.AF_INET6 if ':' in a else socket.AF_INET, (a.strip('[]'), int(p))), None
    if not re.fullmatch(r'[a-z0-9.-]{1,253}|\[[0-9a-f:.]+\]', host) or not 0 < port < 65536:
        return None, 'bad host or port'
    allowed = key in TCP_ALLOW
    if port < 1024 and port != 23 and not allowed:
        return None, f'port {port} is not allowed (see --tcp-allow)'
    try:
        infos = socket.getaddrinfo(host.strip('[]'), port, type=socket.SOCK_STREAM)
    except OSError:
        return None, f'{host} not found'
    for fam, _, _, _, sa in infos:
        # connect to the address checked here, so DNS can't switch it to a private one later
        if allowed or ipaddress.ip_address(sa[0].split('%')[0]).is_global:
            return (fam, sa), None
    return None, f'{host} is a private or local address (see --tcp-allow)'


if not args.no_tcp:
    threading.Thread(target=tcp_reaper, daemon=True).start()


def build_id():
    """identifies the hamclock.wasm being served, so a tab left open across a change reloads"""
    try:
        st = os.stat(os.path.join(HERE, 'hamclock.wasm'))
        return f'{st.st_size}-{st.st_mtime_ns}'
    except OSError:
        return ''


# pages connected to /events, and set once this server is stopping
events_lock = threading.Lock()
events_open = 0
stopping = threading.Event()

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def end_headers(self):
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_request(self, code='-', size='-'):
        # the TCP, PAGE and HC lines say more, and polls would fill the log every 20 seconds
        quiet = self.path.startswith('/tcp/') or self.path in ('/events', '/log')
        if not (quiet and str(code).startswith('2')):
            super().log_request(code, size)

    def guess_type(self, path):
        return 'application/wasm' if path.endswith('.wasm') else super().guess_type(path)

    def do_OPTIONS(self):
        # a preflight means another website is trying to use the proxies: never allow it
        self.send_error(403, 'cross-origin requests are not allowed')

    def do_GET(self):
        if self.path == '/events':
            return self.events()
        if self.path.startswith('/ohb/'):
            return self.backend(self.path[len('/ohb'):])
        if self.path.startswith('/xhttp/'):
            return self.xhttp(self.path[len('/xhttp/'):])
        if self.path.startswith('/tcp/'):
            return self.tcp('GET')
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/tcp/'):
            return self.tcp('POST')
        if self.path == '/log':
            return self.page_log()
        self.send_error(405)

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

    def not_our_page(self):
        """why this request may not use the proxies, or None if it comes from our page"""
        if not self.headers.get('X-HamClock-Proxy'):
            return 'missing X-HamClock-Proxy'
        if self.headers.get('Host', '') not in OUR_HOSTS:
            return f'Host {self.headers.get("Host")!r} is not this server (see --allow-host)'
        if self.headers.get('Origin') and self.headers['Origin'] != 'http://' + self.headers['Host']:
            return 'cross-origin request'
        if self.headers.get('Sec-Fetch-Site', 'same-origin') != 'same-origin':
            return 'cross-site request'
        return None

    def text(self, status, msg, data=None):
        body = data if data is not None else (msg + '\n').encode()
        self.send_response(status, msg if data is None else None)
        self.send_header('Content-Type', 'application/octet-stream' if data is not None else 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def xhttp(self, hostpath):
        # only this page may use the proxy
        why = self.not_our_page()
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

    def page_log(self):
        """print lines of HamClock's output sent by the page"""
        n = min(int(self.headers.get('Content-Length') or 0), 1 << 20)
        body = self.rfile.read(n)
        if self.not_our_page():
            return self.text(403, 'not from the HamClock page')
        if args.show_hamclock_log:
            for line in body.decode('utf-8', 'replace').splitlines():
                sys.stderr.write(f'HC: {line}\n')
        self.send_response(204)
        self.end_headers()

    def events(self):
        """keep a page's status stream open until it disconnects or this server stops"""
        global events_open
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        with events_lock:
            events_open += 1
            n = events_open
        sys.stderr.write(f'PAGE connected ({n} open)\n')
        try:
            config = json.dumps({'log': args.show_hamclock_log, 'build': build_id()})
            self.wfile.write(b'retry: 2000\n: hello\n\n')      # reconnect every 2 s when lost
            self.wfile.write(f'event: config\ndata: {config}\n\n'.encode())
            last = time.time()
            while not stopping.wait(1):
                # pages never send on this connection, so anything readable is the tab hanging up
                if select.select([self.connection], [], [], 0)[0] and \
                        not self.connection.recv(1, socket.MSG_PEEK):
                    break
                if time.time() - last >= 15:
                    self.wfile.write(b': keepalive\n\n')        # keeps proxies from timing out
                    last = time.time()
            else:
                self.wfile.write(b'event: stopped\ndata: \n\n')
        except OSError:
            pass
        finally:
            with events_lock:
                events_open -= 1
                n = events_open
            if not stopping.is_set():
                sys.stderr.write(f'PAGE disconnected ({n} open)\n')

    def tcp(self, method):
        global tcp_next_id
        url = urllib.parse.urlsplit(self.path)
        op = url.path[len('/tcp/'):]
        q = dict(urllib.parse.parse_qsl(url.query))
        sid = q.get('s', '')
        why = 'the relay is off (--no-tcp)' if args.no_tcp else self.not_our_page()
        if not why and not re.fullmatch(r'[0-9a-f]{8,64}', sid):
            why = 'bad session'
        if not why and (op, method) not in (('open', 'POST'), ('send', 'POST'), ('close', 'POST'),
                                            ('poll', 'GET')):
            why = f'no {method} /tcp/{op}'
        if why:
            sys.stderr.write(f'TCP refused /tcp/{op}: {why}\n')
            return self.text(403, why)
        body = self.rfile.read(min(int(self.headers.get('Content-Length') or 0), 65536)) \
            if method == 'POST' else b''

        if op == 'open':
            host, port = q.get('host', '').lower(), int(q.get('port') or 0)
            with tcp_cond:
                tcp_seen[sid] = time.time()
                n = sum(1 for c in tcp_conns.values() if c.sid == sid)
            dest, why = tcp_destination(host, port) if n < TCP_MAX_PER_PAGE \
                else (None, f'more than {TCP_MAX_PER_PAGE} connections')
            if not dest:
                sys.stderr.write(f'TCP refused {host}:{port}: {why}\n')
                return self.text(403, why)
            try:
                sock = socket.socket(dest[0], socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect(dest[1])
                sock.settimeout(None)
            except OSError as e:
                why = e.strerror or str(e) or 'timed out'
                sys.stderr.write(f'TCP {host}:{port} failed: {why}\n')
                return self.text(502, why)
            with tcp_cond:
                c = TCPConn(tcp_next_id, sid, sock, f'{host}:{port}')
                tcp_next_id += 1
                tcp_conns[c.id] = c
            via = f' via {dest[1][0]}:{dest[1][1]}' if f'{host}:{port}' in TCP_UPSTREAM else ''
            sys.stderr.write(f'TCP #{c.id} {c.name} open{via}\n')
            threading.Thread(target=c.reader, daemon=True).start()
            return self.text(200, 'OK', str(c.id).encode())

        cid = int(q.get('id') or 0)
        if op == 'send':
            with tcp_cond:
                c = tcp_conns.get(cid)
                if not c or c.sid != sid or c.eof:
                    return self.text(410, 'not connected')
            try:
                c.sock.sendall(body)
            except OSError as e:
                with tcp_cond:
                    c.finish(f'lost: {e.strerror or e}')
                return self.text(410, 'not connected')
            with tcp_cond:
                c.n_out += len(body)
            return self.text(200, 'OK', b'')

        if op == 'close':
            with tcp_cond:
                c = tcp_conns.get(cid)
                if c and c.sid == sid:
                    c.finish('closed by HamClock')
                    del tcp_conns[cid]
            return self.text(200, 'OK', b'')

        # poll: wait until something arrives on any of this page's connections, then return it all
        # as frames of "ID LENGTH EOF\n" followed by LENGTH bytes, EOF being 1 once the connection
        # has ended and this is the last of its data. ids lists the connections the page thinks are
        # open; any this server doesn't have (it restarted, or reaped them) are reported ended.
        wait = min(max(int(q.get('wait') or 0), 0), TCP_POLL_MAX * 1000) / 1000
        ids = {int(i) for i in q.get('ids', '').split(',') if i.isdigit()}
        out = bytearray()
        with tcp_cond:
            tcp_seen[sid] = time.time()
            for i in ids:
                c = tcp_conns.get(i)
                if not c or c.sid != sid:
                    out += f'{i} 0 1\n'.encode()
            deadline = time.time() + wait
            while True:
                ready = [c for c in tcp_conns.values() if c.sid == sid and (c.buf or c.eof)]
                left = deadline - time.time()
                if ready or out or left <= 0:
                    break
                tcp_cond.wait(left)
            for c in ready:
                out += f'{c.id} {len(c.buf)} {int(c.eof)}\n'.encode() + c.buf
                c.buf = bytearray()
                if c.eof:
                    del tcp_conns[c.id]
            tcp_seen[sid] = time.time()
        return self.text(200, 'OK', bytes(out))


def stop_now(*_):
    raise KeyboardInterrupt

try:
    server = http.server.ThreadingHTTPServer((args.bind, args.port), Handler)
except OSError as e:
    print(f'Can not listen on port {args.port}: {e.strerror or e}.\n'
          f'Is another HamClock still running? Stop it, or use --port.', flush=True)
    sys.exit(1)
for sig in ('SIGTERM', 'SIGHUP'):           # a signal from a launcher, or the terminal closing
    if hasattr(signal, sig):
        signal.signal(getattr(signal, sig), stop_now)
if args.exit_on_stdin_close:
    # a launcher stops us by closing our input: works the same on every system, and if the launcher
    # dies, so do we, instead of keeping the port busy
    def watch_stdin():
        while sys.stdin.buffer.read(4096):
            pass
        server.shutdown()
    threading.Thread(target=watch_stdin, daemon=True).start()
print(f'HamClock: http://localhost:{args.port}/   (listening on {args.bind}:{args.port})', flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    # tell open pages before exiting, so they can say the server stopped rather than vanished
    stopping.set()
    with events_lock:
        n = events_open
    if n:
        time.sleep(0.5)
    print(f'\nStopped' + (f'; told {n} open page{"s" if n != 1 else ""}' if n else ''), flush=True)
