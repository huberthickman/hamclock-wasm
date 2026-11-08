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
   virtual HTTP connection and hands out a fake descriptor. Other ports log
   `raw TCP is not available in the browser build` and fail, so the DX cluster and similar
   features degrade the way they do when a host is unreachable.
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

`dist/<size>/` holds five files. Only `index.html` and `devserver.py` are written by hand;
Emscripten generates the other three on every build.

| File | Size | What it is |
|---|---|---|
| `hamclock.wasm` | 1.7 to 1.9 MB stripped, 9 to 10 MB with `-g` | HamClock itself: all the compiled C++ |
| `hamclock.js` | about 225 KB (55 KB gzipped) | Emscripten's generated JavaScript "glue", explained below |
| `hamclock.worker.js` | about 7 KB | Startup script each Web Worker runs first: it loads `hamclock.js` inside the worker and attaches it to the shared memory. Only needed up to Emscripten 3.1.57: from 3.1.58 the worker code is inside `hamclock.js` and this file is an empty placeholder, and from 3.1.68 it isn't generated at all |
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
  `Module._hc_fb_frames`, `Module._hc_event` and `Module._hc_url_take`.

Never edit `hamclock.js`, because the next build overwrites it. Changes go in the C++ (the
request handling is in `ArduinoLib/WiFiClient.cpp`), the link flags in the Makefile, or
`web/index.html`.

## The host page (web/index.html)

- Built with `-sINVOKE_RUN=0`. The page mounts IDBFS, waits for the IndexedDB load, then calls
  `callMain(['-b', host, '-e', '-1', '-r', '-1', '-w', '-1', '-o', …])`. `-e/-r/-w -1` disable
  the RESTful and Live Web servers, and `-o` sends the log to the browser console.
- `HC_BACKEND_BASE` is set in the Emscripten environment in `preRun`.
- The defaults at the top of the file (`DEFAULT_BACKEND_HOST`, `DEFAULT_BACKEND_BASE`) are the
  only things a deployer should need to edit.

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

Sizes with Emscripten 3.1.51 and LLVM 18: the wasm is 9 to 10 MB with HamClock's default `-g`.
After `wasm-opt --strip-debug -O2` it's 1.7 MB (800x480) to 1.9 MB (1600x960), or about
625 KB gzipped at 800x480. The JavaScript glue is about 225 KB, 55 KB gzipped.

## Sizes

`WASM_SIZE` maps to the same `-D_CLOCK_WxH` defines as the native targets. App coordinates stay
800×480; only the framebuffer and the fonts scale. Larger builds request larger maps (for
example `map-D-2640x1320-*.bmp.z` at 3200x1920), which OHB provides.

## Limitations and ideas

- **Raw TCP and UDP** (DX cluster, APRS-IS, HamAlert, hamlib, flrig, gpsd, WSJT-X): Emscripten
  can carry POSIX sockets over WebSockets to a relay. A public relay would need a destination
  allowlist, or it becomes an open proxy.
- **Blocking fetches**: one request at a time per HamClock thread, the same as native. Could
  become asynchronous later, but that would change upstream code paths.
- **PWA**: a manifest and service worker would let it be installed on a home screen (iPad) and
  start offline with cached maps. That's the route to an iPad HamClock that doesn't need the
  App Store.
