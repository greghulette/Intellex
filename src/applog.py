"""Write everything the app says to a file, because nothing else can see it.

WHY THIS EXISTS
The host is chatty and useful -- it prints every attach, drop, reconnect, probe
result, flash line and cross-origin refusal. Run from a terminal you can read all
of it. But the app is WINDOWED: NaviLink.bat launches with pythonw and the frozen
build is built console=False, so stdout and stderr are attached to nothing at all.
Every one of those lines was being written into a void, and the first question
about any misbehaviour -- "what did it actually do?" -- had no answer.

So tee it. The console still gets everything when there is one; a file always does.

WHERE
<user data dir>/logs/, beside the tool bundles and the firmware cache, for the same
reason they live there: the frozen app's own directory is a temp dir that is
deleted on exit, so a log written beside the executable would be gone precisely
when someone went looking for it.

ONE FILE PER RUN, because the interesting question is almost always "what happened
that time", and a single appended file makes you hunt for the session boundary.
Old ones are pruned so this cannot grow without limit on a machine that launches
the app several times a day.
"""
from __future__ import annotations

import datetime
import sys
from typing import Optional, TextIO

import paths

KEEP_RUNS = 20          # a few days of ordinary use; enough to look back
_log_file: Optional[TextIO] = None
_log_path = None


class _Tee:
    """Write to the original stream AND the log. Never let the log break the app.

    A failed write here must not take down whatever was being reported -- the
    logging is there to explain a problem, not to become one.
    """

    def __init__(self, stream: Optional[TextIO], sink: TextIO):
        self._stream = stream
        self._sink = sink

    def write(self, text: str) -> int:
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass
        try:
            self._sink.write(text)
            # Unbuffered by intent: the lines that matter most are the ones written
            # immediately before a hang or a crash, which is exactly when a buffer
            # is never flushed. A local file write is cheap enough not to care.
            self._sink.flush()
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        for s in (self._stream, self._sink):
            if s is not None:
                try:
                    s.flush()
                except Exception:
                    pass

    # pywebview and some libraries probe these; a bare object would raise.
    def isatty(self) -> bool:
        try:
            return bool(self._stream and self._stream.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._stream is None:
            raise OSError("no fileno")
        return self._stream.fileno()


def path():
    """Where this run is being logged, or None if logging could not start."""
    return _log_path


def _prune(d) -> None:
    try:
        runs = sorted(d.glob("navilink-*.log"))
        for old in runs[:-KEEP_RUNS]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def start() -> Optional[str]:
    """Begin teeing stdout/stderr to a new log file. Returns its path, or None.

    Safe to call twice; the second call does nothing.
    """
    global _log_file, _log_path
    if _log_file is not None:
        return str(_log_path)
    try:
        d = paths.user_data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        _prune(d)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        _log_path = d / f"navilink-{stamp}.log"
        _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        _log_file = None
        _log_path = None
        return None

    _log_file.write(f"NaviLink log - {datetime.datetime.now().isoformat(timespec='seconds')}\n")
    _log_file.write(f"  python   {sys.version.split()[0]}\n")
    _log_file.write(f"  frozen   {paths.FROZEN}\n")
    _log_file.write(f"  bundle   {paths.BUNDLE_DIR}\n")
    _log_file.write(f"  data     {paths.user_data_dir()}\n")
    _log_file.write(f"  argv     {' '.join(sys.argv[1:]) or '(none)'}\n\n")
    _log_file.flush()

    sys.stdout = _Tee(sys.stdout, _log_file)
    sys.stderr = _Tee(sys.stderr, _log_file)
    return str(_log_path)
