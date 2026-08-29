#!/usr/bin/env python3
"""Smoke-test the NaviCore WebSocket command endpoint.

    python tools/smoke_ws.py                     # PING against the default AP IP
    python tools/smoke_ws.py --host 192.168.4.1
    python tools/smoke_ws.py --send '{"type":"GET_CONFIG"}' --wait 5

Why this exists rather than "just open it in a browser": ws:// is not a navigable
scheme, so a browser address bar gives ERR_UNKNOWN_URL_SCHEME without ever
contacting the board. A browser can only open one from JavaScript -- and from an
https:// page it will refuse ws:// as mixed content anyway. A native client has
neither restriction, which is the same reason the desktop app is native.

Exit status is meaningful: 0 only if the board answered.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

try:
    from websockets.sync.client import connect
except ImportError:
    sys.exit("websockets not installed -- run: pip install -r requirements.txt")


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test the NaviCore WebSocket endpoint.")
    ap.add_argument("--host", default="192.168.4.1",
                    help="board address (default: the ESP32 SoftAP gateway)")
    ap.add_argument("--path", default="/ws")
    ap.add_argument("--send", default='{"type":"PING"}',
                    help="line to send (default: a PING)")
    ap.add_argument("--wait", type=float, default=3.0,
                    help="seconds to keep reading replies (default: 3)")
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    a = ap.parse_args()

    url = f"ws://{a.host}{a.path}"
    print(f"connecting  {url}")

    try:
        ws = connect(url, open_timeout=a.connect_timeout)
    except Exception as e:
        print(f"\nFAILED to connect: {type(e).__name__}: {e}")
        print("\nCheck, in order:")
        print(f"  1. This machine is joined to the droid's WiFi (is {a.host} its gateway?)")
        print("  2. The boot log shows: [WS] command endpoint ready")
        print("     No [WS] line at all => wifiEnabled was false at boot. It is read once")
        print("     in setup(), so a Save without a reboot changes nothing.")
        print("  3. [WIFI] REFUSED in the boot log => the AP never started (short password).")
        return 1

    replies: list[str] = []
    with ws:
        print(f"connected\nsending     {a.send}")
        # Newline-terminated: the firmware frames on newlines, so a bare message is
        # buffered rather than dispatched. Add one if the caller did not.
        ws.send(a.send if a.send.endswith("\n") else a.send + "\n")

        # The board replies from loop() on Core 1, not from the request handler, so
        # the answer arrives a beat after the send rather than synchronously. Keep
        # reading until the window closes -- one command can produce many frames
        # (the sink flushes in ~1400 B chunks, so a CONFIG arrives as several).
        deadline = time.monotonic() + a.wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = ws.recv(timeout=remaining)
            except TimeoutError:
                break
            except Exception as e:
                print(f"\nrecv error: {type(e).__name__}: {e}")
                break
            replies.append(msg)
            print(f"\n<<< {msg.strip()}")

    if not replies:
        print("\nConnected, but the board sent nothing back.")
        print("The socket opening proves the AP and the HTTP server are up, so the")
        print("gap is between the queue and the reply: check the board's serial log")
        print("for output that should have been tee'd here instead.")
        return 1

    joined = "".join(replies)
    print(f"\n--- {len(replies)} frame(s), {len(joined)} bytes ---")

    # A PONG is proof the whole path works: frame -> Core-0 handler -> queue ->
    # Core-1 drain -> processInputLine -> capture sink -> async frame back.
    for line in joined.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "PONG":
            print(f"PONG -- endpoint verified end to end. Firmware {obj.get('version')}")
            return 0

    print("Got a reply, but no PONG in it (fine if you sent something else).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
