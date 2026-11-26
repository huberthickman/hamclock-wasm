# Design notes

How the HamClock C++ client runs in a browser, and why each piece is built the way it is.
Everything here refers to the patches in `../patches/` applied to openhamclock/hamclock@b9db5fb.

## Starting point

HamClock is written against an Arduino emulation layer (`ESPHamClock/ArduinoLib/`): `WiFiClient`,
`WiFiServer`, `WiFiUdp`, `EEPROM` and `Adafruit_RA8875` (the display), reimplemented for Linux
and macOS on POSIX sockets plus X11, `/dev/fb0` or memory. The application code (about 226k
lines) almost never touches the OS directly. So the port is mostly a third implementation of a
few `ArduinoLib` classes.

The `_WEB_ONLY` build variant was the natural base. It already draws into an in-memory
framebuffer for Live Web and has no X11 dependency. Before any changes, the unmodified tree
compiled and linked under Emscripten with zero errors, because Emscripten's POSIX emulation
provides every symbol. The patches are about making it behave correctly, not about getting it to
compile.

## Threads and blocking

HamClock blocks constantly. The main loop calls `loop()` and `usleep()`, every backend fetch does
connect, write and blocking line reads, and the display, radio, blinker and NMEA code run their
own pthreads. A browser's main thread can't block.

The fix is to link with `-sPROXY_TO_PTHREAD`. Emscripten then runs `main()` in a Web Worker,
where blocking calls (`usleep`, mutex waits, synchronous XHR) are allowed, and HamClock's
`pthread_create` calls become more workers from a pool (`-sPTHREAD_POOL_SIZE=16`). The browser's
main thread is left only to draw the canvas and collect input.

This needs `SharedArrayBuffer`, which browsers only enable when the page is *cross-origin
isolated*: served over HTTPS (or from localhost) with

```
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Embedder-Policy: require-corp
```

`web/devserver.py` sends both. A production host has to send them too.

Asyncify was the alternative. It rewrites blocking calls into resumable ones without workers, but
it makes the binary bigger and slower, and it doesn't fit HamClock's existing threads.

## Networking (patch 0001)

Every backend request goes through `WiFiClient`. There are 34 `connect(backend_host,
backend_port)` call sites, each followed by `httpHCGET()`, which writes a hand-built request:

```
GET /ham/HamClock/<page> HTTP/1.0
Host: <backend_host>
User-Agent: HamClock-UNIX/4.34b99 (id … up …) crc … LV7 <about 60 config fields>
If-Modified-Since: …          (sometimes)
Connection: close
```

The callers then parse the response themselves with `httpSkipHeader()` and line or byte reads.

In `__EMSCRIPTEN__` builds:

1. `connect(host, port)` with port 80, 443 or 8080 doesn't open a socket. It marks the client as a
   virtual HTTP connection and hands out a fake descriptor. Other ports go through the page
   server's TCP relay ([patch 0008](#tcp-relay-patch-0008)); without one they fail with a
   message, the way an unreachable host does.
2. `write()` collects the request text.
3. The first `available()` or `read()` performs it. The C++ reads the request line and headers
   and builds a URL. If the host is `backend_host` and `HC_BACKEND_BASE` is set, the base replaces
   `http://host`. The request goes through Emscripten's
   [Fetch API](https://emscripten.org/docs/api_reference/fetch.html): `emscripten_fetch()` with
   `EMSCRIPTEN_FETCH_SYNCHRONOUS | EMSCRIPTEN_FETCH_LOAD_TO_MEMORY | EMSCRIPTEN_FETCH_REPLACE`.
   Underneath, that's a synchronous `XMLHttpRequest`, which browsers allow only off the main
   thread, so it works because HamClock runs in a worker. `REPLACE` skips the Fetch API's own
   IndexedDB cache, and the build turns that cache off entirely (`-sFETCH_SUPPORT_INDEXEDDB=0`),
   because HamClock keeps its own cache in `~/.hamclock`.
4. The reply is rebuilt as `HTTP/1.0 <status>` (from `fetch->status` and `statusText`), the
   response headers the browser exposes (`emscripten_fetch_get_response_headers()`), a correct
   `Content-Length` (the browser has already undone any compression) and the body
   (`fetch->data`). It's then drained through the existing read-ahead buffer. When the buffer is
   exhausted the client closes itself, just like EOF on a socket.


The few request headers that matter are carried over:

- `If-Modified-Since` is forwarded, and a 304 reply reaches HamClock as a 304.
- `User-Agent` can't be set by scripts, so it's sent as `X-HamClock-UA`. `devserver.py` turns
  it back into `User-Agent` when proxying, so OHB's statistics and `version.pl` are unaffected.

The only response header HamClock reads is `Content-Length`, which the shim generates itself.
That means a cross-origin backend doesn't need `Access-Control-Expose-Headers`.

`connectCommand()` (the curl pipe that `connecthttpsHCGET()` and `hamsat.cpp` use for HTTPS) pulls
the URL out of the command, fetches it the same way, and returns only the body, as curl does.

Failures come back as a failed read: a network error, a CORS refusal or an unreachable host, all
of which the browser reports as status 0. HamClock then shows the same "download failed" or
"data invalid" messages it shows natively. HTTP errors such as 404 aren't failures at this
level. They reach HamClock with their status, as they would natively.

**Failed requests in Safari and Firefox** (patch 0009). Emscripten counts each fetch on a
per-thread keepalive counter, and gives the count back from its error handler. When a synchronous
request fails at the network level, Safari and Firefox both fire the request's `error` event and
throw from `send()`. Emscripten handles both (3.1.51 does, and 7.0.1 still does), so the count is
given back twice. Once the
counter would go below zero, a runtime assertion aborts all of HamClock
(`Aborted(Assertion failed)` from `runtimeKeepalivePop`). Every request fails while
`devserver.py` is stopped, so stopping it for longer than two HamClock fetches killed the tab in
those browsers. Chromium only throws, so it was never affected.

All synchronous fetches therefore go through `syncFetch()`, which calls
`emscripten_runtime_keepalive_push()` first, and `..._pop()` afterwards unless the request
failed with status 0. A double give-back then balances. A single one, in Chromium, leaves the
counter one higher, which only means "keep this thread alive", as it is anyway. Tested in
Chromium with a test copy of `hamclock.js` whose `XMLHttpRequest.send()` also fires `error`
before throwing, as Safari does. Without the patch, HamClock aborted while its server was
stopped for 70 s; with it, HamClock kept running and carried on when the server returned.
Plain Chromium was unchanged.

**Timeouts.** The native client gives up connecting after 8 seconds. Browsers don't allow a
timeout on a synchronous request, and the Fetch API's `timeoutMSecs` only applies to asynchronous
ones. `emscripten_fetch_wait()` with a timeout looks like an alternative, but in Emscripten 3.1.51
it returns an error immediately for a request already in progress. So a server that accepts the
connection but never answers holds that HamClock thread until the browser itself gives up.

## Display (patch 0003)

`_WEB_ONLY`'s `fbThread` checks every 50 ms (`REFRESH_US`) and copies the drawing canvas into
`fb_stage` whenever something changed. `wasm_glue.cpp` exports the address and geometry of `fb_stage`,
plus `wasm_frames`, a counter `fbThread` bumps after each copy.

The page polls it in `requestAnimationFrame`, at most every 100 ms. When the counter has changed
it reads the `0x00RRGGBB` words straight out of shared wasm memory, swaps red and blue, sets
alpha, and calls `putImageData`. `ImageData` can't wrap a `SharedArrayBuffer`, so one copy per
frame is unavoidable. Skipping unchanged frames keeps a 3200×1920 build (6.1 million pixels) at
about 3.5% main-thread time. The canvas is scaled to fit the window: pixels are kept sharp when
enlarged and smoothed when shrunk.

## Input (patch 0003)

The first version called `tft.setMouse()` and `tft.putChar()` directly from the browser main
thread, and the page hung on the first keystroke. Those functions take HamClock mutexes. The likely
cause is a worker holding one while it waits on a call Emscripten proxies to the main thread
(stdout and file-system calls are proxied there), while the main thread spins on the same mutex.
Whatever the exact cycle, blocking on worker-owned locks from the main thread is unsafe.

So the rule is that **the browser main thread never takes a HamClock lock.** The page writes
`{type, a, b, c}` events into a 256-entry single-producer, single-consumer ring in wasm memory
(`hc_event()`, using atomics only). An input thread drains it every 10 ms and does exactly what
`liveweb.cpp` does for a Live Web client:

| Event | Action (as in liveweb.cpp) |
|---|---|
| mouse move | `tft.setMouse(x, y)` |
| touch / click | `wifi_tt_s = {x, y}; wifi_tt_live = true; wifi_tt = TT_TAP or TT_TAP_BX` |
| key | `tft.putChar(c, ctrl, shift)` with the same named-key mapping |

Coordinates are in app units (`tft.width()` × `tft.height()`). The page converts from
`getBoundingClientRect()`, so this works at any build size and display scale. Pointer events
cover mouse, pen and touch.

## Links (patch 0005)

Natively, `openURL()` runs `open` or `xdg-open`. For a touch from a Live Web client it instead
stages the URL (`openLiveWebURL()`), and the next Live Web poll sends it to that browser as
`open URL`, or `embed URL` from `openURLPopupEmbed()` for sites known to allow framing, such as
QRZ-style bio pages other than qrz.com, and the ADS-B map.

The browser build reuses that path. A browser has no system browser to launch, so `openURL()` and
`openURLPopupEmbed()` always stage, whether the request came from a tap or the keyboard. There's
no Live Web client to poll for it, so instead:

1. The input thread in `wasm_glue.cpp` collects the staged URL with `takeLiveWebURL()` and copies
   it into a one-entry slot as `open URL` or `embed URL`.
2. The page polls every 100 ms with `hc_url_take()`, copies the string and releases the slot with
   `hc_url_done()`. Like input, this uses atomics only, so the page's thread never takes a
   HamClock lock.
3. The page ignores anything that isn't `http:` or `https:`.

**New tabs and popup blockers.** Browsers only let a page open a tab shortly after a click, tap or
key press ("transient user activation", about 5 seconds in Chromium). The URL normally arrives
well within that window, so the page checks `navigator.userActivation.isActive`. If it's still
active, the page opens the tab with `window.open(url, '_blank', 'noopener')`. If not, for example
because HamClock was busy with a slow download when the tap arrived, the page shows a small bar,
"HamClock wants to open ⟨host⟩ [Open]". Clicking Open is a fresh gesture, so the tab always opens.
Browsers without `navigator.userActivation` try `window.open()` and fall back to the bar if it
returns `null`.

**The embed overlay** matches Live Web's: an `<iframe>` covering most of the clock, with "Open in
new tab" and "Close" buttons, a hint after 4 seconds in case the site refuses to be framed, and
Escape to close. One extra wrinkle comes from cross-origin isolation:
`Cross-Origin-Embedder-Policy: require-corp` blocks iframes whose pages don't send matching
headers, and ordinary sites don't. The iframe uses the `credentialless` attribute, which loads
such pages without cookies, and Chromium allows that under COEP. Only Chromium-based browsers
(Chrome and Edge 110+) support `credentialless`; Firefox and Safari, including Safari on iPad,
don't, so there "embed" links open in a new tab instead of the overlay.

Tested in headless Chromium: a real click through the MOTD popup's "Open link" opened the tab
directly; the same taps fired with no user activation showed the bar, and its Open button opened
the right URL; and a cross-origin page with no COEP headers loaded in the overlay while the page
stayed cross-origin isolated.

## Restarts (patch 0006)

Natively, `doReboot()` calls `ESP::restart()`, which re-executes HamClock with the same arguments,
adding `-K` (skip the startup countdown) and/or `-0` (restore default settings) when asked. A
program can't re-execute itself in a browser: `execvp()` fails and HamClock exits, leaving the
page frozen on its last frame. HamClock restarts itself in several places that stay visible in
the browser build: restoring a saved configuration, "Reset to default configuration", and the
fatal-error screen's restart button.

So in the browser build, `doReboot()` calls `wasmRestart()` in `wasm_glue.cpp`, which:

1. raises `restart_req` (atomic), with bits for `-K` and `-0`;
2. waits forever, like `ESP::restart()`, which never returns either.

The page checks `hc_restart_req()` every 100 ms. When it's set, the page:

1. stores the `-K`/`-0` arguments in `sessionStorage`;
2. saves `~/.hamclock` to IndexedDB with `FS.syncfs()` and waits for it to finish, since HamClock
   may have just written new settings, for example a restored configuration;
3. reloads.

On load, the page reads the stored arguments, removes them, and appends them to `callMain()`.
They deliberately never go in the URL: a bookmarked or manually reloaded `?args=-0` would reset
the settings every time.

The user can also restart simply by reloading the page. The only differences: a manual reload
relies on the asynchronous save when the page closes (see [Storage](#storage)), and shows the
startup countdown.

**Menu items left out at compile time.** The `hamclock-wasm` target builds with:

| Define | Removes | Why |
|---|---|---|
| `NO_UPGRADE` | the new-version check and upgrade prompt | upgrading downloads with curl and re-executes |
| `NO_SYSTEM_CONTROLS` | Reboot computer, Shutdown computer | a page can't control the computer |
| `NO_HAMCLOCK_CONTROLS` | Restart, Exit and Factory reset HamClock | reloading the page restarts; a page can't exit; clearing site data resets |

The Android build sets the first two; it implements restart and exit natively.

Tested in headless Chromium: restoring a saved configuration reloaded the page and the next start
received `-K` and skipped Setup; "Reset to default configuration" reloaded with `-K -0` and showed
Setup with default settings; a manual reload afterwards received neither flag; and
`sessionStorage` was empty after each restart.

## Third-party APIs (patch 0007)

Two panes fetch from services other than the backend, with curl through `connectCommand()`:

- **Sat Alerts:** `https://hams.at/api/alerts/upcoming`, with `-H "Authorization: Bearer <key>"`
  when a hams.at API key is set. Without a key hams.at still answers, but leaves out the
  personalized `match_percent` and `is_workable` fields.
- **Mesh Mon:** `https://meshtastic.liamcottle.net/api/v1/nodes/<id>` and `/neighbours`, for the
  node IDs in Setup's watch list.

Neither service sends `Access-Control-Allow-Origin`, and hams.at answers the preflight that an
`Authorization` header triggers with 404 (checked with curl, November 2026). So a browser can't
read them from a page on any other site, with or without a key.

**In the browser build:**

1. `connectCommand()` splits the curl command like a shell (quotes group words) and keeps the
   URL, `-A` (user agent) and every `-H` header. Before this, it kept only the URL, so the hams.at
   key was silently dropped.
2. When the page sets `HC_XHTTP_BASE` (default `/xhttp`), `https://host/path` is fetched as
   `/xhttp/host/path` from the page's own server, with:
   - the `-H` headers, so the key arrives;
   - the user agent as `X-HamClock-UA`, which the server turns back into `User-Agent`;
   - `X-HamClock-Proxy: 1`, which the server requires.
3. https to `backend_host` goes to `HC_BACKEND_BASE`, like plain http.

**The proxy in `devserver.py`** forwards `/xhttp/HOST/PATH` to `https://HOST/PATH`, only for
allowlisted hosts (`hams.at`, `meshtastic.liamcottle.net`, plus any `--xhttp-allow`), passing on
`Authorization` and the user agent. A forwarding proxy on `localhost` is an obvious target for
other websites in the same browser, so it only accepts requests that:

- carry `X-HamClock-Proxy`. Other websites can only send a custom header after a preflight, and
  `devserver.py` refuses every preflight (`OPTIONS` gets 403);
- are addressed to this server. The `Host` header must be `localhost`, `127.0.0.1` or `[::1]` on
  its port (or an `--allow-host` name), which defeats DNS rebinding, where an attacker's domain
  is pointed at `127.0.0.1`;
- have no foreign `Origin`, and no `Sec-Fetch-Site` other than `same-origin`.

`devserver.py` also now listens on `127.0.0.1` by default instead of every interface (`--bind` to
change). Browsers only run the page from `localhost` or HTTPS anyway, so nothing that worked before
is lost. The log notes whether an `Authorization` header was forwarded, never its value.

Tested in headless Chromium against stand-ins for both services that send no CORS headers. With a
key entered in Setup, Sat Alerts showed "(has key)" and match percentages, which the stand-in only
returns for the right `Bearer` key. Mesh Mon fetched its node and plotted it. Both upstream
requests carried `User-Agent: HamClock-UNIX/4.34b99`, and only hams.at's carried the key. Requests
without the header, with a foreign `Host` or `Origin`, cross-site, to an unlisted host, or as a
preflight were all refused.

## TCP relay (patch 0008)

The DX Cluster, HamAlert and Nearby APRS panes hold a telnet-style TCP connection open
(`dxcluster.cpp`, `hamalert.cpp`, `aprscluster.cpp`), and rigctld, rotctld, flrig and gpsd use
short TCP exchanges. They all go through `WiFiClient::connect(host, port)`, then `print()`,
`available()`, `read()` (usually through `getTCPLine()`/`getTCPChar()`) and `stop()`, and they
detect a dropped connection with `if (!client)` after reads.

**In the browser build,** when the page sets `HC_TCP_BASE` (default `/tcp`), `connect()` to any
port other than 80, 443 or 8080 uses the page server's relay. Every request carries
`X-HamClock-Proxy: 1` and `s=`, a random id for this page made with `getentropy()` on first use:

| Call | Request | Reply |
|---|---|---|
| `connect()` | `POST /tcp/open?host=H&port=P` | 200 with a connection id; 403 or 502 with the reason, which is printed |
| `write()` | `POST /tcp/send?id=N` with the bytes | 200, or 410 if the connection has ended, which `stop()`s as a failed `write(2)` does |
| (background) | `GET /tcp/poll?wait=20000&ids=1,2` | frames of `ID LENGTH EOF\n` then LENGTH bytes, once anything arrives or after 20 s |
| `stop()` | `POST /tcp/close?id=N` | 200 |

All are synchronous `emscripten_fetch()` calls, as in patch 0001.

- **One poller for all connections.** A detached thread, started on the first relayed `connect()`,
  long-polls for every open connection at once and appends what arrives to a per-connection
  buffer, under one mutex with a condition variable. One poll at a time means the relay holds one
  of the browser's six connections per origin, however many panes are open. When there are no
  relayed connections, the thread sleeps on the condition variable rather than polling.
- **Reads behave like a socket.** `available(pending_ms)` refills `peek[]` from the buffer, waiting
  on the condition variable up to `pending_ms`, so `read()`'s usual timeout applies and the
  panes' login code, which waits for prompts a character at a time, works unchanged.
- **EOF.** When the server reports a connection ended, `available()` returns what's left, then
  `stop()`s, so `!client` is true just as after `read(2)` returns 0. If a poll fails because the
  server is gone, every relayed connection is marked ended and the panes see a lost connection
  within a second or so, then retry on their own schedules. The poll's `ids=` list lets a
  restarted server report connections it doesn't know as ended.
- **Reused ids.** Each connection also gets a generation number, so if a restarted server hands
  out an id a stale `WiFiClient` still holds, that client sees its connection as ended and its
  `stop()` doesn't close the new one.

**The relay in `devserver.py`** opens the socket, reads it on a thread per connection into a
buffer (at most 1 MB unread, then it gives up on that connection), and answers polls from those
buffers. It applies the same page-only checks as `/xhttp`, and also:

- **Destinations:** public addresses only, on port 23 or 1024 and up. The host is resolved once
  and the checked address is the one connected to, so DNS can't switch it to a private address
  afterwards. `--tcp-allow HOST:PORT` permits anything else, such as `localhost:4532` for
  rigctld.
- **Limits:** 8 connections per page, 10 s to connect, polls wait at most 25 s.
- **Cleanup:** a page that hasn't polled for 60 s has gone away (closed, reloaded or crashed), so
  its connections are closed.
- **Logging:** one line when a connection opens, one when it ends (who ended it, how long it
  lasted, bytes each way), and one for each refusal. Never the data, so logins and passwords
  stay out of the log. Successful `/tcp/` requests are left out of the access log, because polls
  would fill it.

Tested in headless Chromium against stand-ins for a DXSpider node, HamAlert and an APRS-IS server
(`--tcp-upstream` points the real names at them). With the three panes on screen, all three
connected at once and logged in; the stand-ins received `KV0VWD-55`, the HamAlert call and
password, and the APRS-IS `user ... filter r/...` line. The APRS pane listed the stand-in's
stations, and every DX cluster and HamAlert spot reached HamClock's spot parser. Stopping
`devserver.py` made all three panes report a lost connection within a second. Requests without
the header, with a foreign `Host` or `Origin`, cross-site, or as a preflight were refused, and an
abandoned connection was closed after 69 s.

Then on macOS against the real servers: the DX Cluster pane connected to `dxspider.co.uk:7300`
and listed live spots, HamAlert delivered a spot from a telnet trigger, and Nearby APRS listed
stations from `rotate.aprs2.net:14580`.

## The launcher

`web/launcher.py` is a tkinter window, standard library only, that runs `devserver.py` for one of
the size folders beside it and shows its output. It's a front end: everything it does can be done
with `devserver.py` on the command line.

- **Running `devserver.py`.** It starts `sys.executable -u <size>/devserver.py --port 8000
  --backend-url http://HOST:PORT --show-hamclock-log --exit-on-stdin-close` with pipes for its
  input and output. `PYTHONIOENCODING=utf-8` keeps non-ASCII output from failing on Windows, and
  on Windows it runs without a console window. A thread reads the output into a queue, which the
  Tk loop drains every 100 ms, since Tk may only be used from its own thread.
- **Stopping.** The launcher closes `devserver.py`'s input. `--exit-on-stdin-close` turns that into
  the same clean shutdown as Ctrl-C, which tells open tabs first. This works the same on every
  system, unlike signals, which on Windows can only kill. If `devserver.py` is still running 3 s
  later it is terminated, and killed after 5 s. If the launcher itself dies, its end of the pipe
  closes and `devserver.py` exits, rather than holding port 8000.
- **Opening the browser.** After `devserver.py` prints that it is listening, the launcher waits
  2.5 s. A tab left open from before reconnects within about 2 s (the `/events` retry interval) and
  shows up as `PAGE connected`; only if none does is `webbrowser.open()` called. The URL carries
  `?b=HOST:PORT`, so HamClock's `-b` matches the backend `devserver.py` proxies to.
- **Status.** `PAGE connected (n open)` and `PAGE disconnected (n open)` lines give the tab count
  shown in the status line.
- **Output.** All lines are kept (up to 20,000) so "Show file requests" can redisplay them and
  "Save Output…" can write them. Lines are coloured red when they contain refused, failed, error or
  a traceback (`HC:` lines excepted, since HamClock's own messages use those words routinely).
- **Settings** are a small JSON file in the user's configuration folder.

Tested on Linux with Python 3.12 and Tk 9.0, under a virtual display, with headless Chromium as
the browser, through a whole session:
- start with a custom backend, opening one tab;
- stop, with the tab showing its notice;
- switch to 1600x960, where the open tab reconnected, reloaded at the new size and no second tab
  was opened;
- quit while running, which stopped `devserver.py` first.

Also tested: port 8000 already in use (an error message and status); the launcher being killed
(`devserver.py` exited and freed the port); and a Python without tkinter (the install message).
Python from python.org on macOS and Windows has Tk 8.6, and the launcher uses nothing beyond it.

## Platform stubs (patch 0002)

| Native behavior | Browser build |
|---|---|
| `WiFi::status()` lists interfaces with `getifaddrs()` | Always connected; the page couldn't have loaded otherwise |
| `WiFi::localIP()` opens a probe socket to the backend | Returns 0.0.0.0 without opening a socket |
| Time from NTP over UDP | `useOSTime()` is forced true; the browser clock is NTP-disciplined anyway |
| Main loop throttled with `getrusage()` | `getrusage()` doesn't exist, so the loop uses the existing minimum idle cadence |
| Status line: local IP, backend IP | "Web browser", and the page's backend base (Emscripten's `getaddrinfo()` returns made-up addresses) |

## Storage

`~/.hamclock` (`/home/web_user/.hamclock`) is an IDBFS mount. The page loads it from IndexedDB
before calling `main()`, saves it every 30 s and on `pagehide`. It holds the settings file
(`eeprom`) and cached maps, so a reload skips Setup. Storage is per browser and per origin.

## What the build produces

`dist/<size>/` holds four files, or five with Emscripten 3.1.51. Only `index.html` and
`devserver.py` are written by hand; Emscripten generates the rest on every build.

| File | Size | What it is |
|---|---|---|
| `hamclock.wasm` | 1.7 to 2.6 MB stripped, 9 to 10 MB with `-g` | HamClock itself: all the compiled C++ |
| `hamclock.js` | about 300 KB (80 KB gzipped) with 6.0.3 | Emscripten's generated JavaScript "glue", explained below |
| `hamclock.worker.js` | about 7 KB, 3.1.51 only | Startup script each Web Worker runs first: it loads `hamclock.js` inside the worker and attaches it to the shared memory. Only needed up to Emscripten 3.1.57: from 3.1.58 the worker code is inside `hamclock.js` and this file is an empty placeholder, and from 3.1.68 it isn't generated at all |
| `index.html` | | The host page: canvas drawing, input, storage setup, configuration |
| `devserver.py` | | Local server with the COOP/COEP headers and the `/ohb` proxy |

### What hamclock.js does

A wasm binary can't do anything by itself: it can't load itself, start threads, open files or
print. `hamclock.js` supplies all of that:

- **Loads and starts the wasm.** It fetches `hamclock.wasm`, compiles it with
  `WebAssembly.instantiateStreaming`, and creates the shared `WebAssembly.Memory` that all of
  HamClock's threads use.
- **Provides the operating system HamClock thinks it has.** Calls like `open`, `read`, `write`,
  `stat`, `mkdir`, `gettimeofday`, `getenv` and `printf` compile to imports that `hamclock.js`
  implements in JavaScript:
  - **Files:** go to an in-memory file system (MEMFS). `~/.hamclock` is mounted on the
    IndexedDB-backed IDBFS (see [Storage](#storage)).
  - **Output:** stdout reaches the DevTools console.
  - **Time and environment:** come from the browser, including the `HC_BACKEND_BASE` variable the
    page sets.
- **Runs the threads.** It creates the Web Workers that `main()` and HamClock's pthreads run in,
  and keeps a pool of them ready. When a worker needs something only the main thread can do,
  such as a file-system operation, it passes the call over to the main thread and waits.
- **Includes the Fetch API.** `emscripten_fetch()`, which patch 0001 calls from C++, is implemented
  in JavaScript here: it creates the `XMLHttpRequest`, sets the request headers, and copies the
  response into wasm memory.
- **Exposes the API the page uses.** `Module.callMain`, `Module.FS`, `Module.HEAPU8`,
  `Module.ENV`, and the exported C functions such as `Module._hc_fb_pixels`,
  `Module._hc_fb_frames`, `Module._hc_event`, `Module._hc_url_take` and `Module._hc_restart_req`.

Never edit `hamclock.js`, because the next build overwrites it. Changes go in the C++ (the
request handling is in `ArduinoLib/WiFiClient.cpp`), the link flags in the Makefile, or
`web/index.html`.

## The host page (web/index.html)

- Built with `-sINVOKE_RUN=0`. The page mounts IDBFS, waits for the IndexedDB load, then calls
  `callMain(['-b', host, '-e', '-1', '-r', '-1', '-w', '-1', '-o', …])`. `-e/-r/-w -1` disable
  the RESTful and Live Web servers, and `-o` sends the log to the browser console.
- `HC_BACKEND_BASE`, `HC_XHTTP_BASE` and `HC_TCP_BASE` are set in the Emscripten environment in
  `preRun`.
- **One tab at a time.** Every tab of the page shares one IndexedDB `eeprom`, so two running
  HamClocks would overwrite each other's settings. The page loads `hamclock.js` only once it holds
  a [Web Lock](https://developer.mozilla.org/en-US/docs/Web/API/Web_Locks_API) named `hamclock`,
  which it keeps until the tab closes or reloads. A second tab shows "already open in another
  tab" and queues for the lock, so it starts by itself when the first tab goes. Reloads, including
  HamClock's own restarts, release and retake the lock without a notice. Browsers without
  `navigator.locks` just start.
- **Watching the server.** The page keeps a Server-Sent Events connection to `/events`
  (`DEFAULT_EVENTS`, `?events=`). `devserver.py` catches Ctrl-C, SIGTERM and SIGHUP, sends each
  open page `event: stopped`, waits half a second and exits. The page then shows "HamClock's
  local communication process has stopped" and saves settings at once. The notices call
  `devserver.py` "HamClock's local communication process" rather than a server, for people who
  don't think in those terms. A drop without the event shows "Can't reach HamClock's local
  communication process", but only if it lasts 3 seconds. EventSource reconnects by itself
  (the server asks for every 2 s), and the notice goes away when it does. HamClock is never
  paused: its fetches fail and the relayed panes report lost connections, as natively during a
  network outage, and they recover when the server returns. If the first connection never
  opens, the server has no `/events` (say, the page is hosted elsewhere) and the page stops
  watching.
- **Counting pages.** `devserver.py` logs `PAGE connected (n open)` and `PAGE disconnected`. It
  checks every second whether each tab has hung up, so the count stays right; a launcher can use
  it to know whether a tab is still open.
- The defaults at the top of the file (`DEFAULT_BACKEND_HOST`, `DEFAULT_BACKEND_BASE`,
  `DEFAULT_XHTTP_BASE`, `DEFAULT_TCP_BASE`, `DEFAULT_EVENTS`) are the only things a deployer
  should need to edit.
- **HamClock's output.** Everything HamClock prints, from any thread, reaches the page's
  `Module.print`: pthreads' `fd_write` is proxied to the main thread. The page still copies each
  line to the console, and also keeps the last 500. When `/events`' `config` message says
  `"log": true` (`devserver.py --show-hamclock-log`), it posts what it has to `/log` about once a
  second, and `devserver.py` prints each line as `HC: …`. While the server is away the lines wait,
  up to 500, and are sent once it's back. `/log` has the same page-only checks as `/xhttp`. With
  logging off, the page keeps nothing.
- **A tab left open across a change.** `config` also carries a build id, the served
  `hamclock.wasm`'s size and modification time. If it differs from the one the tab started with
  (the launcher was switched to another size, or there's a new build), the tab saves its settings
  and reloads.

## Build flags

| Flag | Why |
|---|---|
| `-sPROXY_TO_PTHREAD` | `main()` in a worker, so blocking works |
| `-sPTHREAD_POOL_SIZE=16` | Workers created ahead of time for HamClock's threads |
| `-sALLOW_MEMORY_GROWTH=1 -sINITIAL_MEMORY=256MB` | Big builds and large maps |
| `-sSTACK_SIZE=8MB -sDEFAULT_PTHREAD_STACK_SIZE=2MB` | HamClock puts large buffers on the stack; Emscripten's default is 64 KB |
| `-sEXIT_RUNTIME=0 -sINVOKE_RUN=0` | The page decides when `main()` runs |
| `-lidbfs.js` | Persistent `~/.hamclock` |
| `-sFETCH -sFETCH_SUPPORT_INDEXEDDB=0` | Emscripten's Fetch API for `WiFiClient`, without its IndexedDB cache |
| `-sEXPORTED_RUNTIME_METHODS=FS,HEAPU8,…` | What `index.html` uses |

The compile step also gets `-D_WEB_ONLY`, the size's `-D_CLOCK_WxH`, and the three `NO_*` defines
described under [Restarts](#restarts-patch-0006). The Makefile records the size and defines in
`.wasm-size` and cleans the tree whenever they change, because object files don't record them.

Sizes with Emscripten 6.0.3: the wasm is 9 to 10 MB with HamClock's default `-g`. After
`wasm-opt --strip-debug -O2` it's 1.7 MB (800x480), 1.9 MB (1600x960), 2.2 MB (2400x1440) and
2.6 MB (3200x1920), about 625 KB gzipped at 800x480. The JavaScript glue is about 300 KB, 80 KB
gzipped. With 3.1.51 the wasm was much the same size and the JavaScript smaller, about 225 KB.

`build.sh` keeps object files between builds, but records the patches' checksum and the first
line of `emcc --version` in `build/.patch-sum`. When either changes, it removes the Makefile's
`.wasm-size` stamp, which makes the Makefile clean before building: make tracks neither header
changes nor the compiler, and objects from another Emscripten version may not link, or worse,
may link.

## Emscripten versions

The browser build targets **Emscripten 6.0.3**. It was built and tested with 3.1.51 (December
2023) until November 2026. Findings from building HamClock with later versions:

| Versions | Result |
|---|---|
| 3.1.51 | Works. Fully tested, the reference until 6.0.3 |
| 5.0.7 | Works. Build, relay, stop and restart, notices, tab lock and settings tested |
| 6.0.1, 6.0.2 | **Avoid.** They pass a request body to the browser as a view onto the wasm memory, which is a `SharedArrayBuffer` in a threaded build, and browsers refuse to send that: every relayed write fails, so the DX Cluster, HamAlert and APRS panes can't log in. Fixed in 6.0.3, which copies the body again |
| 6.0.3 | Works. **The target.** Everything below tested |
| 6.0.4 to 6.0.12 | Not built. Need LLVM 24, which only comes with the official SDK. From the changelogs and source, nothing here affects HamClock |
| 6.0.13 | Not built. Removed `FS.filesystems`, which the page used to mount IDBFS; the page now uses plain `IDBFS`, which works with every version |
| 7.0.0 and later | Compiled, not linked (LLVM 24 again). libc++ 23 stops including headers indirectly; `passwords.cpp` was the only file affected, fixed by patch 0010 |

In every version checked, from 3.1.51 to 7.0.1, a synchronous request that fails at the network
level is still reported twice in Safari and Firefox, so patch 0009 stays.

Tested with 6.0.3, in headless Chromium, at every size:
- all four sizes start and draw at full resolution;
- settings survive a reload;
- links open in a new tab after a real click, or through the "click to open" bar without one;
- the TCP relay: DX Cluster, HamAlert and APRS log in and receive spots;
- 70 s without `devserver.py`, with Safari-style request failures, then carrying on;
- the stopped and unreachable notices, the one-tab lock, and reloads;
- a launcher session: start, stop, switching size (the open tab reloaded at the new size), and
  quitting while running.

Restarts from inside HamClock weren't driven here (they sit behind HamClock's own menus), but
use the same polling as links, and `_hc_restart_req` is exported. The build used the released
LLVM 23.1.6 rather than the snapshot that the 6.0.3 SDK ships, and Chromium only.

## Sizes

`WASM_SIZE` maps to the same `-D_CLOCK_WxH` defines as the native targets. App coordinates stay
800×480; only the framebuffer and the fonts scale. Larger builds request larger maps (for
example `map-D-2640x1320-*.bmp.z` at 3200x1920), which OHB provides.

## Limitations and ideas

- **UDP** (WSJT-X and other UDP spot sources for the DX Cluster pane): the relay is TCP only. A
  UDP listener in `devserver.py` that queues datagrams for the page would work the same way.
- **The TCP relay on a hosted copy** would make connections on every user's behalf, with their
  logins passing through it. Fine for a local `devserver.py`; a question for OHB otherwise.
- **Why not WebSockets?** Emscripten can carry POSIX sockets over WebSockets, but a WebSocket's
  messages arrive on the event loop of the thread that owns it, and HamClock's threads never
  return to theirs. Long-polling with synchronous requests fits a blocking program, and needs
  nothing beyond Python's standard library on the server.
- **Blocking fetches**: one request at a time per HamClock thread, the same as native. Could
  become asynchronous later, but that would change upstream code paths.
- **PWA**: a manifest and service worker would let it be installed on a home screen (iPad) and
  start offline with cached maps. That's the route to an iPad HamClock that doesn't need the
  App Store.
