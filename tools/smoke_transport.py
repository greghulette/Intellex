#!/usr/bin/env python3
"""Exercise a Transport implementation against real hardware.

    python tools/smoke_transport.py ws   --host 192.168.4.1
    python tools/smoke_transport.py list
    python tools/smoke_transport.py serial --port COM5

Both transports are driven through the SAME code below. That is the actual claim
being tested: the layer above cannot tell them apart. If this file needs an
`if serial:` anywhere past construction, the abstraction has failed.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("tools", 1)[0] + "src")

from transport import Transport, TransportError, list_serial_ports   # noqa: E402


class LineAssembler:
    """Bytes in, complete lines out — the consumer side of contract 1.

    Deliberately mirrors what the config tool does in the browser: ONE incremental
    UTF-8 decoder kept alive across chunks, so a multi-byte character split by a
    chunk boundary reassembles instead of becoming mojibake. Both transports feed
    this identically, which is the point.
    """

    # A board with a live monitor emits PWM_UPDATE ~20x/sec, which buries every
    # other line. Counted but not printed unless asked for -- the interesting
    # traffic is everything else.
    NOISY = ("PWM_UPDATE",)

    def __init__(self, show_all: bool = False):
        import codecs
        self._dec = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""
        self._show_all = show_all
        self.lines: list[str] = []
        self.suppressed = 0

    def feed(self, chunk: bytes) -> None:
        self._buf += self._dec.decode(chunk)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            self.lines.append(line)
            if not self._show_all and any(n in line for n in self.NOISY):
                self.suppressed += 1
                continue
            print(f"  <<< {line}")


def run(t: Transport, asm: LineAssembler, label: str, send: str, wait: float) -> int:
    print(f"opening     {label}")
    try:
        t.open()
    except TransportError as e:
        print(f"FAILED: {e}")
        return 1
    print("open        yes")

    try:
        print(f"sending     {send}")
        t.write((send + "\n").encode("utf-8"))

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        t.close()
        print("closed")

    if not asm.lines:
        print("\nNo complete lines received.")
        return 1

    extra = f"  ({asm.suppressed} PWM_UPDATE hidden — use --all)" if asm.suppressed else ""
    print(f"\n{len(asm.lines)} line(s) received.{extra}")
    if any('"type":"PONG"' in l.replace(" ", "") for l in asm.lines):
        print("PONG seen — transport verified end to end.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["serial", "ws", "list"])
    ap.add_argument("--port", help="COM port for the serial transport")
    ap.add_argument("--host", default="192.168.4.1")
    ap.add_argument("--send", default='{"type":"PING"}')
    ap.add_argument("--wait", type=float, default=3.0)
    ap.add_argument("--all", action="store_true",
                    help="print PWM_UPDATE too (suppressed by default: a live monitor floods it)")
    a = ap.parse_args()

    if a.kind == "list":
        ports = list_serial_ports()
        if not ports:
            print("no serial ports found")
            return 1
        for p in ports:
            print(f"  {p['path']:<8} {p['description']}")
        return 0

    asm = LineAssembler(show_all=a.all)
    lost: list[str] = []

    if a.kind == "serial":
        if not a.port:
            return ap.error("--port is required for the serial transport") or 2
        from serial_transport import SerialTransport
        t: Transport = SerialTransport(a.port, asm.feed, lost.append)
        label = f"serial {a.port}"
    else:
        from ws_transport import WebSocketTransport
        t = WebSocketTransport(a.host, asm.feed, lost.append)
        label = f"ws://{a.host}/ws"

    rc = run(t, asm, label, a.send, a.wait)
    for why in lost:
        print(f"link lost: {why}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
