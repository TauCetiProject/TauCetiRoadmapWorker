#!/usr/bin/env python3
"""Allow-everything-and-log forward proxy for worker agents.

    python3 -I allowproxy.py [--port 3128] [--dir DIR]

Listens on 127.0.0.1:<port> and on a unix socket ($XDG_RUNTIME_DIR/allowproxy.sock) for agents in
a network jail. Handles CONNECT host:port (TLS tunnels) and plain HTTP. Every host is allowed and
logged to DIR/access.log, except the hosts in the deny lists: the bundled deny.txt beside this file
(exfiltration channels and pirate mirrors) and DIR/deny.txt (the operator's additions). Refusals are
logged to DIR/deny.log. The lists are re-read on every request, so editing a file is enough.

DIR defaults to $ALLOWPROXY_DIR, else ~/.local/state/tauceti/allowproxy.
Deny-list syntax: one host per line; a leading dot covers every subdomain; '#' starts a comment.
"""
import argparse
import os
import re
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
STATE = {"dir": ""}


def state_dir():
    return STATE["dir"] or os.environ.get("ALLOWPROXY_DIR") or os.path.expanduser("~/.local/state/tauceti/allowproxy")


def deny_rules():
    rules = []
    for path in (os.path.join(HERE, "deny.txt"), os.path.join(state_dir(), "deny.txt")):
        try:
            with open(path) as f:
                rules += [line.split("#", 1)[0].strip().lower() for line in f]
        except OSError:
            pass
    return [r for r in rules if r]


def allowed(host, rules=None):
    host = host.lower().rstrip(".")
    for r in deny_rules() if rules is None else rules:
        if r.startswith("."):
            if host == r[1:] or host.endswith(r):
                return False
        elif host == r:
            return False
    return True


def log(name, line):
    with LOCK:
        with open(os.path.join(state_dir(), name), "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + line + "\n")


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(c):
    try:
        c.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            d = c.recv(65536)
            if not d:
                return
            head += d
            if len(head) > 65536:
                return
        hdr, _, rest = head.partition(b"\r\n\r\n")
        line = hdr.split(b"\r\n")[0].decode("latin1", "replace")
        m = re.match(r"(\w+)\s+(\S+)\s+HTTP/", line)
        if not m:
            return
        method, target = m.group(1), m.group(2)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
        else:
            mm = re.match(r"https?://([^/:]+)(?::(\d+))?", target)
            if not mm:
                c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            host, port = mm.group(1), int(mm.group(2) or 80)
        if not allowed(host):
            log("deny.log", f"DENY {method} {host}:{port}")
            msg = b"Blocked by the worker deny list: this host is not a permitted source for agents.\n"
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % len(msg) + msg)
            return
        log("access.log", f"ALLOW {method} {host}:{port}")
        up = socket.create_connection((host, port), timeout=30)
        up.settimeout(None)
        c.settimeout(None)
        if method == "CONNECT":
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:  # clients may pipeline the TLS ClientHello behind the CONNECT request
                up.sendall(rest)
        else:
            up.sendall(head)
        t = threading.Thread(target=pipe, args=(up, c), daemon=True)
        t.start()
        pipe(c, up)
        t.join()
    except Exception as e:  # noqa: BLE001 - a proxy thread must never die noisily
        log("deny.log", f"ERROR {e!r}")
    finally:
        try:
            c.close()
        except OSError:
            pass


def serve(sock):
    while True:
        try:
            c, _ = sock.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(c,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=3128)
    ap.add_argument("--dir", default="")
    args = ap.parse_args()
    STATE["dir"] = args.dir
    os.makedirs(state_dir(), exist_ok=True)
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t.bind(("127.0.0.1", args.port))
    t.listen(128)
    unix = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "allowproxy.sock")
    try:
        os.unlink(unix)
    except OSError:
        pass
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    u.bind(unix)
    os.chmod(unix, 0o600)
    u.listen(128)
    threading.Thread(target=serve, args=(u,), daemon=True).start()
    sys.stderr.write(f"allowproxy on 127.0.0.1:{args.port} and {unix}; logs in {state_dir()}\n")
    serve(t)


if __name__ == "__main__":
    main()
