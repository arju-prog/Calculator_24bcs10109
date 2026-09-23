# Calculator that stays on the line

An HTTP/1.1 calculator served straight off a TCP socket. Python 3 standard
library only — no framework, no `http.server`, nothing but `socket`.

The arithmetic is not the point. Holding the connection open is.

## What it answers

| Request | Status | Body |
|---|---|---|
| `GET /add?a=2&b=3` | 200 | `5` |
| `GET /sub?a=10&b=4` | 200 | `6` |
| `GET /mul?a=6&b=7` | 200 | `42` |
| `GET /div?a=9&b=3` | 200 | `3` |
| `GET /div?a=1&b=0` | 400 | division by zero |
| `GET /add?a=x&b=3` | 400 | arguments must be numeric |
| `GET /pow?a=2&b=8` | 404 | no such operation |
| `POST /add` | 405 | method not allowed (`Allow: GET`) |
| `GET /add` with no `Host` | 400 | HTTP/1.1 requires a Host header |

## Running it

Two terminals:

```bash
python3 server.py            # listens on 8080
python3 test_client.py       # marks it
```

Both take an optional port (`python3 server.py 9090`), and the server also
accepts `host port`.

## Result

```
connected to localhost:8080 — one socket, 6 requests

  ok   GET /add?a=2&b=3       -> 200  5     (expected 200  5)
  ok   GET /sub?a=10&b=4      -> 200  6     (expected 200  6)
  ok   GET /mul?a=6&b=7       -> 200  42    (expected 200  42)
  ok   GET /div?a=1&b=0       -> 400        (expected 400)
  ok   GET /pow?a=2&b=8       -> 404        (expected 404)
  ok   POST /add              -> 405        (expected 405)

  socket still open: True
  1 TCP handshake, 7 responses

  stretch checks:
    ok   pipelining (3 at once, in order) -> ['2', '9', '5']
    ok   Content-Length framing (body drained) -> 10, 10
    ok   chunked body decoded, next request clean -> 405, 200 15
    ok   Connection: close honoured (header=close, socket closed=True)
    ok   GET /add with no Host -> 400 (expected 400)
    ok   GET /add?a=x&b=3 -> 400 (expected 400)

RESULT: all core cases passed
```

The client's `socket still open: True` is its own word for it. The server's log
is the independent proof — **one client port, fifteen requests**:

```
[127.0.0.1:53557] GET /add?a=2&b=3 -> 200 5
[127.0.0.1:53557] GET /sub?a=10&b=4 -> 200 6
[127.0.0.1:53557] GET /mul?a=6&b=7 -> 200 42
[127.0.0.1:53557] GET /div?a=1&b=0 -> 400 division by zero
[127.0.0.1:53557] GET /pow?a=2&b=8 -> 404 no such operation
[127.0.0.1:53557] POST /add -> 405 method not allowed
[127.0.0.1:53557] GET /add?a=20&b=22 -> 200 42        <- reuse probe
[127.0.0.1:53557] GET /add?a=1&b=1 -> 200 2           <- pipelined
[127.0.0.1:53557] GET /mul?a=3&b=3 -> 200 9           <- pipelined
[127.0.0.1:53557] GET /sub?a=9&b=4 -> 200 5           <- pipelined
[127.0.0.1:53557] GET /add?a=5&b=5 -> 200 10          <- carried a body
[127.0.0.1:53557] GET /mul?a=2&b=5 -> 200 10          <- the request behind it
[127.0.0.1:53557] POST /add?a=7&b=8 -> 405            <- chunked body
[127.0.0.1:53557] GET /add?a=7&b=8 -> 200 15          <- the request behind it
[127.0.0.1:53557] GET /add?a=2&b=3 -> 200 5
[127.0.0.1:53557] honouring Connection: close
```

One accept, one port, fifteen exchanges, closed only when asked. The two
remaining connections in a full run are deliberate: the missing-`Host` and
non-numeric cases each close the line, so they get a socket of their own.

## The part that is actually hard

With HTTP/1.0 you hung up after each reply, so "where does this request end?"
answered itself: at EOF, for free. Keep the connection open and nobody answers
it for you. One `recv()` is not one request — it can hand you half a request,
or three of them glued together.

So the socket is wrapped in a small buffered reader (`SocketReader`) that owns
the leftovers:

- **`read_until_blank_line()`** scans for `CRLFCRLF`, returns the header block,
  and leaves everything after it in the buffer.
- **`read_exactly(n)`** slices off exactly `n` body bytes and leaves the rest.
- **`read_chunked()`** walks the chunk sequence and stops on the byte after the
  terminator.

Byte *n+1* is the next request's first byte and belongs to somebody else.

Two of the tests exist only to catch an off-by-one here. Each sends a request
**with a body** immediately followed by another request **in the same write**.
Read one byte too few or too many and the second request is garbage; both come
back `10` and `15`.

## Stretch goals

All four, and each one is tested rather than asserted.

**`Connection: close`** — honoured in both directions. The server echoes
`Connection: close`, sends the response, and shuts down; HTTP/1.0 clients get
the same treatment unless they ask for `keep-alive`.

**An idle timeout you can defend** — 10 seconds between requests, then a `408`
and a close. The number is a tradeoff, not a default: it bounds how long a
vanished peer can pin a thread, while being far longer than any gap a live
client leaves between requests. Verified:

```
first response ok: True
now idling past the 10s timeout...
after 10.0s server sent: b'HTTP/1.1 408 Request Timeout'
socket closed by server: True
```

**Chunked encoding** — decoded for real, chunk extensions and trailers
included. `Transfer-Encoding` beats `Content-Length` per RFC 7230 §3.3.3, and a
request carrying both is rejected as a smuggling attempt rather than guessed at.

**Pipelining** — falls out of the buffered reader for free. Three requests in
one write, three answers in order: `['2', '9', '5']`.

## Files

| File | |
|---|---|
| `server.py` | the server |
| `test_client.py` | one socket, every request, plus the stretch checks |
