# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Flash Standard Interface (FSI) programmer and UCB reads.

Command sequences are Infineon's own, transcribed verbatim from the iLLD
IfxFlash.h (Flash/Std) so tcprog drives the flash exactly as the on-chip driver
does, only over DAP WRITEMEM instead of from the CPU. Command interface base
0xAF000000; program addresses use the cached 0xA0000000 PFLASH alias.
"""
import os
import sys

from .dap import Dap, DapError
from .image import parse_bmhd, validate_bmhd
from .util import EX_VERIFY, Fail, _error, log

FSI_BASE = 0xAF000000
DMU_HF_STATUS = 0xF8040010          # busy bits: D0=bit0, P0=bit2, P1=bit3
PFLASH_PAGE, DFLASH_PAGE = 32, 8    # IFXFLASH_{PFLASH,DFLASH}_PAGE_LENGTH
PFLASH_LOG_SECTOR = 0x4000          # 16 KiB logical sector (192 x 2 over 6 MB)

# UCB (User Configuration Block) layout. Block N sits at <ucb_base> + N*stride;
# its confirmation word is at block + 0x1F0. Block indices are the DMU
# Disable-Protection selectors (NOTES-fsi): BMHD0 0x00, PFLASH 0x10, DFLASH 0x11,
# DBG 0x12. TC3xx stride is 512 B; TC2xx is 1 KiB.
UCB_CONFIRM_OFF = 0x1F0
UCB_BLOCKS = (("bmhd0", 0x00), ("pflash", 0x10), ("dflash", 0x11), ("dbg", 0x12))
_CONFIRM = {0x43211234: "UNCONFIRMED", 0x57B5327F: "CONFIRMED", 0x00000000: "ERASED"}


def _ucb_stride(family):
    return 0x400 if family == "tc2xx" else 0x200


def _prog_alias(addr):
    """Map an uncached PFLASH address (0x80xxxxxx) to the 0xA0xxxxxx alias the
    FSI enter-page-mode check requires; DFLASH (0xAFxxxxxx) is left as-is."""
    if (addr & 0xFF000000) == 0x80000000:
        return (addr & 0x00FFFFFF) | 0xA0000000
    return addr


def _busy_mask(addr):
    """HF_STATUS busy bits for the flash the address lives in: PFLASH P0|P1, DFLASH D0."""
    return 0x01 if (addr & 0xFF000000) == 0xAF000000 else (1 << 2) | (1 << 3)


def _is_pflash(addr):
    return (addr & 0xFF000000) in (0x80000000, 0xA0000000)


def _pflash_sectors(addr, length):
    """(first_sector_addr, count) of the 16 KiB PFLASH logical sectors that cover
    [addr, addr+length)."""
    first = addr & ~(PFLASH_LOG_SECTOR - 1)
    last = (addr + length - 1) & ~(PFLASH_LOG_SECTOR - 1)
    return first, (last - first) // PFLASH_LOG_SECTOR + 1


class Flash:
    """The iLLD FSI command sequences over a Dap. Writes to flash; destructive."""
    def __init__(self, dap):
        self.dap = dap

    def _cmd(self, offset, value):
        self.dap.write_word(FSI_BASE | offset, value)

    def clear_status(self):
        self._cmd(0x5554, 0xFA)

    def reset_to_read(self):
        self._cmd(0x5554, 0xF0)

    def wait_unbusy(self, mask, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while True:
            status = self.dap.try_read_word(DMU_HF_STATUS)
            if status is None:
                raise DapError("HF_STATUS unreadable during flash op")
            if not (status & mask):
                return
            if time.monotonic() > deadline:
                raise DapError("flash busy past %.1fs (HF_STATUS=0x%08X)" % (timeout, status))

    def enter_page_mode(self, page_addr):
        page_addr = _prog_alias(page_addr)
        if (page_addr & 0xFF000000) == 0xA0000000:
            self._cmd(0x5554, 0x50)                 # program flash
        elif (page_addr & 0xFF000000) == 0xAF000000:
            self._cmd(0x5554, 0x5D)                 # data flash
        else:
            raise DapError("0x%08X is not a programmable flash page" % page_addr)

    def load_page(self, word_lo, word_hi):
        self._cmd(0x55F0, word_lo)
        self._cmd(0x55F4, word_hi)

    def write_page(self, page_addr):
        page_addr = _prog_alias(page_addr)
        self._cmd(0xAA50, page_addr)
        self._cmd(0xAA58, 0x00)
        self._cmd(0xAAA8, 0xA0)
        self._cmd(0xAAA8, 0xAA)

    def erase_sectors(self, sector_addr, count):
        self._cmd(0xAA50, _prog_alias(sector_addr))
        self._cmd(0xAA58, count)
        self._cmd(0xAAA8, 0x80)
        self._cmd(0xAAA8, 0x50)

    def program_page(self, page_addr, page_bytes, busy_mask):
        """Program one aligned page (32 B PFLASH / 8 B DFLASH), padded with 0xFF."""
        page_len = len(page_bytes)
        self.enter_page_mode(page_addr)
        for off in range(0, page_len, 8):
            chunk = page_bytes[off:off + 8].ljust(8, b"\xFF")
            self.load_page(int.from_bytes(chunk[0:4], "little"),
                           int.from_bytes(chunk[4:8], "little"))
        self.write_page(page_addr)
        self.wait_unbusy(busy_mask)


def _read_region(dap, addr, length):
    """Block-read [addr, addr+length) and slice out the exact bytes."""
    start = addr & ~(Dap.BLOCK - 1)
    end = (addr + length + Dap.BLOCK - 1) & ~(Dap.BLOCK - 1)
    data = dap.read_blocks(list(range(start, end, Dap.BLOCK)))
    return data[addr - start: addr - start + length]


def _program(dap, image, args):
    """Erase (optional) + program + verify every segment. Raises DapError on a
    transport/flash fault; returns an exit code."""
    flash = Flash(dap)
    flash.clear_status()

    if args.erase == "sectors":
        for segment in image.segments:
            if not _is_pflash(segment.addr):
                return _error("--erase sectors only supports PFLASH; use --erase none "
                             "for 0x%08X" % segment.addr, os.EX_USAGE)
            first, count = _pflash_sectors(segment.addr, len(segment.data))
            log("tcprog: erase %d sector(s) from 0x%08X" % (count, first), verbose=True)
            flash.erase_sectors(first, count)
            flash.wait_unbusy(_busy_mask(segment.addr))

    for segment in image.segments:
        page = PFLASH_PAGE if _busy_mask(segment.addr) != 0x01 else DFLASH_PAGE
        mask = _busy_mask(segment.addr)
        data, base = segment.data, segment.addr & ~(page - 1)
        pad_front = segment.addr - base
        buf = b"\xFF" * pad_front + data
        if len(buf) % page:
            buf += b"\xFF" * (page - len(buf) % page)
        for off in range(0, len(buf), page):
            flash.program_page(base + off, buf[off:off + page], mask)
        if not args.quiet:
            sys.stderr.write("\r  programmed %d/%d bytes" % (segment.end - segment.addr,
                                                             image.total_bytes))
    if not args.quiet:
        sys.stderr.write("\n")
    flash.reset_to_read()

    if not args.no_verify:
        for segment in image.segments:
            back = _read_region(dap, segment.addr, len(segment.data))
            if back != segment.data:
                bad = next(i for i in range(len(back)) if back[i] != segment.data[i])
                return _error("verify mismatch at 0x%08X (wrote 0x%02X, read 0x%02X)"
                             % (segment.addr + bad, segment.data[bad], back[bad]), EX_VERIFY)
        log("tcprog: verified %d bytes" % image.total_bytes, verbose=True)

    if not args.quiet:
        log("tcprog: wrote %d bytes, verified." % image.total_bytes
            if not args.no_verify else
            "tcprog: wrote %d bytes (not verified)." % image.total_bytes)
    return os.EX_OK


def _read_ucbs(dap, chip):
    """Read every known UCB's confirmation word (+ BMHD0 header). -> list of
    dicts {name, addr, confirm (int|None), state, bmhd (dict|None)}."""
    ucb = chip.first("ucb") if chip else None
    if ucb is None:
        raise Fail("no UCB region known for this target; pass -c/--chip or --device",
                   os.EX_USAGE)
    stride = _ucb_stride(chip.family)
    out = []
    for name, index in UCB_BLOCKS:
        block = ucb.base + index * stride
        confirm = dap.try_read_word(block + UCB_CONFIRM_OFF)
        # A confirmed UCB is read-protected, so its confirm word reads back as
        # unreadable (None) rather than a code.
        state = "CONFIRMED (read-protected)" if confirm is None \
            else _CONFIRM.get(confirm, "0x%08X (unknown)" % confirm)
        entry = {"name": name, "addr": block, "confirm": confirm, "state": state,
                 "bmhd": None}
        if name.startswith("bmhd"):
            header = b"".join((dap.try_read_word(block + off) or 0).to_bytes(4, "little")
                              for off in range(0, 16, 4))
            entry["bmhd"] = parse_bmhd(header)
            entry["bmhd_ok"], entry["bmhd_problems"] = validate_bmhd(header, chip)
        out.append(entry)
    return out
