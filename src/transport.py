"""
Transports — the seam that makes serial and droid-WiFi indistinguishable to the UI.

Every transport is a RAW BYTE PIPE. Open it, write bytes, get bytes back, close it.
Nothing here understands NaviCore's protocol, and that is deliberate: the config tool
already does line assembly, UTF-8 stream decoding, `;w20,` bridge framing, 187-byte
fragmentation and write pacing. Re-implementing any of that down here would fork logic
that exists — and the pieces of it that look redundant are all scar tissue from real
failures (see CLAUDE.md "Rules that are easy to break").

THREE CONTRACTS. Breaking any of them reintroduces a specific, documented bug:

  1. on_data delivers RAW BYTES. Never str, never pre-split lines.
     The page keeps one TextDecoder({stream: True}) alive so a UTF-8 character split
     across two reads reassembles. Decode here and multi-byte characters at chunk
     boundaries become mojibake in the terminal.

  2. write() RAISES on device loss. It must not swallow errors into a queue.
     index.html ~5316: a failing write is the only liveness proof the tool has, because
     a read() on a vanished device can hang forever without ever rejecting. Fire-and-
     forget writes brought back a measured 16.7-hour "Connected but frozen" panel.

  3. open() does NOT assert DTR/RTS.
     This is the bug that justifies the whole project. Chrome asserts both inside
     Web Serial's open() with no way to suppress it, so the droid resets on every page
     refresh ("Reset reason: 11 - USB peripheral"). pyserial can open quietly, and that
     is a fix no browser-side work can achieve.
"""

from __future__ import annotations

import abc
from typing import Callable, Iterable, Optional

# Raw bytes from the far end. Called from a reader thread/task, not the UI thread.
DataCallback = Callable[[bytes], None]
# Called once when the link drops for a reason the transport itself detected.
LostCallback = Callable[[str], None]


class TransportError(Exception):
    """Link failed. Raised by write() on device loss — see contract 2 above."""


class Transport(abc.ABC):
    """One byte pipe. Implementations: SerialTransport, WebSocketTransport."""

    def __init__(self, on_data: DataCallback, on_lost: Optional[LostCallback] = None):
        self._on_data = on_data
        self._on_lost = on_lost

    @abc.abstractmethod
    def open(self) -> None:
        """Attach. MUST NOT assert DTR/RTS (contract 3)."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Send raw bytes. MUST raise TransportError on device loss (contract 2)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Detach. Safe to call when already closed."""

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    def set_signals(self, *, dtr: Optional[bool] = None, rts: Optional[bool] = None) -> None:
        """Drive the control lines.

        Meaningful only on a real serial port — it is how the flasher enters the
        bootloader, and how we avoid resetting the board on open. A no-op elsewhere:
        a WebSocket has no control lines, and the caller should not have to care which
        transport it holds.
        """
        return None


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------

def list_serial_ports() -> list[dict]:
    """Enumerate serial ports.

    Unlike Web Serial's requestPort(), this needs NO user gesture and no permission
    prompt — the app can offer a populated list on launch instead of making the user
    hunt through a browser chooser.
    """
    from serial.tools import list_ports  # imported lazily so --help works without pyserial

    out = []
    for p in list_ports.comports():
        out.append({
            "path": p.device,
            "description": p.description or "",
            "vid": p.vid,
            "pid": p.pid,
            "serial_number": p.serial_number or "",
        })
    return out


# --------------------------------------------------------------------------------------
# Implementations — deliberately unwritten
# --------------------------------------------------------------------------------------
# SerialTransport and WebSocketTransport land next. They are stubbed rather than sketched
# because a half-written transport that silently swallows a write error looks like working
# code and violates contract 2 — the failure mode is a UI that says "Connected" for hours
# against a dead board. Write them against the contracts above, deliberately.
#
# SerialTransport notes when it is written:
#   - serial.Serial(port, 115200, timeout=...) — but set dsrdtr/rtscts so the constructor
#     does not raise DTR. Verify against a real board: the boot log prints its reset reason,
#     so "Reset reason: 11 - USB peripheral" on connect means we are still asserting.
#   - The baud is real here. On the NaviCore side USB-CDC ignores it (native USB, not a
#     UART), but a bridge WCB is UART0 at a genuine 115200.
#   - Reader thread → on_data(raw bytes). No decoding, no line splitting.
