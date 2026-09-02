#!/usr/bin/env python3
"""End-to-end test of the host: page -> /_link -> transport -> droid -> back.

Stands in for the config tool. It does exactly what the page will do -- talk to
one same-origin WebSocket and nothing else -- so if this passes, the page's job
is a small additive branch rather than new machinery.

    python tools/smoke_host.py --attach ws              # via the droid's AP
    python tools/smoke_host.py --attach serial --port COM5

Assumes the host is already running:  python src/host.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp


async def run(base: str, attach: str, port: str | None, host_ip: str,
              send: str, wait: float) -> int:
    async with aiohttp.ClientSession() as s:
        # 1. Control plane: what can we see?
        async with s.get(f"{base}/_api/ports") as r:
            ports = (await r.json())["ports"]
        print(f"host sees {len(ports)} serial port(s)")

        # 2. Attach a transport.
        body = {"kind": "serial", "port": port} if attach == "serial" \
            else {"kind": "ws", "host": host_ip}
        async with s.post(f"{base}/_api/attach", json=body) as r:
            res = await r.json()
        if not res.get("ok"):
            print(f"attach FAILED: {res.get('error')}")
            return 1
        print(f"attached  {res['target']}")

        # 3. The byte pipe -- the ONLY thing the page will use.
        got: list[str] = []
        try:
            async with s.ws_connect(f"{base}/_link") as ws:
                print(f"sending   {send}")
                await ws.send_str(send + "\n")

                # Reassemble exactly as the page does: one streaming decoder, split
                # on newlines. Chunk boundaries are meaningless at this layer.
                import codecs
                dec = codecs.getincrementaldecoder("utf-8")()
                buf = ""
                # asyncio.timeout() is 3.11+, and the launchers build their venv
                # from whatever python3 is first on PATH -- on a stock Mac that is
                # the 3.9 framework build. wait_for is the same fence and works
                # everywhere, so the smoke test still runs on the interpreter the
                # app itself is running on. It raising AttributeError here would
                # break the one check you reach for when nothing else works.
                async def drain():
                    nonlocal buf
                    async for msg in ws:
                        chunk = msg.data if isinstance(msg.data, bytes) \
                            else str(msg.data).encode()
                        buf += dec.decode(chunk)
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line or "PWM_UPDATE" in line:
                                continue
                            got.append(line)
                            print(f"  <<< {line}")

                try:
                    await asyncio.wait_for(drain(), wait)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
        finally:
            async with s.post(f"{base}/_api/detach") as r:
                await r.json()
            print("detached")

    if not got:
        print("\nNothing came back through the bridge.")
        return 1
    for line in got:
        if line.startswith("{"):
            try:
                if json.loads(line).get("type") == "PONG":
                    print("\nPONG through the host — page → bridge → droid → back. Verified.")
                    return 0
            except json.JSONDecodeError:
                pass
    print(f"\n{len(got)} line(s) back, no PONG (fine if you sent something else).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--attach", choices=["serial", "ws"], default="ws")
    ap.add_argument("--port", help="COM port when --attach serial")
    ap.add_argument("--host-ip", default="192.168.4.1")
    ap.add_argument("--send", default='{"type":"PING"}')
    ap.add_argument("--wait", type=float, default=3.0)
    a = ap.parse_args()
    if a.attach == "serial" and not a.port:
        return ap.error("--port is required with --attach serial") or 2
    return asyncio.run(run(a.base, a.attach, a.port, a.host_ip, a.send, a.wait))


if __name__ == "__main__":
    sys.exit(main())
