"""SerialTransport — a real OS serial port, opened without resetting the droid.

Implements the contracts in transport.py. Read that docstring first; the three
rules there are each a specific bug, and this file is where two of them are kept.
"""

from __future__ import annotations

import threading
from typing import Optional

import serial  # pyserial

from transport import Transport, TransportError, DataCallback, LostCallback

# The board's USB-CDC ignores this (native USB, not a UART), but a bridge WCB is a
# real UART0 behind a CP210x/CH9102 and 115200 is genuinely its line rate. One
# constant serves both because the fictional case does not care.
DEFAULT_BAUD = 115200

# Block briefly so the reader thread notices close() promptly without spinning.
_READ_TIMEOUT_S = 0.05
# Ceiling per read, not a target -- read() returns whatever has arrived.
_READ_CHUNK = 4096


class SerialTransport(Transport):
    def __init__(self, path: str, on_data: DataCallback,
                 on_lost: Optional[LostCallback] = None, baud: int = DEFAULT_BAUD):
        super().__init__(on_data, on_lost)
        self._path = path
        self._baud = baud
        self._ser: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------
    def open(self) -> None:
        if self._ser is not None:
            return

        # THE WHOLE REASON THIS PROJECT IS NATIVE.
        #
        # A NaviCore v2 drives USB natively (ESP32-S3 USB Serial/JTAG, no bridge
        # chip), and its ROM watches DTR/RTS to decide whether to reset. Chrome
        # asserts both inside Web Serial's open() with no way to suppress it, so the
        # droid reboots every time a page opens the port -- measured, and recorded in
        # config_tool/index.html (~4468) as unfixable from a browser.
        #
        # pyserial CAN open quietly, but only if the lines are set BEFORE the port is
        # opened. Constructing serial.Serial(port, baud) opens immediately and it is
        # already too late. So: build unconfigured, set dtr/rts (stored as the desired
        # state and applied at open), THEN open.
        s = serial.Serial()
        s.port = self._path
        s.baudrate = self._baud
        s.timeout = _READ_TIMEOUT_S
        s.write_timeout = 2.0
        # Not flow control -- these are the control-line states applied on open.
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"cannot open {self._path}: {e}") from e

        self._ser = s
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop,
                                        name=f"serial-rx:{self._path}", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        r, self._reader = self._reader, None
        if r and r is not threading.current_thread():
            r.join(timeout=1.0)
        s, self._ser = self._ser, None
        if s:
            try:
                s.close()
            except Exception:
                pass  # already gone; nothing useful to do or report

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # -- io ----------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        s = self._ser
        if s is None or not s.is_open:
            raise TransportError("port is not open")
        # Serialised because a partial interleave corrupts a line. The page's own
        # sendLine() already chains its writes for the same reason; this is the
        # equivalent guarantee one layer down.
        with self._write_lock:
            try:
                s.write(data)
            except (serial.SerialException, serial.SerialTimeoutException, OSError) as e:
                # CONTRACT 2: propagate. A failing write is the only liveness proof the
                # UI has -- a read on a vanished device can block forever without ever
                # erroring. Swallowing this reinstates a measured 16.7-hour "Connected
                # but frozen" panel.
                raise TransportError(f"write failed on {self._path}: {e}") from e

    def set_signals(self, *, dtr: Optional[bool] = None, rts: Optional[bool] = None) -> None:
        """Drive DTR/RTS -- bootloader entry, and the reset we avoid on open."""
        s = self._ser
        if s is None or not s.is_open:
            raise TransportError("port is not open")
        try:
            if dtr is not None:
                s.dtr = dtr
            if rts is not None:
                s.rts = rts
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"set_signals failed on {self._path}: {e}") from e

    # -- reader ------------------------------------------------------------------
    def _read_loop(self) -> None:
        s = self._ser
        while not self._stop.is_set() and s is not None and s.is_open:
            try:
                # in_waiting then read() -- read(1) would wake per byte, and
                # read(_READ_CHUNK) would sit out the timeout waiting to fill.
                n = s.in_waiting or 1
                chunk = s.read(min(n, _READ_CHUNK))
            except (serial.SerialException, OSError) as e:
                if not self._stop.is_set():
                    # Close BEFORE reporting -- see the note in ws_transport. A
                    # reader that merely returns leaves is_open lying, and the port
                    # held, if the loss races attach()'s publish.
                    self.close()
                    self._fire_lost(f"read failed: {e}")
                return
            if not chunk:
                continue          # idle timeout, not an error
            try:
                # CONTRACT 1: raw bytes up. No decode, no line splitting -- the page
                # keeps one streaming UTF-8 decoder alive so a multi-byte character
                # split across two reads reassembles. Decoding here makes mojibake at
                # every chunk boundary.
                self._on_data(chunk)
            except Exception:
                # A consumer bug must not kill the reader and take the link down.
                pass

    def _fire_lost(self, why: str) -> None:
        cb = self._on_lost
        if cb:
            try:
                cb(why)
            except Exception:
                pass
