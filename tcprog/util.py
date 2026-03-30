# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Exit codes, logging, and the Fail exception shared across tcprog."""
import sys

# Exit codes: the standard sysexits.h values via os.EX_* (EX_OK=0, EX_USAGE=64
# bad args, EX_DATAERR=65 bad image, EX_UNAVAILABLE=69 no probe/target/binding,
# EX_CANTCREAT=73 output file, EX_IOERR=74 transfer failed, EX_TEMPFAIL=75
# timeout, EX_NOPERM=77 debug-locked). Only these three have no sysexits
# equivalent (a HIL pipeline keys off them):
EX_CHECK = 80    # ident --check: detected part != expected
EX_VERIFY = 81   # read-back verification mismatch
EX_REFUSED = 82  # a destructive op refused for safety (e.g. UCB write w/o flag)

# --- verbose logging (set once from -v/--verbose in main; higher = more detail) ---
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
    """Print `tcprog: <msg>` to stderr and return the exit code (for `return _error(...)`)."""
    sys.stderr.write("tcprog: %s\n" % msg)
    return code


class Fail(Exception):
    """A fatal condition carrying an exit code. `locked` marks the debug-locked,
    no-password case so ident can print its own report instead of a bare error."""
    def __init__(self, msg, code, locked=False):
        super().__init__(msg)
        self.code = code
        self.locked = locked
