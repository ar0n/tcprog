# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Exit codes, logging, progress output, and the Fail exception shared across tcprog."""
import sys
import time

# Exit codes: the standard sysexits.h values via os.EX_* (EX_OK=0, EX_USAGE=64
# bad args, EX_DATAERR=65 bad image, EX_UNAVAILABLE=69 no probe/target/binding,
# EX_CANTCREAT=73 output file, EX_IOERR=74 transfer failed, EX_NOPERM=77
# debug-locked or password refused). Only these three have no sysexits
# equivalent (a HIL pipeline keys off them):
EX_CHECK = 80    # ident --check: detected part != expected
EX_VERIFY = 81   # read-back verification mismatch
EX_REFUSED = 82  # a destructive op refused for safety (e.g. UCB write w/o flag)

# --- verbose logging (set once from -v/--verbose in main; on or off) ---
_VERBOSE_MODE = 0


def set_verbose(level):
    global _VERBOSE_MODE
    _VERBOSE_MODE = level


def log(msg, verbose=False):
    """Write a diagnostic to stderr. Always prints, unless `verbose=True`, which
    prints only under -v."""
    if verbose and not _VERBOSE_MODE:
        return
    sys.stderr.write(msg + "\n")


def _error(msg, code):
    """Print `tcprog: error: <msg>` to stderr, or `tcprog: refused: <msg>` for a
    safety refusal (EX_REFUSED), and return the exit code (for `return
    _error(...)`). Messages start lowercase, have no trailing period, and put a
    reason or hint after "; "."""
    sys.stderr.write("tcprog: %s: %s\n" % ("refused" if code == EX_REFUSED else "error", msg))
    return code


def warn(msg):
    """Print `tcprog: warning: <msg>` to stderr: the operation goes on."""
    sys.stderr.write("tcprog: warning: %s\n" % msg)


class Fail(Exception):
    """A fatal condition carrying an exit code. `locked` marks the debug-locked,
    no-password case so ident can print its own report instead of a bare error."""
    def __init__(self, msg, code, locked=False):
        super().__init__(msg)
        self.code = code
        self.locked = locked


class Progress:
    """Progress of a long operation on stderr. On a terminal it is one line
    updated in place; otherwise (CI logs, pipes, files) it prints a plain line at
    most every 10 %, plus the current state when a step takes longer than
    `interval` seconds (default HEARTBEAT; --progress-interval), so logs stay
    short and a slow step does not look hung.
    Shown with --progress; disabled entirely with enabled=False. Use it as a
    context manager so the line is finished on every exit path."""

    HEARTBEAT = 30.0

    def __init__(self, label, total, unit="", enabled=True, interval=None):
        self.label, self.total, self.unit = label, max(total, 1), unit
        self.enabled = enabled
        self.tty = enabled and sys.stderr.isatty()
        self.interval = interval or self.HEARTBEAT
        self._last_time = time.monotonic()
        self._last_step = -1
        self._shown = False

    def update(self, done):
        if not self.enabled:
            return
        pct = min(100, int(done * 100 / self.total))
        text = "tcprog: %s %d/%d%s (%d%%)" % (self.label, done, self.total, self.unit, pct)
        if self.tty:
            sys.stderr.write("\r" + text)
            sys.stderr.flush()
            self._shown = True
            return
        now = time.monotonic()
        if pct // 10 > self._last_step or now - self._last_time >= self.interval:
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
            self._last_step, self._last_time = pct // 10, now

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finish()

    def finish(self):
        """End the in-place line on a terminal (plain lines need nothing)."""
        if self.tty and self._shown:
            sys.stderr.write("\n")
            self._shown = False


def _size(n):
    """A byte count for reports: 256 B, 27 KiB, 1.5 KiB."""
    if n < 1024:
        return "%d B" % n
    return ("%d KiB" % (n // 1024)) if not n % 1024 else ("%.1f KiB" % (n / 1024.0))
