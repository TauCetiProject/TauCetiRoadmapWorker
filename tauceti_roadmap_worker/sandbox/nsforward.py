#!/usr/bin/env python3
"""Inside a netjail namespace (run as root of that namespace, before the command drops to the real
uid). Provides the jail's only network services:

  * 127.0.0.1:3128  explicit HTTP proxy -> relayed to the host allow-listing proxy's unix socket
  * 127.0.0.53:53   DNS: every A query answers 127.0.0.1 (AAAA: no answer), so programs that resolve
                    hostnames themselves (the Claude CLI does) end up connecting to loopback
  * 127.0.0.1:443   transparent TLS relay: reads the ClientHello's server name and opens a CONNECT
                    tunnel to that host through the host proxy, then passes the bytes through
  * 127.0.0.1:80    transparent HTTP relay: reads the Host header and does the same for port 80

Everything therefore reaches the internet only through allowproxy.py on the host, which logs it.
netjail starts this file from $NETJAIL_FORWARDER (the worker sets it to the bundled copy).
"""
import os, re, socket, struct, threading, time

DBG = os.path.join(os.environ.get("ALLOWPROXY_DIR") or os.path.expanduser("~/.local/state/tauceti/allowproxy"),
                   "nsforward-debug.log")


def dbg(msg):
    try:
        with open(DBG, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass

UNIX = f"/run/user/{os.environ.get('NETJAIL_HOST_UID') or os.getuid()}/allowproxy.sock"


def host_proxy():
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    u.connect(UNIX)
    return u


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


def tunnel(c, host, port, first=b""):
    """Open CONNECT host:port through the host proxy, forward `first` then relay both ways."""
    try:
        u = host_proxy()
        u.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            d = u.recv(4096)
            if not d:
                raise OSError("proxy closed")
            resp += d
        if not resp.startswith(b"HTTP/1.1 200"):
            raise OSError("proxy refused")
        if first:
            u.sendall(first)
    except OSError as e:
        dbg(f"tunnel {host}:{port} failed {e!r}")
        c.close()
        return
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
    pipe(c, u)


def explicit_proxy(c):
    """Explicit proxy clients: hand the whole conversation to the host proxy unchanged."""
    try:
        u = host_proxy()
    except OSError as e:
        dbg(f"explicit: host_proxy failed {e!r}")
        c.close()
        return
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
    pipe(c, u)


def sni(data):
    """Server name from a TLS ClientHello, or None."""
    try:
        if data[0] != 0x16:
            return None
        p = 5 + 4 + 2 + 32  # record hdr, handshake hdr, version, random
        sid = data[p]; p += 1 + sid
        cs = struct.unpack(">H", data[p:p + 2])[0]; p += 2 + cs
        cm = data[p]; p += 1 + cm
        ext_len = struct.unpack(">H", data[p:p + 2])[0]; p += 2
        end = p + ext_len
        while p + 4 <= end:
            t, l = struct.unpack(">HH", data[p:p + 4]); p += 4
            if t == 0:
                q = p + 2
                while q + 3 <= p + l:
                    nt = data[q]; nl = struct.unpack(">H", data[q + 1:q + 3])[0]; q += 3
                    if nt == 0:
                        return data[q:q + nl].decode("ascii", "ignore")
                    q += nl
            p += l
    except (IndexError, struct.error):
        return None
    return None


def tls_relay(c):
    c.settimeout(15)
    try:
        first = c.recv(16384)
    except OSError:
        c.close(); return
    c.settimeout(None)
    host = sni(first)
    if not host:
        dbg(f"tls: no server name in {len(first)} bytes, head={first[:16].hex()}")
        c.close(); return
    tunnel(c, host, 443, first)


def http_relay(c):
    c.settimeout(15)
    try:
        first = b""
        while b"\r\n\r\n" not in first and len(first) < 65536:
            d = c.recv(16384)
            if not d:
                break
            first += d
    except OSError:
        c.close(); return
    c.settimeout(None)
    m = re.search(rb"\r\nHost:\s*([^\r\n:]+)(?::(\d+))?", first, re.I)
    if not m:
        c.close(); return
    tunnel(c, m.group(1).decode("latin1"), int(m.group(2) or 80), first)


def dns():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.53", 53))
    while True:
        try:
            q, addr = s.recvfrom(4096)
            if len(q) < 12:
                continue
            tid, flags, qd = q[:2], q[2:4], struct.unpack(">H", q[4:6])[0]
            # parse the first question
            p = 12
            while p < len(q) and q[p] != 0:
                p += 1 + q[p]
            p += 1
            qtype = struct.unpack(">H", q[p:p + 2])[0]
            question = q[12:p + 4]
            hdr = tid + b"\x81\x80" + b"\x00\x01" + (b"\x00\x01" if qtype == 1 else b"\x00\x00") + b"\x00\x00\x00\x00"
            ans = b""
            if qtype == 1:  # A
                ans = b"\xc0\x0c" + b"\x00\x01\x00\x01" + b"\x00\x00\x00\x3c" + b"\x00\x04" + bytes([127, 0, 0, 1])
            s.sendto(hdr + question + ans, addr)
        except OSError:
            continue


def serve(addr, handler):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(addr); s.listen(256)
    while True:
        try:
            c, _ = s.accept()
        except OSError:
            continue
        threading.Thread(target=handler, args=(c,), daemon=True).start()


def main():
    threading.Thread(target=dns, daemon=True).start()
    threading.Thread(target=serve, args=(("127.0.0.1", 443), tls_relay), daemon=True).start()
    threading.Thread(target=serve, args=(("127.0.0.1", 80), http_relay), daemon=True).start()
    serve(("127.0.0.1", 3128), explicit_proxy)


if __name__ == "__main__":
    main()
