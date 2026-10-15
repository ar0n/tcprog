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
from .image import (parse_bmhd_tc2, parse_bmhd_tc3, parse_bmhd_tc4,
                    validate_bmhd_tc2, validate_bmhd_tc3, validate_bmhd_tc4)
from .util import EX_VERIFY, Fail, _error, log

FSI_BASE = 0xAF000000               # TC3xx command base (back-compat default)

# FSI second-cycle opcodes. SAFETY: Verify-Erased is one nibble from Erase.
# 0x5F = Verify Erased Logical Sector Range (non-destructive, array unchanged).
# 0x50 = Erase Logical Sector Range (DESTRUCTIVE). Never confuse the two.
FSI_ERASE_CYCLE2 = 0x50
FSI_VERIFY_ERASED_CYCLE2 = 0x5F
PFLASH_PAGE, DFLASH_PAGE = 32, 8    # IFXFLASH_{PFLASH,DFLASH}_PAGE_LENGTH
PFLASH_LOG_SECTOR = 0x4000          # 16 KiB logical sector (TC3xx 6 MB / TC4 20 MB)

# TC4 HCI_STATUS completion handshake (IfxFlash_isRequestReceived/Executed).
HCI_REQACK, HCI_REQDONE = 1 << 30, 1 << 31


class FlashProfile:
    """Per-family parameters for the FSI command interface and its DMU status/
    error registers. The command *sequences* are byte-identical across TC3xx and
    TC4 (verbatim from iLLD IfxFlash.h); only the command base, the DMU register
    map, the EVER/PVER bit positions, and the completion-wait style differ. This
    captures exactly those, so one Flash driver covers both families.

    wait: "busy" polls the per-bank busy bits in the status register (TC3xx 2G
    DMU HF_STATUS); "reqdone" waits on the REQACK->REQDONE handshake in TC4's
    HCI_STATUS, the sequence the TC4 iLLD itself uses after a command.
    """
    def __init__(self, *, cmd_base, errsr, errsr_name, ever_bit, pver_bit,
                 reject_mask, clre, clre_clear_all, status, wait,
                 load_lo, load_hi, dflash_hi, dflash_sector):
        self.cmd_base = cmd_base
        self.errsr = errsr
        self.errsr_name = errsr_name
        self.ever = 1 << ever_bit
        self.pver = 1 << pver_bit
        self.reject_mask = reject_mask          # OPER | SQER | PROER (a reject, not an answer)
        self.clre = clre
        self.clre_clear_all = clre_clear_all
        self.status = status
        self.wait = wait
        # Load-page offsets for a 32-bit master (iLLD IfxFlash_loadPage2X32):
        # TC3xx takes lo at 0x55F0 then hi at 0x55F4; TC4 streams both to 0x55F4.
        self.load_lo = load_lo
        self.load_hi = load_hi
        # DFLASH lives at a family-specific base (TC3xx 0xAFxxxxxx, TC4 0xAExxxxxx)
        # with its own logical-sector size (TC3xx 4 KiB, TC4 2 KiB). PFLASH is
        # 0x80/0xA0 and 16 KiB on both; DFLASH page is 8 B on both.
        self.dflash_hi = dflash_hi              # high byte of a DFLASH address
        self.dflash_sector = dflash_sector


# TC3xx 2G DMU (Infra/Sfr IfxDmu_regdef.h): HF_* registers. EVER bit4, PVER
# bit3; OPER/SQER/PROER are bits 0/1/2. Verified on TC375 silicon.
PROFILE_TC3XX = FlashProfile(
    cmd_base=0xAF000000,
    errsr=0xF8040034, errsr_name="HF_ERRSR", ever_bit=4, pver_bit=3,
    reject_mask=(1 << 0) | (1 << 1) | (1 << 2),
    clre=0xF8040038, clre_clear_all=(1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5),
    status=0xF8040010, wait="busy", load_lo=0x55F0, load_hi=0x55F4,
    dflash_hi=0xAF, dflash_sector=0x1000)

# TC4Dx DMU (Infra/Sfr/TC4Dx IfxDmu_reg.h + IfxDmu_bf.h): HCI_* registers.
# Command base 0xF8080000 (IfxFlash_cfg_TC4Dx.h IFXFLASH_CMD_BASE_ADDRESS).
# HCI_ERR @0xF8040010: EVER bit7, PVER bit6, OPER bit16, SQER bit1, PROER bit2.
# HCI_CLRERR @0xF8040014 clears CADER..CEVER. HCI_STATUS @0xF8040004 carries the
# REQACK/REQDONE handshake. Command sequences are identical to TC3xx.
PROFILE_TC4XX = FlashProfile(
    cmd_base=0xF8080000,
    errsr=0xF8040010, errsr_name="HCI_ERR", ever_bit=7, pver_bit=6,
    reject_mask=(1 << 16) | (1 << 1) | (1 << 2),
    clre=0xF8040014,
    clre_clear_all=(1 << 0) | (1 << 1) | (1 << 2) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7),
    status=0xF8040004, wait="reqdone", load_lo=0x55F4, load_hi=0x55F4,
    dflash_hi=0xAE, dflash_sector=0x800)

FLASH_PROFILES = {"tc3xx": PROFILE_TC3XX, "tc4xx": PROFILE_TC4XX}


def profile_for_family(family):
    """The FSI/DMU profile for a device family. TC2xx is not covered (FLASH0 FSR
    layout differs); callers gate on that before asking."""
    try:
        return FLASH_PROFILES[family]
    except KeyError:
        raise DapError("no flash profile for family %r (blank-check covers "
                       "tc3xx and tc4xx)" % family)

# UCB (User Configuration Block) layout. Block N sits at <ucb_base> + N*stride;
# its confirmation word is at block + 0x1F0. Block indices are the DMU
# Disable-Protection selectors (NOTES-fsi): BMHD0 0x00, PFLASH 0x10, DFLASH 0x11,
# DBG 0x12. TC3xx stride is 512 B; TC2xx is 1 KiB.
UCB_CONFIRM_OFF = 0x1F0
UCB_BLOCKS = (("bmhd0", 0x00), ("pflash", 0x10), ("dflash", 0x11), ("dbg", 0x12))
_CONFIRM = {0x43211234: "UNCONFIRMED", 0x57B5327F: "CONFIRMED", 0x00000000: "ERASED"}

# TC4xx: four 2 KiB BMHD blocks (iLLD Lcf .bmhd_N_org, Ifx_Ssw_Bmhd), each with
# the 8-word password at +0x7D0 and the confirmation word (+ a copy) at +0x7F0/8.
TC4_BMHD_BASE, TC4_BMHD_STRIDE, TC4_BMHD_COUNT = 0xAE404800, 0x800, 4
TC4_CONFIRM_OFF, TC4_CONFIRM_COPY_OFF = 0x7F0, 0x7F8


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


def _is_dflash(addr, p):
    """True if addr is in this family's DFLASH window (TC3xx 0xAFxxxxxx, TC4
    0xAExxxxxx)."""
    return (addr >> 24) == p.dflash_hi


def _sector_size(addr, p):
    """Logical-sector size for the flash addr lives in: 16 KiB PFLASH, the
    family's DFLASH sector for DFLASH, or None if addr is neither."""
    if _is_pflash(addr):
        return PFLASH_LOG_SECTOR
    if _is_dflash(addr, p):
        return p.dflash_sector
    return None


def _flash_sectors(addr, length, sector):
    """(first_sector_addr, count) of the logical sectors of size `sector` that
    cover [addr, addr+length)."""
    first = addr & ~(sector - 1)
    last = (addr + length - 1) & ~(sector - 1)
    return first, (last - first) // sector + 1


def _pflash_sectors(addr, length):
    """(first_sector_addr, count) of the 16 KiB PFLASH logical sectors that cover
    [addr, addr+length)."""
    return _flash_sectors(addr, length, PFLASH_LOG_SECTOR)


class Flash:
    """The iLLD FSI command sequences over a Dap. Writes to flash; destructive.

    `family` selects the per-family register profile (command base, DMU status/
    error map, EVER/PVER bits, wait style). Defaults to TC3xx so existing callers
    are unchanged; pass "tc4xx" to drive a TC4 part's HCI command interface."""
    def __init__(self, dap, family="tc3xx"):
        self.dap = dap
        self.p = profile_for_family(family)

    def _cmd(self, offset, value):
        self.dap.write_word(self.p.cmd_base | offset, value)

    def clear_status(self):
        self._cmd(0x5554, 0xFA)

    def reset_to_read(self):
        self._cmd(0x5554, 0xF0)

    def wait_unbusy(self, mask, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while True:
            status = self.dap.try_read_word(self.p.status)
            if status is None:
                raise DapError("flash status register unreadable during flash op")
            if not (status & mask):
                return
            if time.monotonic() > deadline:
                raise DapError("flash busy past %.1fs (status=0x%08X)" % (timeout, status))

    def _wait_cmd_done(self, addr, timeout=5.0):
        """Wait for one FSI command to complete. TC3xx polls the per-bank busy
        bits for the flash the address lives in; TC4 waits for the command
        interface to report REQDONE in HCI_STATUS."""
        if self.p.wait != "reqdone":
            self.wait_unbusy(_busy_mask(addr), timeout)
            return
        import time
        deadline = time.monotonic() + timeout
        while True:
            status = self.dap.try_read_word(self.p.status)
            if status is None:
                raise DapError("HCI_STATUS unreadable during flash op")
            if status & HCI_REQDONE:
                return
            if time.monotonic() > deadline:
                raise DapError("flash cmd not done past %.1fs (HCI_STATUS=0x%08X)"
                               % (timeout, status))

    def enter_page_mode(self, page_addr):
        page_addr = _prog_alias(page_addr)
        if _is_pflash(page_addr):
            self._cmd(0x5554, 0x50)                 # program flash
        elif _is_dflash(page_addr, self.p):
            self._cmd(0x5554, 0x5D)                 # data flash
        else:
            raise DapError("0x%08X is not a programmable flash page" % page_addr)

    def load_page(self, word_lo, word_hi):
        self._cmd(self.p.load_lo, word_lo)
        self._cmd(self.p.load_hi, word_hi)

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
        self._cmd(0xAAA8, FSI_ERASE_CYCLE2)

    def clear_errors(self):
        """Clear the DMU error latches so a following verify reads clean."""
        self.dap.write_word(self.p.clre, self.p.clre_clear_all)

    def verify_erased_sectors(self, sector_addr, count):
        """Blank-check [sector_addr, +count sectors) with the flash state machine
        instead of reading the array. Non-destructive: issues Verify Erased
        Logical Sector Range (0x80, 0x5F), NOT Erase. Returns True if the range
        is fully blank (the error register's EVER stayed clear), False if any
        cell is programmed. Raises DapError on a transport fault or a sequence/
        protection error (which is not a blank/not-blank answer)."""
        p = self.p
        addr = _prog_alias(sector_addr)
        if p.wait == "reqdone":
            # TC4: reset the REQACK/REQDONE handshake before issuing the command.
            self.clear_status()
        self.clear_errors()
        self._cmd(0xAA50, addr)
        self._cmd(0xAA58, count)
        self._cmd(0xAAA8, 0x80)
        self._cmd(0xAAA8, FSI_VERIFY_ERASED_CYCLE2)
        self._wait_cmd_done(addr)
        errsr = self.dap.read_word(p.errsr)
        if errsr & p.reject_mask:              # OPER / SQER / PROER
            self.clear_errors()
            raise DapError("verify-erased rejected (%s=0x%08X)" % (p.errsr_name, errsr))
        blank = not (errsr & p.ever)
        self.clear_errors()
        return blank

    def program_page(self, page_addr, page_bytes):
        """Program one aligned page (32 B PFLASH / 8 B DFLASH), padded with 0xFF."""
        page_len = len(page_bytes)
        self.enter_page_mode(page_addr)
        for off in range(0, page_len, 8):
            chunk = page_bytes[off:off + 8].ljust(8, b"\xFF")
            self.load_page(int.from_bytes(chunk[0:4], "little"),
                           int.from_bytes(chunk[4:8], "little"))
        self.write_page(page_addr)
        self._wait_cmd_done(page_addr)


def _read_region(dap, addr, length):
    """Read exactly [addr, addr+length), never past the end: over-reading the
    erased tail of a partly-programmed block traps on TC3xx. Batch the whole
    blocks that fit, read the ragged ends word by word."""
    end = addr + length
    lo = (addr + Dap.BLOCK - 1) & ~(Dap.BLOCK - 1)   # first block fully inside
    hi = end & ~(Dap.BLOCK - 1)                       # end of the last such block
    if lo >= hi:
        return read_words(dap, addr, length)[:length]
    out = read_words(dap, addr, lo - addr)
    out += dap.read_blocks(list(range(lo, hi, Dap.BLOCK)))
    out += read_words(dap, hi, end - hi)
    return bytes(out)[:length]


def read_region_sparse(dap, addr, length, group=16, on_progress=None):
    """Read [addr, addr+length) tolerating erased/unreadable blocks.

    Erased flash traps with a no-response on 2G/TC4 parts, and after a miss the
    target ignores reads until the connect handshake is re-sent, so every miss
    reconnects. Blocks that answer are collected; blocks that do not are left
    out. Returns (segments, missed_blocks) where segments is a list of
    (block-aligned addr, bytes) runs of contiguous answered blocks, each cropped
    to the requested [addr, addr+length) window.

    on_progress(done_blocks, total_blocks, kept_bytes) is called as it goes.
    """
    block = Dap.BLOCK
    start = addr & ~(block - 1)
    end = (addr + length + block - 1) & ~(block - 1)
    block_addrs = list(range(start, end, block))
    total = len(block_addrs)

    segments = []          # list of [addr, bytearray] contiguous runs
    kept_bytes = 0
    missed = 0

    def keep(a, data):
        nonlocal kept_bytes
        kept_bytes += len(data)
        if segments and segments[-1][0] + len(segments[-1][1]) == a:
            segments[-1][1] += data
        else:
            segments.append([a, bytearray(data)])

    i = 0
    while i < total:
        n = min(group, total - i)
        addrs = block_addrs[i:i + n]
        data = None
        if n > 1:
            try:
                data = dap.read_blocks(addrs)
            except DapError:
                dap.connect()
                data = None
        if data is not None and len(data) == n * block:
            for j, a in enumerate(addrs):
                keep(a, data[j * block:(j + 1) * block])
        else:
            # the group hit a hole: fall back to one block at a time
            for a in addrs:
                try:
                    one = dap.read_block(a)
                except DapError:
                    dap.connect()
                    missed += 1
                    continue
                keep(a, one)
        i += n
        if on_progress:
            on_progress(i, total, kept_bytes)

    lo, hi = addr, addr + length
    cropped = []
    for seg_addr, seg_data in segments:
        a0 = max(seg_addr, lo)
        a1 = min(seg_addr + len(seg_data), hi)
        if a1 > a0:
            cropped.append((a0, bytes(seg_data[a0 - seg_addr:a1 - seg_addr])))
    return cropped, missed


def blank_map(dap, addr, length, sector=PFLASH_LOG_SECTOR, on_progress=None,
              family="tc3xx"):
    """Occupancy map of [addr, addr+length) by PFLASH logical sector, using the
    flash controller's hardware blank-check (no array reads, no ECC traps).

    Returns a list of (sector_addr, blank: bool), one entry per sector. Covers
    TC3xx (2G DMU HF_*) and TC4 (TC4Dx HCI_*) via the family profile; the caller
    must gate the family and keep the target unlocked/flash-booting. Issues only
    Verify-Erased (non-destructive) sequences.
    """
    first = addr & ~(sector - 1)
    last = (addr + length - 1) & ~(sector - 1)
    sectors = list(range(first, last + 1, sector))
    flash = Flash(dap, family)
    flash.clear_status()
    out = []
    for i, sa in enumerate(sectors):
        blank = flash.verify_erased_sectors(sa, 1)
        out.append((sa, blank))
        if on_progress:
            on_progress(i + 1, len(sectors), sa, blank)
    flash.reset_to_read()
    return out


def read_region_blankcheck(dap, addr, length, sector=PFLASH_LOG_SECTOR,
                           on_map=None, on_read=None, family="tc3xx"):
    """Sparse read of [addr, addr+length) guided by the hardware blank-check:
    map occupancy by sector (no array reads), then block-read ONLY the
    programmed sectors. The erased bulk is never touched, so this avoids the
    per-erased-block reconnect that read_region_sparse pays. PFLASH only; covers
    TC3xx and TC4 via the family profile.

    Returns (segments, missed) like read_region_sparse. on_map(done, total) and
    on_read(done_bytes, total_bytes) report the two phases.
    """
    occ = blank_map(dap, addr, length, sector=sector, family=family,
                    on_progress=(lambda d, t, sa, bl: on_map(d, t)) if on_map else None)
    programmed = [sa for sa, blank in occ if not blank]
    # merge contiguous programmed sectors into ranges, each clamped to the window
    ranges = []
    for sa in programmed:
        if ranges and ranges[-1][1] == sa:
            ranges[-1][1] = sa + sector
        else:
            ranges.append([sa, sa + sector])
    total = sum(b - a for a, b in ranges) or 1
    segments, missed, done = [], 0, 0
    for a, b in ranges:
        lo = max(a, addr)
        hi = min(b, addr + length)
        if hi <= lo:
            continue
        segs, miss = read_region_sparse(dap, lo, hi - lo)
        segments.extend(segs)
        missed += miss
        done += hi - lo
        if on_read:
            on_read(done, total)

    # Per-sector fidelity check: a sector the blank-check called programmed but
    # from which nothing read back (e.g. uncorrectable ECC) is reported, not
    # faked. Done per sector so a readable neighbour cannot mask it.
    def _has_data(lo, hi):
        return any(sa < hi and sa + len(d) > lo for sa, d in segments)

    unreadable = []
    for sa, blank in occ:
        if blank:
            continue
        lo, hi = max(sa, addr), min(sa + sector, addr + length)
        if hi > lo and not _has_data(lo, hi):
            if unreadable and unreadable[-1][1] == lo:
                unreadable[-1] = (unreadable[-1][0], hi)
            else:
                unreadable.append((lo, hi))
    return segments, missed, unreadable


def _program(dap, image, args, family="tc3xx"):
    """Erase (optional) + program + verify every segment. Raises DapError on a
    transport/flash fault; returns an exit code."""
    flash = Flash(dap, family)
    flash.clear_status()

    if args.erase == "sectors":
        for segment in image.segments:
            sector = _sector_size(segment.addr, flash.p)
            if sector is None:
                return _error("--erase sectors only supports P/DFLASH; use --erase none "
                             "for 0x%08X" % segment.addr, os.EX_USAGE)
            first, count = _flash_sectors(segment.addr, len(segment.data), sector)
            log("tcprog: erase %d sector(s) from 0x%08X" % (count, first), verbose=True)
            flash.erase_sectors(first, count)
            flash._wait_cmd_done(segment.addr)

    for segment in image.segments:
        page = DFLASH_PAGE if _is_dflash(segment.addr, flash.p) else PFLASH_PAGE
        data, base = segment.data, segment.addr & ~(page - 1)
        pad_front = segment.addr - base
        buf = b"\xFF" * pad_front + data
        if len(buf) % page:
            buf += b"\xFF" * (page - len(buf) % page)
        for off in range(0, len(buf), page):
            flash.program_page(base + off, buf[off:off + page])
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


def bmhd_blocks(chip):
    """The BMHD blocks tcprog knows for this chip. -> [(name, addr, size)]."""
    if chip.family == "tc4xx":
        return [("bmhd%d" % n, TC4_BMHD_BASE + n * TC4_BMHD_STRIDE, TC4_BMHD_STRIDE)
                for n in range(TC4_BMHD_COUNT)]
    ucb = chip.first("ucb")
    if ucb is None:
        raise Fail("no UCB region known for this target; pass -c/--chip or --device",
                   os.EX_USAGE)
    return [("bmhd0", ucb.base, _ucb_stride(chip.family))]


def read_words(dap, addr, size):
    """Read [addr, addr+size) one word at a time, reconnecting on a miss. -> bytes."""
    out = bytearray()
    for off in range(0, size, 4):
        value = dap.read_word_retry(addr + off)
        if value is None:
            raise Fail("no response reading 0x%08X" % (addr + off), os.EX_IOERR)
        out += value.to_bytes(4, "little")
    return bytes(out)


def _read_ucbs_tc4(dap, chip):
    """TC4xx: the known UCBs are the four BMHD blocks, each with a confirmation
    word and a copy."""
    out = []
    for name, block, _size in bmhd_blocks(chip):
        confirm = dap.read_word_retry(block + TC4_CONFIRM_OFF)
        copy = dap.read_word_retry(block + TC4_CONFIRM_COPY_OFF)
        state = "unreadable" if confirm is None \
            else _CONFIRM.get(confirm, "0x%08X (unknown)" % confirm)
        if confirm != copy:
            state += " (copy %s)" % ("unreadable" if copy is None else "0x%08X" % copy)
        header = read_words(dap, block, 16)
        ok, problems = validate_bmhd_tc4(header, block, chip)
        out.append({"name": name, "addr": block, "confirm": confirm, "state": state,
                    "bmhd": parse_bmhd_tc4(header), "bmhd_ok": ok, "bmhd_problems": problems})
    return out


def _read_classic_ucbs(dap, chip, bmhd_nbytes, parse, validate):
    """Shared TC2xx/TC3xx UCB read: every known UCB's confirmation word, plus the
    BMHD0 header. The BMHD block map (UCB_BLOCKS at ucb.base) is the same for both
    families; only the BMHD format differs (16 B TC3xx, 32 B TC2xx), passed in."""
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
                              for off in range(0, bmhd_nbytes, 4))
            entry["bmhd"] = parse(header)
            entry["bmhd_ok"], entry["bmhd_problems"] = validate(header, chip)
        out.append(entry)
    return out


def _read_ucbs_tc2(dap, chip):
    """TC2xx (AURIX 1G): 32-byte BMHD format (STADBM | BMI|BMHDID | range | CRC)."""
    return _read_classic_ucbs(dap, chip, 32, parse_bmhd_tc2, validate_bmhd_tc2)


def _read_ucbs_tc3(dap, chip):
    """TC3xx: 16-byte BMHD format (BMI | BMHDID | STAD | CRC)."""
    return _read_classic_ucbs(dap, chip, 16, parse_bmhd_tc3, validate_bmhd_tc3)


def _read_ucbs(dap, chip):
    """Read every known UCB's confirmation word (+ BMHD0 header). -> list of
    dicts {name, addr, confirm (int|None), state, bmhd (dict|None)}. On TC4xx
    only the BMHD blocks are known."""
    if chip is not None and chip.family == "tc4xx":
        return _read_ucbs_tc4(dap, chip)
    if chip is not None and chip.family == "tc2xx":
        return _read_ucbs_tc2(dap, chip)
    return _read_ucbs_tc3(dap, chip)
