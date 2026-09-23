#!/usr/bin/env python3
"""
server.py — an HTTP/1.1 calculator that stays on the line.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3

    GET /div?a=1&b=0   -> 400   (division by zero)
    GET /add?a=x&b=3   -> 400   (non-numeric argument)
    GET /pow?a=2&b=8   -> 404   (no such operation)
    POST /add          -> 405   (wrong method)
    GET /add (no Host) -> 400   (HTTP/1.1 requires a Host header)

No framework. One socket. Every request read off the same connection until
the client hangs up, we time out, or somebody sends `Connection: close`.

The whole point of the exercise lives in SocketReader: because the connection
stays open, "where does this request end and the next begin?" is no longer
answered by EOF. We must consume exactly the bytes that belong to this
request -- the blank line that ends the headers, then exactly Content-Length
body bytes (or the chunked sequence), and not one byte more. Byte n+1 is the
next request and belongs to somebody else.

Usage:
    python3 server.py [port]                  # defaults to 8080
    python3 server.py [host] [port]
"""

import socket
import sys
import threading
import urllib.parse

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
IDLE_TIMEOUT = 10.0          # seconds a connection may sit idle between
                             # requests before we close it (a timeout we can
                             # defend: it bounds how long a dead peer ties up
                             # a thread, without punishing a slow-but-alive one)
MAX_HEADER_BYTES = 64 * 1024 # refuse an unbounded header section
MAX_BODY_BYTES = 1024 * 1024 # refuse an unbounded body

OPS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a / b,   # ZeroDivisionError handled by caller
}


# ---------------------------------------------------------------------------
# A tiny buffered reader over a blocking socket.
#
# We cannot just call recv() per request: one recv() may return several
# pipelined requests glued together, or half of one. So we keep a byte buffer,
# hand out exactly what each request needs, and leave the rest for the next
# read. This is the machinery that lets us "stay on the line".
# ---------------------------------------------------------------------------
class SocketReader:
    def __init__(self, conn):
        self.conn = conn
        self.buf = bytearray()

    def _fill(self):
        """Pull more bytes from the socket. Returns False on clean EOF."""
        chunk = self.conn.recv(4096)
        if not chunk:
            return False
        self.buf.extend(chunk)
        return True

    def read_until_blank_line(self):
        """
        Return the raw header block (request line + headers) WITHOUT the
        terminating CRLFCRLF, leaving any body bytes already received in the
        buffer. Returns None on clean EOF (peer hung up between requests).
        Raises ValueError if the header section grows past MAX_HEADER_BYTES.
        """
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx != -1:
                head = bytes(self.buf[:idx])
                del self.buf[: idx + 4]       # drop head + the blank line
                return head
            # Some clients (and telnet) send bare LF line endings. Accept them.
            idx = self.buf.find(b"\n\n")
            if idx != -1:
                head = bytes(self.buf[:idx])
                del self.buf[: idx + 2]
                return head
            if len(self.buf) > MAX_HEADER_BYTES:
                raise ValueError("header section too large")
            if not self._fill():
                # EOF. If we have leftover bytes it's a truncated request;
                # if the buffer is empty it's a clean close between requests.
                if self.buf:
                    raise ValueError("connection closed mid-headers")
                return None

    def read_exactly(self, n):
        """
        Return exactly n body bytes, reading more from the socket if needed.
        This is the "consume Content-Length bytes and not one more" rule:
        we slice n bytes off the front and leave the remainder in the buffer
        for the NEXT request to read.
        """
        while len(self.buf) < n:
            if not self._fill():
                raise ValueError("connection closed mid-body")
        body = bytes(self.buf[:n])
        del self.buf[:n]
        return body

    def read_line(self):
        """Read one CRLF-terminated line, returning it without the CRLF."""
        while True:
            idx = self.buf.find(b"\r\n")
            if idx != -1:
                line = bytes(self.buf[:idx])
                del self.buf[: idx + 2]
                return line
            if len(self.buf) > MAX_HEADER_BYTES:
                raise ValueError("line too long")
            if not self._fill():
                raise ValueError("connection closed mid-line")

    def read_chunked(self):
        """
        Decode a chunked body (RFC 7230 section 4.1) and return the
        reassembled bytes.

            <hex size>[;ext] CRLF <size bytes> CRLF ... 0 CRLF <trailers> CRLF

        Same obligation as Content-Length, in a different shape: the body's
        end is discovered as we go rather than known up front, and we must
        stop on exactly the byte after the terminating sequence so the next
        request on this connection starts clean.
        """
        body = bytearray()
        while True:
            line = self.read_line()
            size_field = line.split(b";", 1)[0].strip()   # drop chunk extensions
            try:
                size = int(size_field, 16)
            except ValueError:
                raise ValueError("bad chunk size")
            if size < 0:
                raise ValueError("negative chunk size")
            if len(body) + size > MAX_BODY_BYTES:
                raise ValueError("chunked body too large")
            if size == 0:
                # Last chunk. Consume trailers up to the closing blank line.
                while True:
                    trailer = self.read_line()
                    if trailer == b"":
                        break
                return bytes(body)
            body += self.read_exactly(size)
            if self.read_exactly(2) != b"\r\n":
                raise ValueError("chunk not CRLF-terminated")


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
class BadRequest(Exception):
    """400 -- the frame itself is malformed or violates HTTP/1.1."""


class Request:
    __slots__ = ("method", "target", "version", "path", "query", "headers")


def parse_request(head_bytes):
    """Turn a raw header block into a Request. Raises BadRequest on garbage."""
    try:
        text = head_bytes.decode("iso-8859-1")
    except Exception:
        raise BadRequest("undecodable header bytes")

    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not lines[0]:
        raise BadRequest("empty request line")

    # Request line:  METHOD SP target SP HTTP/x.y
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise BadRequest("malformed request line")
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise BadRequest("not an HTTP request line")

    # Headers. A duplicate Host, or a header with no colon, is malformed.
    headers = {}
    for line in lines[1:]:
        if line == "":
            continue
        if ":" not in line:
            raise BadRequest("header without colon")
        name, _, value = line.partition(":")
        name = name.strip().lower()
        if name == "" :
            raise BadRequest("empty header name")
        # Last-wins is fine for most, but a repeated Host is a request-
        # smuggling smell -- reject it.
        if name == "host" and "host" in headers:
            raise BadRequest("duplicate Host header")
        headers[name] = value.strip()

    split = urllib.parse.urlsplit(target)
    req = Request()
    req.method = method
    req.target = target
    req.version = version
    req.path = split.path
    req.query = urllib.parse.parse_qs(split.query)
    req.headers = headers
    return req


# ---------------------------------------------------------------------------
# Response building
# ---------------------------------------------------------------------------
REASON = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    505: "HTTP Version Not Supported",
}


def build_response(status, body_text="", *, close=False, extra_headers=None):
    """
    Serialize a full HTTP/1.1 response. We ALWAYS send Content-Length so the
    client knows exactly where our response ends -- the same framing rule we
    impose on requests, applied to ourselves.
    """
    body = body_text.encode("utf-8")
    headers = [
        f"HTTP/1.1 {status} {REASON.get(status, 'Unknown')}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
    ]
    if close:
        headers.append("Connection: close")
    else:
        headers.append("Connection: keep-alive")
        headers.append(f"Keep-Alive: timeout={int(IDLE_TIMEOUT)}")
    for k, v in (extra_headers or {}):
        headers.append(f"{k}: {v}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body


# ---------------------------------------------------------------------------
# The calculator itself
# ---------------------------------------------------------------------------
def handle_calc(req):
    """
    Apply HTTP semantics, then arithmetic. Returns (status, body_text).
    Order of checks matters and mirrors the marking table:
      - unknown operation  -> 404  (resource doesn't exist)
      - wrong method       -> 405  (resource exists, verb doesn't)
      - bad arguments / /0 -> 400  (request is well-formed HTTP but unprocessable)
    """
    if req.path not in OPS:
        return 404, "no such operation\n"

    if req.method != "GET":
        # The resource exists but only answers GET. 405 MUST advertise Allow.
        return 405, "method not allowed\n"

    # Extract and validate arguments.
    a_vals = req.query.get("a")
    b_vals = req.query.get("b")
    if not a_vals or not b_vals:
        return 400, "missing argument a or b\n"
    try:
        a = float(a_vals[0])
        b = float(b_vals[0])
    except ValueError:
        return 400, "arguments must be numeric\n"

    try:
        result = OPS[req.path](a, b)
    except ZeroDivisionError:
        return 400, "division by zero\n"

    # Present whole numbers as integers (5, not 5.0) to match the examples.
    if result == int(result):
        result = int(result)
    return 200, f"{result}\n"


# ---------------------------------------------------------------------------
# Per-connection loop -- this is where we "stay on the line"
# ---------------------------------------------------------------------------
def serve_connection(conn, addr):
    conn.settimeout(IDLE_TIMEOUT)
    reader = SocketReader(conn)
    peer = f"{addr[0]}:{addr[1]}"
    try:
        while True:
            # ---- read one request frame off the shared connection ----
            try:
                head = reader.read_until_blank_line()
            except socket.timeout:
                # Idle too long between requests. Tell them, then close.
                conn.sendall(build_response(408, "request timeout\n", close=True))
                print(f"[{peer}] idle timeout, closing")
                return
            except ValueError as e:
                conn.sendall(build_response(400, f"{e}\n", close=True))
                print(f"[{peer}] framing error: {e}, closing")
                return

            if head is None:
                print(f"[{peer}] client closed connection")
                return   # clean EOF between requests -- the happy ending

            # ---- parse ----
            try:
                req = parse_request(head)
            except BadRequest as e:
                conn.sendall(build_response(400, f"{e}\n", close=True))
                print(f"[{peer}] 400 bad request: {e}")
                return

            # ---- HTTP/1.1 rule: Host is mandatory ----
            if req.version == "HTTP/1.1" and "host" not in req.headers:
                conn.sendall(build_response(400, "missing Host header\n", close=True))
                print(f"[{peer}] 400 missing Host")
                return

            if req.version not in ("HTTP/1.1", "HTTP/1.0"):
                conn.sendall(build_response(505, "unsupported HTTP version\n", close=True))
                return

            # ---- consume the body, if any, so byte n+1 stays with its owner ----
            # A GET has no semantic body, but if the client declared one we MUST
            # drain it exactly, or the next request on this connection is
            # garbage. Transfer-Encoding wins over Content-Length (RFC 7230
            # section 3.3.3); a request carrying both is a smuggling attempt.
            te = req.headers.get("transfer-encoding", "").lower().strip()
            has_cl = "content-length" in req.headers

            if te and te != "identity":
                if te != "chunked":
                    conn.sendall(build_response(
                        400, "unsupported Transfer-Encoding\n", close=True))
                    return
                if has_cl:
                    conn.sendall(build_response(
                        400, "both Transfer-Encoding and Content-Length\n", close=True))
                    return
                try:
                    _body = reader.read_chunked()
                except ValueError as e:
                    conn.sendall(build_response(400, f"{e}\n", close=True))
                    print(f"[{peer}] chunked decode failed: {e}")
                    return
            else:
                body_len = 0
                if has_cl:
                    try:
                        body_len = int(req.headers["content-length"])
                        if body_len < 0:
                            raise ValueError
                    except ValueError:
                        conn.sendall(build_response(400, "bad Content-Length\n", close=True))
                        return
                    if body_len > MAX_BODY_BYTES:
                        conn.sendall(build_response(413, "body too large\n", close=True))
                        return
                try:
                    _body = reader.read_exactly(body_len)   # drained, then discarded
                except ValueError as e:
                    print(f"[{peer}] {e}")
                    return

            # ---- decide whether the peer wants to keep the line open ----
            conn_hdr = req.headers.get("connection", "").lower()
            client_wants_close = (
                conn_hdr == "close"
                or (req.version == "HTTP/1.0" and conn_hdr != "keep-alive")
            )

            # ---- produce the answer ----
            status, body_text = handle_calc(req)
            extra = []
            if status == 405:
                extra.append(("Allow", "GET"))
            resp = build_response(
                status, body_text,
                close=client_wants_close,
                extra_headers=extra,
            )
            conn.sendall(resp)
            print(f"[{peer}] {req.method} {req.target} -> {status} {body_text.strip()}")

            if client_wants_close:
                print(f"[{peer}] honouring Connection: close")
                return
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Accept loop
# ---------------------------------------------------------------------------
def main():
    # Accept either `server.py PORT` or `server.py HOST PORT`.
    args = sys.argv[1:]
    if len(args) == 1:
        host, port = "127.0.0.1", int(args[0])
    elif len(args) >= 2:
        host, port = args[0], int(args[1])
    else:
        host, port = "127.0.0.1", 8080

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    print(f"calculator listening on {host}:{port}  (Ctrl-C to stop)")

    try:
        while True:
            conn, addr = srv.accept()
            # One thread per connection. The connection -- not the request --
            # is the unit of work, which is the whole idea of HTTP/1.1.
            t = threading.Thread(
                target=serve_connection, args=(conn, addr), daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
