# What a browser client needs from an OHB server

For Open HamClock Backend maintainers. Nothing about the backend protocol changes: the browser
build sends the same `GET /ham/HamClock/...` requests as the native client. What changes is the
path those requests take, because a web page is subject to browser security rules that a native
program isn't.

## What the requests look like

- `GET /ham/HamClock/<path>`, the same paths as the native client: maps, text feeds and `.pl`
  scripts.
- `If-Modified-Since` where the native client sends it.
- **`X-HamClock-UA`** instead of `User-Agent`, carrying exactly the string the native client
  would send. Browsers won't let a page set `User-Agent`.
- Responses are read as raw bytes. The only response header HamClock uses is `Content-Length`,
  and the browser build generates that itself.

## The browser rules that matter

1. **The page needs SharedArrayBuffer.** HamClock runs in Web Workers on shared memory. That
   only works when the page is served over HTTPS (or from localhost) with
   `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp`.
2. **An HTTPS page can't fetch `http://` URLs.** So the backend has to be HTTPS too, or on the
   same origin as the page.
3. **Cross-origin requests need CORS.** If the page and the backend are on different hosts, the
   backend has to allow it. Because `X-HamClock-UA` and `If-Modified-Since` aren't on the
   browser's list of headers allowed without asking, the browser first sends a preflight
   `OPTIONS` request.

## Three ways to deploy

### A. Serve the page from the OHB host (recommended)

Put the built files (`index.html`, `hamclock.js`, `hamclock.wasm`, `hamclock.worker.js`) under,
say, `/web/` on the same host, and set `DEFAULT_BACKEND_BASE = '/'` in `index.html`. The requests
are then same-origin:

- no CORS and no preflight
- `REMOTE_ADDR` is the user's own address, so **`fetchIPGeoloc.pl` and the per-IP `wx.pl`
  rate limit keep working**
- the only new configuration is two headers on the page's path

A sketch against OHB's `nginx-conf/default` (not tested on a real OHB instance):

```nginx
# HamClock browser client
location /web/ {
    alias /opt/hamclock-web/;
    add_header Cross-Origin-Opener-Policy   "same-origin"  always;
    add_header Cross-Origin-Embedder-Policy "require-corp" always;
    types { application/wasm wasm; text/html html; application/javascript js; }
}
```

`X-HamClock-UA` reaches lighttpd as `HTTP_X_HAMCLOCK_UA`. To keep statistics and `version.pl`
identical, NGINX can use it as the User-Agent when it's present. Native clients don't send it, so
they keep their own:

```nginx
# http context
map $http_x_hamclock_ua $hc_user_agent {
    ""      $http_user_agent;      # native clients
    default $http_x_hamclock_ua;   # browser clients
}

# in each location that proxies to lighttpd
proxy_set_header User-Agent $hc_user_agent;
```

### B. Page hosted elsewhere, CORS on OHB

This allows something like a GitHub Pages copy of the client. OHB would need to answer the
preflight and mark its responses as shareable. A sketch:

```nginx
location /ham/HamClock/ {
    if ($request_method = OPTIONS) {
        add_header Access-Control-Allow-Origin  "*";
        add_header Access-Control-Allow-Headers "X-HamClock-UA, If-Modified-Since";
        add_header Access-Control-Max-Age       86400;
        return 204;
    }
    add_header Access-Control-Allow-Origin "*" always;
    # ...existing proxy_pass settings...
}
```

(In NGINX, `add_header` in a location replaces inherited ones, and the `wx.pl` exact-match
location would need the same header.) You can check a server with:

```sh
curl -si -X OPTIONS https://ohb.example.org/ham/HamClock/version.pl \
     -H 'Origin: https://example.com' \
     -H 'Access-Control-Request-Headers: x-hamclock-ua'
```

`REMOTE_ADDR` is still the user's address here.

Two caveats. A third-party page means a third party controls what HamClock code users run. And
GitHub Pages can't set the COOP/COEP headers itself; it would need the `coi-serviceworker`
workaround.

### C. A reverse proxy in front of OHB (development only)

This is what `web/devserver.py` does: the page fetches `/ohb/...` on its own server, which
forwards to OHB and turns `X-HamClock-UA` back into `User-Agent`. It needs nothing from OHB, but
**every user then shares the proxy's IP address**. IP geolocation would locate the proxy, and the
`wx.pl` per-IP limit (10 requests a minute) would be shared by everyone. It's fine on your own
machine, but not for a public deployment.

## Open questions for OHB

- Does the public `ohb.hamclock.app` serve HTTPS with a publicly trusted certificate? (The
  reference NGINX config listens on 443.)
- Would OHB be willing to host the client under its own domain (option A)?
- Are there endpoints besides `fetchIPGeoloc.pl`, the `wx.pl` limits and `version.pl` that depend
  on the client IP or the User-Agent format?
