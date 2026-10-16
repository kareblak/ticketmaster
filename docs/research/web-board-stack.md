# Server and front-end stack for a local SSE web board

Research for [#150](https://github.com/kareblak/ticketmaster/issues/150), map
[#149](https://github.com/kareblak/ticketmaster/issues/149) "Ticketmaster web board view". Done on 2026-10-16 against
`main` at `1d77199`, CPython 3.12.8 on macOS, and the current PyPI and npm releases.

## Question

Which server and front-end stack should serve the web board on localhost with server-sent events (SSE), given
ticketmaster's existing Textual/asyncio app and its plugin venv install?

## Recommendation

- **Server: the stdlib `http.server.ThreadingHTTPServer`** with `daemon_threads = True`, bound to `127.0.0.1` on
  port 0. One poller thread runs the shared board model's `Pacer` and fetch. Each SSE handler thread waits on a
  `threading.Condition` and writes `text/event-stream` frames to `wfile`. It adds no dependencies, so the plugin venv
  and `pyproject.toml` stay as they are. The fetch path is already blocking and threaded, so this is a natural fit.
- **Page: vanilla JS with `EventSource`.** Ship one static HTML file, one small JS file and one CSS file as package
  data, with no htmx. The server renders the board as HTML fragments in Python. The page swaps them in, keeps its own
  filter, chips and detail-panel state, and POSTs the Start confirm.
- **Rejected options.** Starlette + uvicorn and aiohttp both work, and each costs only about 5 MB and 1 s of install.
  But each brings a second runtime model (asyncio) for no feature the board needs. Uvicorn also hangs on shutdown
  while a browser tab holds an SSE stream open (measured below). htmx 2 is in maintenance now that htmx 4 has
  shipped, and htmx 4's SSE extension is days old and documented as "under construction". Either would mean
  vendoring 40-55 KB of JS to replace about a hundred lines of our own.

The rest of this note gives the evidence.

## How the web board would run (from the map's standing decisions)

- The web server is **its own process**: a herdr pane labelled `ticketmaster-web`, a detached process with a pidfile,
  or a foreground process at a shell. It does not run inside the Textual `BoardApp`, so "running alongside the
  board's refresh loop" means the server must run that loop itself. It must not share an event loop with Textual.
- The board's refresh loop is already synchronous and clock-injected. `Pacer` (`ticketmaster/board.py:283`) decides
  what is due given `now`, and `BoardApp._fetch` runs on a **thread** worker (`run_worker(..., thread=True)`,
  `ticketmaster/board.py:932`) because the provider calls block. Taken out of `BoardApp`, this loop can run as a
  plain thread with no event loop. On an asyncio server, it would have to be wrapped in `asyncio.to_thread` or an
  executor.
- The server needs one tracker poll however many tabs are open. That is a broadcast, one producer to N streams.
  Every candidate below can do it.

## Server candidates

### Dependency weight and install time in the plugin venv (measured)

`bin/ticketmaster` builds the venv with `python3 -m venv` and `pip install` on first run and on every version bump,
so every dependency adds to the first board's wait. I installed each stack into a fresh venv with
`pip install --no-cache-dir` (CPython 3.12.8, macOS arm64, 2026-10-16):

| Stack                                | Wall time | site-packages | Packages added beyond Textual's                                                         |
|--------------------------------------|----------:|--------------:|-----------------------------------------------------------------------------------------|
| `textual` (today)                    |     2.1 s |       34.8 MB | —                                                                                       |
| + stdlib `http.server`               |     2.1 s |       34.8 MB | none                                                                                    |
| + `starlette uvicorn`                |     2.7 s |       39.8 MB | starlette 1.8.0, uvicorn 0.54.0, anyio, click, h11, idna                                |
| + `starlette uvicorn sse-starlette`  |     3.4 s |       39.9 MB | the above + sse-starlette 3.5.0                                                         |
| + `aiohttp`                          |     3.6 s |       41.1 MB | aiohttp 3.14.5, aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache, yarl |

The cost in seconds and megabytes is small on a warm network with wheels available. The larger cost is the number
of moving parts. Starlette needs 2 runtime dependencies (`anyio`, and `typing_extensions` before Python 3.13) and
uvicorn needs 2 (`click`, `h11`) ([starlette pyproject][starlette-pyproject], [uvicorn pyproject][uvicorn-pyproject]).
aiohttp pulls in 7 more, several of them C extensions that need a matching wheel or a compiler. Every one of these is
a version the plugin's `pip install` resolves at the user's first run.

### stdlib `http.server` / `ThreadingHTTPServer`

- `ThreadingHTTPServer` (since 3.7) "uses threads to handle requests by using the ThreadingMixIn. This is useful to
  handle web browsers pre-opening sockets, on which HTTPServer would wait indefinitely" ([http.server][http-server]).
  This fits SSE, because every open tab holds a thread for its stream.
- Shutdown with streams open: `ThreadingMixIn.daemon_threads` defaults to `False`, "meaning that Python will not exit
  until all threads created by ThreadingMixIn have exited". `server_close` "waits until all non-daemon threads
  complete, except if block_on_close attribute is False" ([socketserver][socketserver]). Setting
  `daemon_threads = True` makes Ctrl-C and pane-close exit at once.
- `protocol_version` defaults to `HTTP/1.0`. HTTP/1.1 requires "an accurate Content-Length header … in all of its
  responses" ([http.server][http-server]). An SSE stream at HTTP/1.0 is close-delimited, which `EventSource`
  accepts, so keep the default and don't hand-roll chunked encoding.
- The docs warn that "http.server is not recommended for production. It only implements basic security checks"
  ([http.server][http-server]). Here it serves one user on `127.0.0.1`, and the map already requires our own token
  and Host-header check on every request. A one-line check in a `BaseHTTPRequestHandler` subclass covers both. The
  warning is about public exposure, which the map rules out.
- **Measured** with a throwaway prototype (a poller thread, a `Condition`, `daemon_threads = True`, port 0). Three
  concurrent `http.client` "tabs" each received the same `id:`/`event: board`/`data:` frames from one poll.
  `shutdown()` + `server_close()` with all three streams still open returned in **0.30 s**.
- What we write ourselves: SSE framing (`id:`, `event:`, `data:` lines, `retry:`, a `:` comment keep-alive),
  routing for about 5 paths, and serving static package files via `importlib.resources`. That is about 100-150
  lines.

### Starlette + uvicorn

- Starlette has no SSE response of its own. `StreamingResponse` "takes an async generator or a normal
  generator/iterator and streams the response body", and SSE is left to the third-party `sse-starlette`
  `EventSourceResponse` ([Starlette responses][starlette-responses]). Writing the frames by hand in a
  `StreamingResponse` works, as the prototype did.
- Uvicorn can run inside an existing loop: "If you'd like to run Uvicorn from an already running async environment,
  use uvicorn.Server.serve() instead" ([uvicorn docs][uvicorn-index]). `--port 0` picks a free port
  ([uvicorn settings][uvicorn-settings]).
- **Shutdown with an open SSE stream (measured).** With default settings, setting `server.should_exit` while one
  client held `/events` open left `serve()` **still waiting after 8 s**. A graceful shutdown waits for the
  never-ending response. With `timeout_graceful_shutdown=1` ("Maximum number of seconds to wait for graceful
  shutdown. After this timeout, the server will start terminating requests" ([uvicorn settings][uvicorn-settings])),
  it stopped in 1.15 s but logged `ERROR: Cancel 1 running task(s), timeout graceful shutdown exceeded` and two
  `CancelledError` tracebacks. Every stop with a tab open would need the timeout set plus log handling, or
  `sse-starlette`'s shutdown handling.
- Testing: `TestClient` "is built on httpx2. Plain httpx is still supported, but deprecated". It is a test-only
  dependency, and the test transport "does not emulate … concurrent request and response streaming"
  ([Starlette TestClient][starlette-testclient]). An infinite SSE stream is therefore awkward to test through it, and
  a real uvicorn on port 0 is the safer test anyway.

### aiohttp

- `web.StreamResponse` streams after `prepare()`, and "each write() operation [is] encoded in separate chunk"
  ([aiohttp web reference][aiohttp-web]). SSE is a separate package (`aiohttp-sse`) or hand-written framing.
- Testing is built in: `TestClient(TestServer(app))` runs on a random port with no plugin, and `AioHTTPTestCase`
  derives from `unittest.IsolatedAsyncioTestCase` ([aiohttp testing][aiohttp-testing]). These are the best test
  tools of the three, but they come with the heaviest dependency set (table above).
- Like Starlette, aiohttp gives the board an asyncio server process to run a threaded, blocking fetch loop in.

### SSE support and reconnection (protocol facts that hold for every server)

- The response must be `200` with MIME type `text/event-stream`. Otherwise the client will "fail the connection",
  and "once the user agent has failed the connection, it does not attempt to reconnect". A `204 No Content` tells the
  client to stop reconnecting ([HTML spec, SSE][whatwg-sse]). This is useful for a board that has been replaced: it
  returns 204 and the old tab goes quiet instead of retrying forever.
- Clients reconnect on their own after a delay that the server can set with `retry:`. The client sends back the last
  `id:` as `Last-Event-ID` ([HTML spec, SSE][whatwg-sse]). Each event carries the whole board, so the server can
  ignore `Last-Event-ID` and send the current board on every connect.
- Keep-alive: "include a comment line (one starting with a ':' character) every 15 seconds or so"
  ([HTML spec, SSE][whatwg-sse]). In the stdlib design, this is the `Condition.wait(timeout=15)` branch.
- Connection limit: over HTTP/1.x a browser allows only **6** SSE connections "per browser + domain", across all
  tabs, and Chrome and Firefox have marked this "Won't fix" ([MDN EventSource][mdn-eventsource]). Each web board has
  its own random port, so this only bites at a seventh tab of the same board. Close the stream on `pagehide`, and
  treat a seventh tab as out of scope. No stack here serves HTTP/2 on plain localhost, so the choice of server
  doesn't change this.
- `EventSource` is Baseline "widely available … since January 2020" ([MDN EventSource][mdn-eventsource]).

## Front-end candidates

### Vanilla JS with `EventSource`

- It is native to the browser, needs nothing vendored, and reconnects as the spec describes above.
- The page needs a few behaviours of its own:
  - Swap the lanes fragment on each `board` event.
  - Re-apply the filter field and chips, and keep the open detail panel and the selection by issue number.
  - POST the Start confirm, with the token, to an endpoint.
- None of this needs a framework.

### Vendored htmx with its SSE extension

- **htmx 2** (2.0.11): `htmx.min.js` is 52.2 KB and `htmx-ext-sse` 2.2.4 `sse.min.js` is 2.9 KB (measured from the
  npm packages). The extension uses a browser `EventSource` with its own exponential-backoff reconnect on top
  (`sse-connect`, `sse-swap`, `hx-trigger="sse:<name>"`) ([htmx 2 SSE extension][htmx2-sse]).
- **htmx 4.0.0** shipped in late August 2026 ([InfoQ][infoq-htmx4]). Its SSE extension was "significantly
  rewritten" ([what's new in htmx 4][htmx4-new]). It now uses `fetch()` + `ReadableStream` "instead of
  EventSource", with its own reconnect (500 ms doubling to a 60 s cap, ±30% jitter, `retry:` honoured,
  `Last-Event-ID` sent, and the stream paused while the page is hidden). It is a separate file (`hx-sse:connect`),
  and its page is still headed "under construction" ([htmx 4 hx-sse][htmx4-sse]). `htmx.min.js` 4.0.0 is 36.7 KB and
  `ext/hx-sse.min.js` is 6.2 KB (measured).
- **Why not:** the board needs one stream and one swap target. htmx's attribute model is aimed at many small
  server round trips. It also doesn't help with what the page needs most, which is keeping client-side filter and
  panel state across swaps. That needs our JS either way, or a morphing extension (the htmx 2 SSE page notes
  idiomorph support). The timing is also poor: htmx 2 has been superseded, and htmx 4's SSE layer is new, has a
  different transport and attribute names, and its docs are still being written. Vendoring either one means
  tracking its upgrades by hand, because there is no CDN to bump.

### Testability without a real browser

- **Server side (all stacks):** the stdlib server starts in a pytest test on port 0 in a thread, and plain
  `http.client` reads its stream. This is exactly how the prototype exercised it, with no new test dependency
  (`pyproject.toml`'s dev extra is just `pytest`). The token and Host-header refusals, the 204 for a replaced
  board, the Start POST and the "one poll, N streams" broadcast can all be asserted this way. Starlette would add
  `httpx2`, and aiohttp would bring its own test client.
- **Page side:** server-rendered HTML fragments keep the board's rendering in Python, where `pytest` can assert on
  it next to the TUI tests that share the board model. Vanilla JS keeps the untested browser surface to a small
  swap-and-reapply script. A JSON-plus-client-render design, or htmx's attribute wiring, would put more behaviour
  where only a real browser can test it. The split between model, HTTP layer and page is left open by the map
  ("Not yet specified"). This only argues for keeping the JS thin.

## Sources

- [Python docs, `http.server`][http-server]
- [Python docs, `socketserver` (`daemon_threads`, `block_on_close`)][socketserver]
- [WHATWG HTML Living Standard, §9.2 Server-sent events][whatwg-sse]
- [MDN, `EventSource` (connection limit, Baseline)][mdn-eventsource]
- [Starlette docs, Responses][starlette-responses]
- [Starlette docs, Test Client][starlette-testclient]
- [Starlette dependencies][starlette-pyproject]
- [uvicorn docs, programmatic `Config`/`Server.serve()`][uvicorn-index]
- [uvicorn settings (`--port 0`, `--timeout-graceful-shutdown`)][uvicorn-settings]
- [uvicorn dependencies][uvicorn-pyproject]
- [aiohttp server reference, `StreamResponse`][aiohttp-web]
- [aiohttp testing][aiohttp-testing]
- [htmx 2 SSE extension][htmx2-sse]
- [htmx 4 `hx-sse` extension][htmx4-sse]
- [Changes in htmx 4.0][htmx4-new]
- [InfoQ, "htmx 4 released" (release timing only)][infoq-htmx4]
- Measurements: fresh venvs with `pip install --no-cache-dir`, and throwaway SSE prototypes for the stdlib
  (`ThreadingHTTPServer`, 3 clients, shutdown) and Starlette/uvicorn (`Server.serve()`, shutdown with and without
  `timeout_graceful_shutdown`). JS sizes are from the published npm files via jsDelivr. All run on 2026-10-16.

[http-server]: https://docs.python.org/3/library/http.server.html
[socketserver]: https://docs.python.org/3/library/socketserver.html
[whatwg-sse]: https://html.spec.whatwg.org/multipage/server-sent-events.html
[mdn-eventsource]: https://developer.mozilla.org/en-US/docs/Web/API/EventSource
[starlette-responses]: https://github.com/encode/starlette/blob/master/docs/responses.md
[starlette-testclient]: https://github.com/encode/starlette/blob/master/docs/testclient.md
[starlette-pyproject]: https://github.com/encode/starlette/blob/master/pyproject.toml
[uvicorn-index]: https://github.com/encode/uvicorn/blob/master/docs/index.md
[uvicorn-settings]: https://github.com/encode/uvicorn/blob/master/docs/settings.md
[uvicorn-pyproject]: https://github.com/encode/uvicorn/blob/master/pyproject.toml
[aiohttp-web]: https://docs.aiohttp.org/en/stable/web_reference.html
[aiohttp-testing]: https://docs.aiohttp.org/en/stable/testing.html
[htmx2-sse]: https://htmx.org/extensions/sse/
[htmx4-sse]: https://four.htmx.org/extensions/hx-sse
[htmx4-new]: https://four.htmx.org/whats-new-in-htmx-4
[infoq-htmx4]: https://www.infoq.com/news/2026/09/htmx-4-released/
