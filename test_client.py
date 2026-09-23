#!/usr/bin/env python3
"""
test_client.py — mark the calculator the way the assignment says it will be marked.

    one socket. every request.

        s = socket.create_connection(("localhost", 8080))

        GET /add?a=2&b=3   -> 200  5
        GET /sub?a=10&b=4  -> 200  6
        GET /mul?a=6&b=7   -> 200  42
        GET /div?a=1&b=0   -> 400
        GET /pow?a=2&b=8   -> 404
        POST /add          -> 405

        socket still open: True
        1 TCP handshake, 6 responses

Run the server first:   python3 server.py
Then:                   python3 test_client.py

Both accept an optional port argument.
"""

import socket
import sys

HOST = "localhost"
PORT = 8080


class ResponseReader:
    """
    Reads responses off a persistent connection. Same framing discipline as the
    server: find the blank line, read exactly Content-Length body bytes, and
    leave everything after that in the buffer for the next response.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed the connection unexpectedly")
        self.buf.extend(chunk)

    def read_response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        idx = self.buf.find(b"\r\n\r\n")
        head = bytes(self.buf[:idx]).decode("iso-8859-1")
        del self.buf[: idx + 4]

        lines = head.split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()

        n = int(headers.get("content-length", 0))
        while len(self.buf) < n:
            self._fill()
        body = bytes(self.buf[:n]).decode("utf-8")
        del self.buf[:n]
        return status, headers, body


def send_raw(sock, text):
    sock.sendall(text.encode("iso-8859-1"))


def main():
    # Accept `test_client.py PORT` or `test_client.py HOST PORT`.
    args = sys.argv[1:]
    if len(args) == 1:
        host, port = HOST, int(args[0])
    elif len(args) >= 2:
        host, port = args[0], int(args[1])
    else:
        host, port = HOST, PORT

    # ---- exactly ONE TCP handshake ----
    s = socket.create_connection((host, port), timeout=5)
    reader = ResponseReader(s)

    cases = [
        ("GET /add?a=2&b=3",  f"GET /add?a=2&b=3 HTTP/1.1\r\nHost: {host}\r\n\r\n",  200, "5"),
        ("GET /sub?a=10&b=4", f"GET /sub?a=10&b=4 HTTP/1.1\r\nHost: {host}\r\n\r\n", 200, "6"),
        ("GET /mul?a=6&b=7",  f"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: {host}\r\n\r\n",  200, "42"),
        ("GET /div?a=1&b=0",  f"GET /div?a=1&b=0 HTTP/1.1\r\nHost: {host}\r\n\r\n",  400, None),
        ("GET /pow?a=2&b=8",  f"GET /pow?a=2&b=8 HTTP/1.1\r\nHost: {host}\r\n\r\n",  404, None),
        ("POST /add",         f"POST /add HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n", 405, None),
    ]

    passed = 0
    print(f"connected to {host}:{port} — one socket, {len(cases)} requests\n")
    for label, raw, want_status, want_body in cases:
        send_raw(s, raw)
        status, headers, body = reader.read_response()
        body = body.strip()

        ok = status == want_status and (want_body is None or body == want_body)
        passed += ok
        shown = f"{status}" + (f"  {body}" if body and status == 200 else "")
        mark = "ok  " if ok else "FAIL"
        expect = f"{want_status}" + (f"  {want_body}" if want_body else "")
        print(f"  {mark} {label:<22} -> {shown:<10} (expected {expect})")
        if not ok:
            print(f"       body was: {body!r}")

    # ---- is the line still open? ----
    # Send one more request down the same socket. If the server really stayed
    # on the line, this succeeds without a new handshake.
    still_open = True
    try:
        send_raw(s, f"GET /add?a=20&b=22 HTTP/1.1\r\nHost: {host}\r\n\r\n")
        status, _, body = reader.read_response()
        still_open = (status == 200 and body.strip() == "42")
    except Exception as e:
        still_open = False
        print(f"\n  reuse probe failed: {e}")

    print(f"\n  socket still open: {still_open}")
    print(f"  1 TCP handshake, {len(cases) + 1} responses")

    # ---- extra credit, all optional in the brief ----
    print("\n  stretch checks:")

    # Pipelining: fire three requests back-to-back without waiting, then read
    # three answers in order. This only works if the server frames requests
    # correctly rather than relying on one-recv-per-request.
    send_raw(
        s,
        f"GET /add?a=1&b=1 HTTP/1.1\r\nHost: {host}\r\n\r\n"
        f"GET /mul?a=3&b=3 HTTP/1.1\r\nHost: {host}\r\n\r\n"
        f"GET /sub?a=9&b=4 HTTP/1.1\r\nHost: {host}\r\n\r\n",
    )
    got = [reader.read_response()[2].strip() for _ in range(3)]
    pipeline_ok = got == ["2", "9", "5"]
    print(f"    {'ok  ' if pipeline_ok else 'FAIL'} pipelining (3 at once, in order) -> {got}")

    # Body framing: a GET carrying a declared body. The server must swallow
    # exactly those bytes, so the NEXT request parses cleanly.
    body = "ignore-me"
    send_raw(
        s,
        f"GET /add?a=5&b=5 HTTP/1.1\r\nHost: {host}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n{body}"
        f"GET /mul?a=2&b=5 HTTP/1.1\r\nHost: {host}\r\n\r\n",
    )
    r1 = reader.read_response()[2].strip()
    r2 = reader.read_response()[2].strip()
    framing_ok = (r1 == "10" and r2 == "10")
    print(f"    {'ok  ' if framing_ok else 'FAIL'} Content-Length framing (body drained) -> {r1}, {r2}")

    # Chunked request body. The server must decode the chunk sequence and
    # stop on the byte after the terminator, so the request glued on behind
    # it still parses. Sent as one write to make that the real test.
    send_raw(
        s,
        f"POST /add?a=7&b=8 HTTP/1.1\r\nHost: {host}\r\n"
        "Transfer-Encoding: chunked\r\n\r\n"
        "5\r\nhello\r\n"
        "6\r\n world\r\n"
        "0\r\n\r\n"
        f"GET /add?a=7&b=8 HTTP/1.1\r\nHost: {host}\r\n\r\n",
    )
    # POST is still 405 -- the point is that the body was consumed correctly,
    # which the GET behind it proves by returning 15.
    st1 = reader.read_response()[0]
    st2, _, b2 = reader.read_response()
    chunked_ok = (st1 == 405 and st2 == 200 and b2.strip() == "15")
    print(f"    {'ok  ' if chunked_ok else 'FAIL'} chunked body decoded, next request clean "
          f"-> {st1}, {st2} {b2.strip()}")

    # Connection: close must actually close.
    send_raw(s, f"GET /add?a=2&b=3 HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")
    status, headers, _ = reader.read_response()
    advertised = headers.get("connection", "").lower() == "close"
    leftover = s.recv(4096)
    closed = (leftover == b"")
    print(f"    {'ok  ' if advertised and closed else 'FAIL'} Connection: close honoured "
          f"(header={headers.get('connection')}, socket closed={closed})")
    s.close()

    # Missing Host is a separate connection, since it closes the line.
    s2 = socket.create_connection((host, port), timeout=5)
    r2reader = ResponseReader(s2)
    send_raw(s2, "GET /add?a=2&b=3 HTTP/1.1\r\n\r\n")
    status, _, _ = r2reader.read_response()
    print(f"    {'ok  ' if status == 400 else 'FAIL'} GET /add with no Host -> {status} (expected 400)")
    s2.close()

    # Non-numeric argument.
    s3 = socket.create_connection((host, port), timeout=5)
    r3reader = ResponseReader(s3)
    send_raw(s3, f"GET /add?a=x&b=3 HTTP/1.1\r\nHost: {host}\r\n\r\n")
    status, _, _ = r3reader.read_response()
    print(f"    {'ok  ' if status == 400 else 'FAIL'} GET /add?a=x&b=3 -> {status} (expected 400)")
    s3.close()

    print()
    all_core_ok = passed == len(cases) and still_open
    print("RESULT:", "all core cases passed" if all_core_ok
          else f"{passed}/{len(cases)} core cases passed, socket open={still_open}")
    return 0 if all_core_ok else 1


if __name__ == "__main__":
    sys.exit(main())
