#!/usr/bin/env python3
"""Prove the host feeds TWO pages at once, without needing a droid.

    python tools/smoke_fanout.py

WHY THIS EXISTS. Serving the WCB Wizard alongside the NaviCore config tool means
two pages hold /_link at the same time -- as two tabs, as the shell's two iframes,
or as two app windows. Before that, Bridge kept exactly ONE page socket and a
second page silently stole the pipe from the first: the robbed tool went deaf with
no error anywhere, showing "Connected" while receiving nothing.

That failure is invisible in every other check. It needs no hardware to reproduce
and no hardware to test, so there is no excuse for not testing it -- a fake
transport is enough, and it exercises the real Bridge, the real /_link handler and
real WebSockets over a real aiohttp server.

Covered here:
  1. both pages receive every chunk the transport produces
  2. either page can write, and the transport gets it
  3. one page leaving does not deafen the other  <-- the specific regression
  4. a chunk is delivered WHOLE to each page, never split between them
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import aiohttp                                             # noqa: E402
from aiohttp import web                                    # noqa: E402

import host as hostmod                                     # noqa: E402
from transport import Transport                            # noqa: E402

PORT = 8791


class FakeTransport(Transport):
    """A pipe with nothing on the far end. Records writes, injects reads."""

    def __init__(self, on_data, on_lost=None):
        super().__init__(on_data, on_lost)
        self._open = False
        self.written: list[bytes] = []

    def open(self) -> None:
        self._open = True

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def feed(self, data: bytes) -> None:
        """Stand in for the reader thread delivering bytes from a device."""
        self._on_data(data)


def check(ok: bool, what: str) -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + what)
    return ok


async def main() -> int:
    bridge = hostmod.bridge
    fake = FakeTransport(bridge._on_transport_data, bridge._on_transport_lost)
    bridge.attach(fake, "fake", {"kind": "serial", "port": "FAKE"})

    app = hostmod.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, hostmod.BIND_HOST, PORT)
    await site.start()
    base = f"http://{hostmod.BIND_HOST}:{PORT}"
    ok = True

    try:
        async with aiohttp.ClientSession() as s:
            a = await s.ws_connect(f"{base}/_link")
            b = await s.ws_connect(f"{base}/_link")
            # bind_page runs on the server's handler task, which has not necessarily
            # been scheduled by the time ws_connect returns to us.
            for _ in range(50):
                if len(bridge._pages) == 2:
                    break
                await asyncio.sleep(0.02)
            ok &= check(len(bridge._pages) == 2,
                        f"both pages are bound (bridge holds {len(bridge._pages)})")

            # 1 + 4. One chunk, delivered whole, to both.
            chunk = b"PONG {\"v\":\"6.2.1\"}\n"
            fake.feed(chunk)
            ra = await asyncio.wait_for(a.receive(), 3)
            rb = await asyncio.wait_for(b.receive(), 3)
            ok &= check(ra.data == chunk, "page A got the chunk, whole")
            ok &= check(rb.data == chunk, "page B got the same chunk, whole")

            # 2. Either page can write.
            await a.send_bytes(b";w20,S1\n")
            await b.send_bytes(b"?version\n")
            for _ in range(50):
                if len(fake.written) == 2:
                    break
                await asyncio.sleep(0.02)
            ok &= check(fake.written == [b";w20,S1\n", b"?version\n"],
                        f"both pages' writes reached the transport ({fake.written})")

            # 5. ORDER, under a burst. The pipe is a byte stream and each page
            # keeps its own streaming TextDecoder, so a chunk delivered out of
            # order desynchronises that decoder and corrupts a multi-byte
            # character -- silently, and only for the page that got them swapped.
            #
            # This is what the single _pump task exists to guarantee. The previous
            # task-per-chunk fan-out could reorder for page B whenever page A's
            # send for chunk N parked on backpressure while its send for N+1
            # failed fast, so "usually in order" was the strongest claim available.
            N = 200
            for i in range(N):
                fake.feed(f"L{i}\n".encode())
            got_a, got_b = [], []
            for who, sock in ((got_a, a), (got_b, b)):
                buf = b""
                while buf.count(b"\n") < N:
                    m = await asyncio.wait_for(sock.receive(), 5)
                    buf += m.data if isinstance(m.data, bytes) else m.data.encode()
                who.extend(buf.decode().split("\n")[:N])
            want = [f"L{i}" for i in range(N)]
            ok &= check(got_a == want, f"page A got all {N} chunks in order")
            ok &= check(got_b == want, f"page B got all {N} chunks in order")

            # 3. THE REGRESSION. Close A; B must still hear everything.
            await a.close()
            for _ in range(50):
                if len(bridge._pages) == 1:
                    break
                await asyncio.sleep(0.02)
            ok &= check(len(bridge._pages) == 1,
                        f"closing A unbound only A (bridge holds {len(bridge._pages)})")

            after = b"still here\n"
            fake.feed(after)
            rb2 = await asyncio.wait_for(b.receive(), 3)
            ok &= check(rb2.data == after, "page B still receives after A left")

            await b.close()
    finally:
        await runner.cleanup()
        bridge.detach()

    print("\nfan-out: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
