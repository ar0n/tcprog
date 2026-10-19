# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Firmware images (bincopy-backed): loading, flat binaries, hexdumps."""
import os
import sys

import bincopy

try:
    from elftools.common.exceptions import ELFError   # bincopy reads ELF through pyelftools
    _PARSE_ERRORS = (bincopy.Error, ValueError, ELFError)
except ImportError:
    _PARSE_ERRORS = (bincopy.Error, ValueError)


class FirmwareSegment:
    __slots__ = ("addr", "data")

    def __init__(self, addr, data):
        self.addr = addr
        self.data = bytes(data)

    @property
    def end(self):
        return self.addr + len(self.data)

    def __repr__(self):
        return "FirmwareSegment(0x%08X, %d bytes)" % (self.addr, len(self.data))


class FirmwareImage:
    __slots__ = ("segments",)

    def __init__(self, segments):
        self.segments = segments

    @property
    def total_bytes(self):
        return sum(len(segment.data) for segment in self.segments)

    @property
    def span(self):
        if not self.segments:
            return (0, 0)
        return (self.segments[0].addr, self.segments[-1].end)


def load_image(path, fmt=None, address=None):
    """Load a firmware image -> FirmwareImage. bincopy decodes the text formats (SREC /
    Intel HEX / TI-TXT) by content; only the two it cannot - raw .bin (which
    needs an address) and ELF (binary) - are dispatched by the --format override
    or the extension. An extensionless ELF needs an explicit --format elf."""
    if fmt is None:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".bin":
            fmt = "bin"
        elif ext == ".elf":
            fmt = "elf"
    if fmt == "bin" and address is None:
        raise ValueError("raw .bin image '%s' needs a load address; pass -a/--address" % path)
    binfile = bincopy.BinFile()
    try:
        if fmt == "bin":
            binfile.add_binary_file(path, address=address)
        elif fmt == "elf":
            binfile.add_elf_file(path)
        else:
            binfile.add_file(path)        # auto-detect SREC / Intel HEX / TI-TXT
    except OSError as error:
        raise ValueError("cannot read image '%s'; %s"
                         % (path, (error.strerror or str(error)).lower()))
    except _PARSE_ERRORS as error:
        # bincopy raises its own errors for bad records, ValueError (e.g. a
        # UnicodeDecodeError) for a binary file read as text, and pyelftools'
        # ELFError for a file that is not ELF.
        reason = str(error).rstrip(".")
        if reason[:1].isupper() and not reason[1:2].isupper():
            reason = reason[0].lower() + reason[1:]
        raise ValueError("bad image '%s'; %s" % (path, reason))
    segments = [FirmwareSegment(segment.address, segment.data) for segment in binfile.segments]
    return FirmwareImage(segments)


def _out_format(path):
    """Infer an output format from the file extension (default bin)."""
    if not path:
        return "bin"
    ext = os.path.splitext(path)[1].lower()
    if ext in (".hex", ".ihex", ".ihx"):
        return "hex"
    # SREC has no single extension: .srec/.mot/.sre or .s<digits> (.s19/.s37/...)
    if ext in (".srec", ".mot", ".sre") or (ext[:2] == ".s" and ext[2:].isdigit()):
        return "srec"
    return "bin"


def flat_binary(segments, start, end):
    """[(addr, bytes)] as one flat image of [start, end), the bytes no segment
    covers set to 0x00. -> (bytes, [(lo, hi)] filled spans)"""
    out = bytearray(end - start)
    gaps, pos = [], start
    for addr, data in sorted(segments):
        out[addr - start:addr - start + len(data)] = data
        if addr > pos:
            gaps.append((pos, addr))
        pos = max(pos, addr + len(data))
    if pos < end:
        gaps.append((pos, end))
    return bytes(out), gaps


def _hexdump(data, base):
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexs = " ".join("%02x" % b for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        sys.stdout.write("%08x  %-47s  %s\n" % (base + off, hexs, text))
