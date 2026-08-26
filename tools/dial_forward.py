#!/usr/bin/env python3
"""Expose the BUSY Bar's USB port over your tailnet/VPN — for the dial.

The Bar only serves its status WebSocket (dial + button events) on the USB
network interface; the Wi-Fi interface refuses the handshake and the cloud
relay rejects device tokens (firmware 1.1.1). So when app.py runs somewhere
else — busybar-manager on a VPS, the cloud target — it normally has no dial
stream and sits on the next train.

Run this on a computer that can reach the Bar, and point the remote app
at it:

    # USB-cable machine (listens on :8760), falling back to the Bar's
    # Wi-Fi address when the cable is out — Wi-Fi DOES serve the WS, the
    # token just must ride the query string (x-api-token, lowercase);
    # the USB interface ignores it, so always include it in BUSYBAR_WS
    python3 tools/dial_forward.py --target 10.0.4.20:80,<bar-wifi-ip>:80

    # wherever app.py runs
    BUSYBAR_WS=ws://<this machine's tailnet IP>:8760/api/status/ws?x-api-token=<token>

It is a dumb TCP forwarder, so the whole USB API rides along, not just the
WebSocket — anyone who can reach the listen port can drive the Bar exactly
as if they held the USB cable (that interface has no auth). Keep the port
on a tailnet/VPN, never the open internet.

Stdlib only; safe to run under launchd/systemd with KeepAlive/Restart.
"""

import argparse
import socket
import sys
import threading
import time


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # half-close both ways so the peer pump unblocks too
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _connect_upstream(targets):
    """First target that accepts wins — USB when the cable is in, Wi-Fi
    when it's out."""
    err = None
    for host, port in targets:
        try:
            return socket.create_connection((host, port), timeout=1.5), host
        except OSError as e:
            err = e
    raise err


def _handle(client, addr, targets):
    try:
        upstream, chosen = _connect_upstream(targets)
    except OSError as e:
        _log(f"{addr[0]}: Bar unreachable on any of "
             f"{','.join(h for h, _ in targets)} ({e})")
        client.close()
        return
    for s in (client, upstream):
        s.settimeout(None)
        # dial deltas are a few bytes each — don't let Nagle sit on them
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _log(f"{addr[0]} connected -> {chosen}")
    back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    back.start()
    _pump(client, upstream)
    back.join()
    client.close()
    upstream.close()
    _log(f"{addr[0]} disconnected")


def main():
    ap = argparse.ArgumentParser(
        description="TCP-forward the BUSY Bar's USB interface (dial stream)")
    ap.add_argument("--listen", default="0.0.0.0:8760",
                    help="host:port to listen on (default 0.0.0.0:8760)")
    ap.add_argument("--target", default="10.0.4.20:80",
                    help="comma-separated Bar addresses, tried in order per "
                         "connection (default 10.0.4.20:80 — add the Wi-Fi "
                         "ip:80 as a fallback for when the cable is out)")
    args = ap.parse_args()
    lhost, lport = args.listen.rsplit(":", 1)
    targets = []
    for t in args.target.split(","):
        thost, tport = t.strip().rsplit(":", 1)
        targets.append((thost, int(tport)))
    srv = socket.create_server((lhost, int(lport)))
    _log(f"forwarding {args.listen} -> {args.target}")
    while True:
        client, addr = srv.accept()
        threading.Thread(target=_handle, args=(client, addr, targets),
                         daemon=True).start()


if __name__ == "__main__":
    main()
