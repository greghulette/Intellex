"""WebSocketTransport — the same byte pipe, over the droid's SoftAP.

Implements the contracts in transport.py. The point of this class is that the
layer above cannot tell it apart from SerialTransport: identical interface,
identical raw-bytes-up behaviour, identical raise-on-loss. Whether the droid is
on the end of a cable or its own access point becomes a detail the UI never sees.

One asymmetry is real and is handled here rather than pushed upward: the board
speaks newline-delimited lines, but a WebSocket is MESSAGE framed, not stream
framed. The firmware's reply sink flushes in ~1400-byte chunks, so one command's
output arrives as several frames that do not align to line boundaries. Since the
consumer wants a byte stream (contract 1), frames are simply concatenated in
arrival order and handed up as-is -- the same reassembly the serial path gets for
free from the wire.
"""

from __future__ import annotations

import threading
from typing import Optional

from transport import Transport, TransportError, DataCallback, LostCallback

DEFAULT_PATH = "/ws"
DEFAULT_PORT = 80
# The board answers from loop() on Core 1, a beat after the frame lands, so a recv
# timeout is normal idling rather than a fault.
_RECV_TIMEOUT_S = 0.25


class WebSocketTransport(Transport):
    def __init__(self, host: str, on_data: DataCallback,
                 on_lost: Optional[LostCallback] = None,
                 port: int = DEFAULT_PORT, path: str = DEFAULT_PATH,
                 connect_timeout: float = 5.0):
        super().__init__(on_data, on_lost)
        self._url = f"ws://{host}:{port}{path}"
        self._connect_timeout = connect_timeout
        self._ws = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        if self._ws is not None:
            return
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise TransportError("websockets not installed") from e

        try:
            # KEEPALIVE PINGS ARE THE POINT, not politeness.
            #
            # When the droid reboots its AP vanishes, but the TCP socket keeps
            # looking healthy: nothing is sent, nothing errors, and recv() simply
            # blocks. Measured against a real reboot, the board was serving again
            # after 2.6 s while this end did not notice the link had died for 34 s
            # -- so almost all of a 78 s outage was DETECTION, not recovery.
            #
            # WebSocket ping/pong forces the question every few seconds. This is the
            # same lesson the config tool learned on serial, where a failing WRITE
            # became the only liveness proof because a parked read never returns.
            # BUT NOT ON A 3 SECOND FUSE. The droid is a single ESP32 servicing
            # SBUS at ~111 fps, an ESP-NOW mesh and this socket; it genuinely
            # stalls for seconds at a time. A plain ICMP ping to it over ~220k
            # packets measured a MAXIMUM round trip of 3.8 s against a 6 ms
            # average, so a 3 s pong deadline was killing a perfectly healthy
            # link roughly every 90 s -- "keepalive ping timeout", nothing wrong
            # with the droid at all.
            #
            # 5 s between pings, 10 s to answer: still detects a vanished AP in
            # ~15 s worst case rather than the ~34 s a dead socket takes to
            # notice on its own, and the reconnect loop's idle-gated TCP probe
            # covers the fast case anyway.
            self._ws = connect(self._url,
                               open_timeout=self._connect_timeout,
                               ping_interval=5,    # ask...
                               ping_timeout=10)    # ...but allow for a real stall
        except Exception as e:
            raise TransportError(f"cannot connect {self._url}: {type(e).__name__}: {e}") from e

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop,
                                        name=f"ws-rx:{self._url}", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        ws, self._ws = self._ws, None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        r, self._reader = self._reader, None
        if r and r is not threading.current_thread():
            r.join(timeout=1.0)

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    # -- io ----------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise TransportError("socket is not open")
        # The endpoint registers a TEXT handler, so send text. The payload is UTF-8
        # JSON or an ASCII CLI line either way; surrogates would be a caller bug and
        # should surface, not be silently replaced.
        try:
            payload = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise TransportError(f"payload is not valid UTF-8: {e}") from e

        with self._send_lock:
            try:
                ws.send(payload)
            except Exception as e:
                # CONTRACT 2, same as serial: a failed send is how the layer above
                # learns the link died. Never swallow it.
                raise TransportError(f"send failed: {type(e).__name__}: {e}") from e

    # set_signals is inherited as a no-op: a WebSocket has no control lines, and the
    # caller should not have to know which transport it is holding.

    # -- reader ------------------------------------------------------------------
    def _read_loop(self) -> None:
        ws = self._ws
        while not self._stop.is_set() and ws is not None:
            try:
                msg = ws.recv(timeout=_RECV_TIMEOUT_S)
            except TimeoutError:
                continue                      # idle, not a fault
            except Exception as e:
                if not self._stop.is_set():
                    # Close BEFORE reporting. is_open is "did anyone call close()",
                    # so a reader that just returns leaves the transport looking open
                    # forever -- and if the loss lands in the window between open()
                    # and attach()'s publish, nothing else ever closes it either.
                    # close() is safe from this thread: it skips joining itself.
                    self.close()
                    self._fire_lost(f"recv failed: {type(e).__name__}: {e}")
                return
            if msg is None:
                continue
            # CONTRACT 1: raw bytes up. The firmware flushes mid-line at its chunk
            # boundary, so this deliberately does NOT try to be line-aware -- the
            # consumer's streaming decoder and line assembler handle both transports
            # identically, which is the entire point.
            chunk = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
            if not chunk:
                continue
            try:
                self._on_data(chunk)
            except Exception:
                pass                          # a consumer bug must not kill the link

    def _fire_lost(self, why: str) -> None:
        cb = self._on_lost
        if cb:
            try:
                cb(why)
            except Exception:
                pass
