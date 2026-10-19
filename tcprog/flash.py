# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Flash programmer (DMU command sequences), flash reads and blank-check.

tcprog issues the flash command sequences over DAP WRITEMEM instead of from a
CPU. The command interface is at 0xAF000000 on TC3xx and 0xF8080000 on TC4xx;
command addresses must use the non-cached 0xA0000000 PFLASH alias.
"""
import os
import time

from .chips import all_chips, noncached_alias
from .dap import Dap, DapError
from .util import EX_VERIFY, Fail, Progress, log

class ReadFailed(Fail):
    """A read that did not answer; addr is the first word or 1 KiB block that
    failed."""
    def __init__(self, addr):
        super().__init__("no response reading 0x%08X" % addr, os.EX_IOERR)
        self.addr = addr


class VerifyRejected(DapError):
    """The flash controller refused a Verify Erased command (a protected range,
    for example); a transport fault is a plain DapError."""


# Fourth-cycle data of the Erase / Verify Erased command sequences (AA50
# address, AA58 count or 0, AAA8 0x80, AAA8 this value).
# SAFETY: the Verify-Erased commands are one nibble from Erase.
# 0x5F = Verify Erased Logical Sector Range (non-destructive, array unchanged).
# 0x58 = Verify Erased WL, one wordline (non-destructive).
# 0x56 = Verify Erased Page, one page (non-destructive).
# 0x50 = Erase Logical Sector Range (DESTRUCTIVE).
CMD_ERASE_LSR_C4 = 0x50
CMD_VERIFY_ERASED_LSR_C4 = 0x5F
CMD_VERIFY_ERASED_WL_C4 = 0x58
CMD_VERIFY_ERASED_PAGE_C4 = 0x56
# Fourth-cycle data of Write Page / Write Burst (AA50 address, AA58 0, AAA8
# 0xA0, AAA8 this value). A burst programs the whole aligned group that Load
# Page filled. With too little data, TC3xx programs the previous contents of
# the assembly buffer and TC4xx refuses the command (SQER).
CMD_WRITE_PAGE_C4 = 0xAA
CMD_WRITE_BURST_C4 = 0xA6
PFLASH_PAGE, DFLASH_PAGE = 32, 8
PFLASH_WORDLINE = 0x400             # 1 KiB on TC3xx and TC4xx, one DAP block
DFLASH_WORDLINE = 0x200             # DFLASH and UCBs, TC3xx and TC4xx
PFLASH_LOG_SECTOR = 0x4000          # 16 KiB logical sector, TC3xx and TC4xx

# TC4 HCI_STATUS completion handshake and page-mode flags.
HCI_REQDONE = 1 << 31              # REQACK is bit 30
HCI_DFPAGE, HCI_PFPAGE = 1 << 24, 1 << 25

# Largest range one Erase Logical Sector Range may cover: PFLASH <= 512 KB,
# DFLASH <= 256 KB, a UCB range at most one UCB, all inside one physical sector
# or bank; the count is 8-bit.
# Chunks are aligned to these sizes, so they never straddle a physical sector
# (PFLASH physical sectors are 1 MB on TC3xx and 512 KB on TC4xx).
ERASE_MAX_PFLASH, ERASE_MAX_DFLASH = 512 * 1024, 256 * 1024


class FlashProfile:
    """Per-family parameters for the DMU command interface and its status/
    error registers. The command *sequences* are the same on TC3xx and TC4;
    the command base, the DMU register map, the EVER/PVER bit positions and the
    burst sizes differ; the driver class (FamilyLayout.driver: Tc3Flash,
    Tc4Flash) decides how a command is waited for. The Disable Protection status
    register is the chip's hf_protect (device file).
    """
    def __init__(self, *, cmd_base, err, err_name, ever_bit, pver_bit,
                 reject_mask, clr_err, clr_err_all, status,
                 busy_pflash=None, busy_dflash=None, load_lo, load_hi,
                 pflash_burst, dflash_burst, dp_selector):
        self.cmd_base = cmd_base
        self.err = err
        self.err_name = err_name
        self.ever = 1 << ever_bit
        self.pver = 1 << pver_bit
        self.reject_mask = reject_mask          # OPER | SQER | PROER (a reject, not an answer)
        self.clr_err = clr_err
        self.clr_err_all = clr_err_all
        self.status = status
        # TC3xx: the status busy bits for PFLASH, and for DFLASH as
        # ((lowest address, bits), ...) from the highest range down.
        self.busy_pflash = busy_pflash
        self.busy_dflash = busy_dflash
        # Load-page offsets for 32-bit writes: TC3xx takes lo at 0x55F0 then hi
        # at 0x55F4; TC4 sends both to 0x55F4.
        self.load_lo = load_lo
        self.load_hi = load_hi
        # Write Burst size: TC3xx 8 PFLASH pages (256 B), TC4xx 16 (512 B);
        # DFLASH 4 pages (32 B) on both.
        self.pflash_burst = pflash_burst
        self.dflash_burst = dflash_burst
        # Disable Protection: per flash ("pflash" / "dflash") the UCB selector
        # and the hf_protect bit that confirms it.
        self.dp_selector = dp_selector


class FamilyLayout:
    """Where a family keeps its UCBs and DFLASH, and how tcprog treats it.

    UCB index N of window W sits at <window base> + N * ucb_size. A UCB's ORIG
    confirmation word is at +confirm_off; its COPY is the UCB copy_off later
    (0: the same UCB) with the word at +copy_confirm_off. `ucbs` maps the UCBs
    `ucb` reads (BMHDs first, as bmhd0..) to their index in the first window;
    `ucb_names` names (window, index) for prompts, `ucb_unnamed` formats the
    rest. `flash` is the DMU profile and `driver` the flash driver class, both
    None where tcprog does not drive the flash; `halt_for_pflash` halts CPU0
    for PFLASH program/erase. `pflash_bmhd` is (address, size) of a BMHD kept
    in PFLASH instead of a UCB (TC2xx); `prodis` maps a UCB pair to its
    Disable Protection bit in hf_protect (TC3xx). `identity` names the identity
    register the family's id_addr points at ("chipid": SCU_CHIPID, "jtagid":
    CBS_JTAGID); `cpu0_pc` is the CPU0 PC register read to confirm a halt."""
    def __init__(self, *, name, ucb_size, confirm_off, copy_off, copy_confirm_off, ucbs,
                 ucb_names, ucb_unnamed, ucb_windows=None, dflash_window=None,
                 dflash_log_sector=None, flash=None, driver=None, halt_for_pflash=False,
                 pflash_bmhd=None, prodis=None, identity="chipid", cpu0_pc=None):
        self.name = name
        self.ucb_size = ucb_size
        self.confirm_off = confirm_off
        self.copy_off = copy_off
        self.copy_confirm_off = copy_confirm_off
        self.ucbs = ucbs
        self.ucb_names = ucb_names
        self.ucb_unnamed = ucb_unnamed
        # UCB windows [(base, size)]; one erase command covers at most one UCB.
        self.ucb_windows = ucb_windows
        # DFLASH address window [lo, hi) and its logical-sector size. PFLASH is
        # 0x80/0xA0 and 16 KiB on both; the DFLASH page is 8 B on both.
        self.dflash_window = dflash_window
        self.dflash_log_sector = dflash_log_sector
        self.flash = flash
        self.driver = driver
        self.halt_for_pflash = halt_for_pflash
        self.pflash_bmhd = pflash_bmhd
        self.prodis = prodis or {}
        self.identity = identity
        self.cpu0_pc = cpu0_pc


class DmuFlash:
    """The flash command sequences of the TC3xx DMU and the TC4xx HCI over a Dap.
    Writes to flash; destructive. The family's layout gives the register
    profile (command base, status/error map, EVER/PVER bits, burst sizes); a
    subclass gives how a command starts and how its completion is awaited. The
    chip names the hf_protect register.

    Erase and Verify Erased are separate methods, each accepting only its own
    command byte, and every rejected command is followed by Reset to Read."""
    # The 4th-cycle bytes each sequence may send. Erase (0x50) is one nibble
    # from the Verify Erased bytes, so it is in neither list.
    WRITE_CMDS = (CMD_WRITE_PAGE_C4, CMD_WRITE_BURST_C4)
    VERIFY_ERASED_CMDS = (CMD_VERIFY_ERASED_LSR_C4, CMD_VERIFY_ERASED_WL_C4,
                          CMD_VERIFY_ERASED_PAGE_C4)

    def __init__(self, dap, chip):
        self.dap = dap
        self.chip = chip
        self.layout = LAYOUTS[chip.family]
        self.p = self.layout.flash
        # [(lo, hi)] this driver has erased and programmed so far, for the
        # report when an operation stops part way, and the logical sectors a
        # write left alone because they already held the image's data.
        self.erased, self.programmed, self.unchanged = [], [], []
        # The erase or program command being sent or waited for: (what, lo, hi).
        # The flash controller finishes a command it has started, so a stop in
        # that window can leave the range erased or programmed.
        self.in_flight = None

    @staticmethod
    def _record(spans, lo, hi):
        if spans and spans[-1][1] == lo:
            spans[-1] = (spans[-1][0], hi)
        else:
            spans.append((lo, hi))

    def done_text(self):
        """What was erased and programmed so far, for an error message."""
        parts = ["%s %s" % (what, ", ".join("0x%08X..0x%08X" % span for span in spans))
                 for what, spans in (("erased", self.erased), ("programmed", self.programmed))
                 if spans]
        if self.in_flight:
            what, lo, hi = self.in_flight
            parts.append("0x%08X..0x%08X may be %s too (its command had started)"
                         % (lo, hi, what))
        return "; ".join(parts) if parts else "nothing was erased or programmed"

    def stopped(self, error, what):
        """The Fail to raise when `what` ("write", "erase") stops part way on a
        flash or transport error, or on Ctrl-C (exit 130): Reset to Read if the
        link still answers, and say what was already done. On Ctrl-C nothing else
        runs, not even the --after reset."""
        try:
            self.reset_to_read()
        except DapError:
            pass
        if isinstance(error, KeyboardInterrupt):
            return Fail("%s interrupted; %s" % (what, self.done_text()), 130)
        return Fail("%s failed: %s; %s" % (what, error, self.done_text()), os.EX_IOERR)

    def _cmd(self, offset, value):
        self.dap.write_word(self.p.cmd_base | offset, value)

    def clear_status(self):
        self._cmd(0x5554, 0xFA)

    def reset_to_read(self):
        self._cmd(0x5554, 0xF0)

    def clear_errors(self):
        """Clear the DMU error latches so a following verify reads clean."""
        self.dap.write_word(self.p.clr_err, self.p.clr_err_all)

    def _leave_error_mode(self):
        """A rejected command leaves the command interpreter in error mode until
        Reset to Read; without it every later sequence fails with SQER. Then
        clear the error latches."""
        self.reset_to_read()
        self.clear_errors()

    def _poll_status(self, done, what, timeout, on_tick=None):
        """Poll the status register until done(status) is true. on_tick(status)
        runs after each miss (it may raise). Raises DapError if the register
        stops answering or `timeout` seconds pass."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.dap.try_read_word(self.p.status)
            if status is None:
                raise DapError("flash status register unreadable during flash op")
            if done(status):
                return
            if on_tick:
                on_tick(status)
            if time.monotonic() > deadline:
                raise DapError("%s past %.1fs (status=0x%08X)" % (what, timeout, status))

    # How a family starts a command and waits for it (subclasses).
    def _begin_cmd(self):
        pass

    def _wait_cmd_done(self, addr, timeout=5.0):
        raise NotImplementedError

    def _wait_page_mode(self, page_addr):
        pass

    def _wait_protection_done(self):
        pass

    def check_errors(self, what, fail_mask=0):
        """Raise if the last command was rejected (OPER/SQER/PROER) or failed one
        of fail_mask (PVER after a write, EVER after an erase). A rejected command
        leaves the interpreter in ERROR MODE, so Reset to Read before raising."""
        err = self.dap.read_word(self.p.err)
        if err & (self.p.reject_mask | fail_mask):
            self._leave_error_mode()
            raise DapError("%s failed (%s=0x%08X)" % (what, self.p.err_name, err))

    def _enter_page_mode(self, page_addr):
        page_addr = noncached_alias(page_addr)
        if _is_pflash(page_addr):
            self._cmd(0x5554, 0x50)                 # program flash
        elif _is_dflash(page_addr, self.layout):
            self._cmd(0x5554, 0x5D)                 # data flash
        else:
            raise DapError("0x%08X is not a programmable flash page" % page_addr)

    def _program_group(self, addr, data, cycle4, what):
        """Program one aligned page or burst with Write Page / Write Burst. The
        data must fill the group: with a short load TC3xx programs stale buffer
        data and TC4xx refuses the command."""
        if cycle4 not in self.WRITE_CMDS:
            raise ValueError("not a Write Page/Burst command: 0x%02X" % cycle4)
        self._begin_cmd()
        self._enter_page_mode(addr)
        self._wait_page_mode(addr)
        self._begin_cmd()
        for off in range(0, len(data), 8):
            chunk = data[off:off + 8].ljust(8, b"\xFF")
            self._cmd(self.p.load_lo, int.from_bytes(chunk[0:4], "little"))
            self._cmd(self.p.load_hi, int.from_bytes(chunk[4:8], "little"))
        self._begin_cmd()
        self._cmd(0xAA50, noncached_alias(addr))
        self._cmd(0xAA58, 0x00)
        self._cmd(0xAAA8, 0xA0)
        self._cmd(0xAAA8, cycle4)
        self._wait_cmd_done(addr)
        self.check_errors("program %s 0x%08X" % (what, addr), self.p.pver)

    def program(self, addr, data, on_progress=None):
        """Program `data` at `addr`, padded with 0xFF to whole pages. A Write
        Burst only where the data covers the whole aligned burst, so no page
        outside the data is programmed; single pages elsewhere.
        on_progress(bytes_of_data_done) is called after each page or burst."""
        dflash = _is_dflash(addr, self.layout)
        page = DFLASH_PAGE if dflash else PFLASH_PAGE
        burst = self.p.dflash_burst if dflash else self.p.pflash_burst
        base = addr & ~(page - 1)
        pad_front = addr - base
        buf = b"\xFF" * pad_front + data
        if len(buf) % page:
            buf += b"\xFF" * (page - len(buf) % page)
        off = 0
        while off < len(buf):
            start = off
            group = burst if not (base + off) % burst and off + burst <= len(buf) else page
            self.in_flight = ("programmed", base + off, base + off + group)
            if not (base + off) % burst and off + burst <= len(buf):
                self._program_group(base + off, buf[off:off + burst], CMD_WRITE_BURST_C4,
                                    "burst")
                off += burst
            else:
                self._program_group(base + off, buf[off:off + page], CMD_WRITE_PAGE_C4,
                                    "page")
                off += page
            self.in_flight = None
            self._record(self.programmed, base + start, base + off)
            if on_progress:
                on_progress(max(0, min(off - pad_front, len(data))))

    def _erase_sectors(self, sector_addr, count):
        self._cmd(0xAA50, noncached_alias(sector_addr))
        self._cmd(0xAA58, count)
        self._cmd(0xAAA8, 0x80)
        self._cmd(0xAAA8, CMD_ERASE_LSR_C4)

    def erase_range(self, first, count, sector, timeout=10.0):
        """Erase `count` logical sectors of size `sector` from `first`, split
        into commands that stay within ERASE_MAX_* and one bank, each waited for
        and error-checked (EVER = erase verify error)."""
        if _is_pflash(first):
            limit = ERASE_MAX_PFLASH
        elif _is_ucb(first, self.layout):
            limit = self.layout.ucb_size           # one UCB per command
        else:
            limit = ERASE_MAX_DFLASH
        addr, end = first, first + count * sector
        while addr < end:
            stop = min((addr // limit + 1) * limit, end)
            n = (stop - addr) // sector
            self.in_flight = ("erased", addr, stop)
            self._begin_cmd()
            self._erase_sectors(addr, n)
            self._wait_cmd_done(addr, timeout)
            self.check_errors("erase %d logical sector(s) at 0x%08X" % (n, addr), self.p.ever)
            self.in_flight = None
            self._record(self.erased, addr, stop)
            addr = stop

    def erase_runs(self, plan):
        """Erase every (first, count, logical sector size) range of an erase plan."""
        for first, count, sector in plan:
            log("tcprog: erasing %d logical sector(s) from 0x%08X" % (count, first), verbose=True)
            self.erase_range(first, count, sector)

    def disable_protection(self, which, words):
        """Disable Protection for "pflash" or "dflash" with the 8-word password.
        -> True if it was already disabled, False if this call disabled it.
        Raises DapError if the password is refused; the DMU then refuses every
        further attempt until the next reset."""
        protect = self.chip.hf_protect
        if protect is None:
            raise DapError("%s has no hf_protect register in its device file; add "
                           "base = \"%s\" to it, or hf_protect" % (self.chip.name,
                                                                  self.chip.family))
        uc, bit = self.p.dp_selector[which]
        if self.dap.read_word(protect) & bit:
            return True
        # A previously rejected command may have left error mode behind; in it
        # the sequence below fails with SQER before the password is checked.
        self._leave_error_mode()
        self._begin_cmd()
        self._cmd(0x553C, uc)
        for word in words:
            self._cmd(0x553C, word & 0xFFFFFFFF)
        self._wait_protection_done()
        err = self.dap.read_word(self.p.err)
        status = self.dap.read_word(protect)
        if err & self.p.reject_mask or not status & bit:
            self._leave_error_mode()
            raise DapError("%s protection: password refused (%s=0x%08X, protect=0x%08X); "
                           "a reset is needed before trying again"
                           % (which.upper(), self.p.err_name, err, status))
        return False

    def verify_erased_sectors(self, sector_addr, count):
        """Blank-check [sector_addr, +count sectors) with the flash state machine
        instead of reading the array. Non-destructive: issues Verify Erased
        Logical Sector Range (0x80, 0x5F), NOT Erase. Returns True if the range
        is fully blank (the error register's EVER stayed clear), False if any
        cell is programmed. Raises DapError on a transport fault or a sequence/
        protection error (which is not a blank/not-blank answer)."""
        return self._verify_erased(sector_addr, count, CMD_VERIFY_ERASED_LSR_C4)

    def verify_erased_wordline(self, wordline_addr):
        """Blank-check one wordline (Verify Erased WL, 0x80, 0x58), like
        verify_erased_sectors."""
        return self._verify_erased(wordline_addr, 0, CMD_VERIFY_ERASED_WL_C4)

    def verify_erased_page(self, page_addr):
        """Blank-check one page (Verify Erased Page, 0x80, 0x56), like
        verify_erased_sectors. Erased PFLASH fails the read ECC and erased DFLASH
        reads as data, so this is how to tell an erased page from a programmed
        one without reading it."""
        return self._verify_erased(page_addr, 0, CMD_VERIFY_ERASED_PAGE_C4)

    def _verify_erased(self, addr, count, cycle4):
        # Accepts the Verify Erased commands only: the Erase byte differs from
        # them by one nibble, so any other value is refused before it is sent.
        if cycle4 not in self.VERIFY_ERASED_CMDS:
            raise ValueError("not a Verify Erased command: 0x%02X" % cycle4)
        p = self.p
        addr = noncached_alias(addr)
        self._begin_cmd()
        self.clear_errors()
        self._cmd(0xAA50, addr)
        self._cmd(0xAA58, count)
        self._cmd(0xAAA8, 0x80)
        self._cmd(0xAAA8, cycle4)
        self._wait_cmd_done(addr)
        err = self.dap.read_word(p.err)
        if err & p.reject_mask:              # OPER / SQER / PROER
            self._leave_error_mode()
            raise VerifyRejected("verify-erased rejected (%s=0x%08X)" % (p.err_name, err))
        blank = not (err & p.ever)
        self.clear_errors()
        return blank


class Tc3Flash(DmuFlash):
    """TC3xx DMU: a command is done when the busy bits in HF_STATUS for the flash
    it addresses are clear."""

    def _wait_cmd_done(self, addr, timeout=5.0):
        busy = self.p.busy_pflash
        if _is_dflash(addr, self.layout):
            busy = next(bits for base, bits in self.p.busy_dflash if addr >= base)
        self._poll_status(lambda status: not status & busy, "flash busy", timeout)


class Tc4Flash(DmuFlash):
    """TC4xx HCI: Clear Status before each command sequence, then wait for
    REQDONE in HCI_STATUS. Only Clear Status (or Reset to Read) resets
    REQACK/REQDONE, so without it a REQDONE wait sees the previous command's
    completion."""

    def _begin_cmd(self):
        self.clear_status()

    def _wait_cmd_done(self, addr, timeout=5.0):
        self._poll_status(lambda status: status & HCI_REQDONE, "flash cmd not done", timeout)

    def _wait_page_mode(self, page_addr, timeout=1.0):
        """Wait for HCI_STATUS.PFPAGE/DFPAGE after Enter Page Mode, failing
        early on a sequence/protection error."""
        bit = HCI_PFPAGE if _is_pflash(page_addr) else HCI_DFPAGE

        def fail_early(_status):
            self.check_errors("enter page mode at 0x%08X" % page_addr)
        self._poll_status(lambda status: status & bit, "page mode not entered", timeout,
                          fail_early)

    def _wait_protection_done(self):
        self._wait_cmd_done(0)


for _driver in (Tc3Flash, Tc4Flash):
    if CMD_ERASE_LSR_C4 in _driver.WRITE_CMDS + _driver.VERIFY_ERASED_CMDS:
        raise ImportError("the Erase command byte is in a write or verify allowlist")


def flash_driver(dap, chip):
    """The flash driver for the chip's family. Raises DapError where tcprog does
    not drive the flash (TC2xx)."""
    return driven_layout(chip.family).driver(dap, chip)


# TC3xx: 512 B UCBs at 0xAF400000, confirmation at +0x1F0, each COPY 8 UCBs
# (0x1000) later. The ORIG indices are the Disable-Protection selectors
# (BMHD0 0x00, PFLASH 0x10, DFLASH 0x11, DBG 0x12). HF_PROTECT PRODISP/
# PRODISD/PRODISDBG (bits 0/1/2) show Disable Protection in effect.
_TC3_UCBS = {"bmhd0": 0x00, "bmhd1": 0x01, "bmhd2": 0x02, "bmhd3": 0x03,
             "pflash": 0x10, "dflash": 0x11, "dbg": 0x12}
_TC3_COPY_OFF = 0x1000
_TC3_PRODIS = {"pflash": 0, "dflash": 1, "dbg": 2}

# TC3xx DMU HF_* registers: EVER bit4, PVER bit3; OPER/SQER/PROER are bits
# 0/1/2. HF_STATUS busy bits: P0BUSY..P5BUSY (bits 2..7) for PFLASH, D1BUSY
# (bit 1) for DFLASH1 (0xAFC00000 up), D0BUSY (bit 0) for the rest of
# 0xAFxxxxxx (DFLASH0, UCB, CFS).
PROFILE_TC3XX = FlashProfile(
    cmd_base=0xAF000000,
    err=0xF8040034, err_name="HF_ERRSR", ever_bit=4, pver_bit=3,
    reject_mask=(1 << 0) | (1 << 1) | (1 << 2),
    clr_err=0xF8040038, clr_err_all=(1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5),
    status=0xF8040010,
    busy_pflash=0xFC, busy_dflash=((0xAFC00000, 0x02), (0xAF000000, 0x01)),
    load_lo=0x55F0, load_hi=0x55F4,
    pflash_burst=0x100, dflash_burst=0x20,
    dp_selector={which: (_TC3_UCBS[which], 1 << _TC3_PRODIS[which])
                 for which in ("pflash", "dflash")})

# TC4Dx DMU HCI_* registers. Command base 0xF8080000. HCI_ERR @0xF8040010:
# EVER bit7, PVER bit6, OPER bit16, SQER bit1, PROER bit2. HCI_CLRERR
# @0xF8040014 clears CADER..CEVER. HCI_STATUS @0xF8040004 carries the
# REQACK/REQDONE handshake.
PROFILE_TC4XX = FlashProfile(
    cmd_base=0xF8080000,
    err=0xF8040010, err_name="HCI_ERR", ever_bit=7, pver_bit=6,
    reject_mask=(1 << 16) | (1 << 1) | (1 << 2),
    clr_err=0xF8040014,
    clr_err_all=(1 << 0) | (1 << 1) | (1 << 2) | (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7),
    status=0xF8040004, load_lo=0x55F4, load_hi=0x55F4,
    pflash_burst=0x200, dflash_burst=0x20,
    # One UCB_RTC_FLASH covers both: selector 0x10 lifts global read and write
    # protection of HOST PFLASH and DFLASH and sets PRODISFL (bit 13).
    dp_selector={"pflash": (0x10, 1 << 13), "dflash": (0x10, 1 << 13)})

# TC2xx: 1 KiB UCBs, confirmation at +0x70 (copy +0x78); UCB0 PFLASH, UCB1
# DFLASH, UCB5 DBG. The TC2xx BMHDs are not in a UCB but in PFLASH. The flash
# status lives in the FLASH0 module, which tcprog does not drive.
LAYOUT_TC2XX = FamilyLayout(
    name="tc2xx", ucb_size=0x400, confirm_off=0x70, copy_off=0, copy_confirm_off=0x78,
    ucbs={"pflash": 0x00, "dflash": 0x01, "dbg": 0x05}, ucb_names={}, ucb_unnamed="{index}",
    pflash_bmhd=(0xA0000000, 32))

# TC3xx: one UCB window; the DFLASH logical sector is 4 KiB in single-ended
# mode, complement-sensing mode (2 KiB) is not modelled.
LAYOUT_TC3XX = FamilyLayout(
    name="tc3xx", ucb_size=0x200, confirm_off=0x1F0, copy_off=_TC3_COPY_OFF,
    copy_confirm_off=0x1F0, ucbs=_TC3_UCBS,
    ucb_names={**{(0, index): name.upper() + "_ORIG" for name, index in _TC3_UCBS.items()},
               **{(0, index + _TC3_COPY_OFF // 0x200): name.upper() + "_COPY"
                  for name, index in _TC3_UCBS.items()}},
    ucb_unnamed="{index}",
    ucb_windows=((0xAF400000, 0x6000),), dflash_window=(0xAF000000, 0xB0000000),
    dflash_log_sector=0x1000, flash=PROFILE_TC3XX, driver=Tc3Flash, halt_for_pflash=True,
    prodis=_TC3_PRODIS, cpu0_pc=0xF881FE08)

# TC4xx: UCB0 0xAE400000 and UCB1 0xAEC00000, 2 KiB per UCB. The four BMHD
# UCBs (UCB_RTC_BMHD0..3) each hold the 8-word password at +0x7D0 and the
# ORIG and COPY confirmation words at +0x7F0/+0x7F8.
_TC4_BMHDS = {"bmhd%d" % n: 9 + n for n in range(4)}
LAYOUT_TC4XX = FamilyLayout(
    name="tc4xx", ucb_size=0x800, confirm_off=0x7F0, copy_off=0, copy_confirm_off=0x7F8,
    ucbs=_TC4_BMHDS,
    ucb_names={**{(0, index): "RTC_" + name.upper() for name, index in _TC4_BMHDS.items()},
               (0, 21): "RTC_FLASH_ORIG", (0, 22): "RTC_FLASH_COPY",
               (1, 1): "DBGCS_ORIG", (1, 2): "DBGCS_COPY"},
    ucb_unnamed="UCB{window}_{index:02d}",
    ucb_windows=((0xAE400000, 0x14000), (0xAEC00000, 0xD000)),
    dflash_window=(0xAE000000, 0xAF000000), dflash_log_sector=0x800, flash=PROFILE_TC4XX,
    driver=Tc4Flash, identity="jtagid")

LAYOUTS = {layout.name: layout for layout in (LAYOUT_TC2XX, LAYOUT_TC3XX, LAYOUT_TC4XX)}
# The families whose flash tcprog drives.
DRIVEN = tuple(name for name, layout in LAYOUTS.items() if layout.driver is not None)


def identity_kind(id_addr, chip=None):
    """What the identity register at id_addr is ("chipid" or "jtagid"): from the
    chip's own family when it reads its identity there (a --device file
    included), else from the built-in families that do; None if none does."""
    if chip is not None and chip.id_addr == id_addr and chip.family in LAYOUTS:
        return LAYOUTS[chip.family].identity
    kinds = {LAYOUTS[known.family].identity for known in all_chips()
             if known.id_addr == id_addr and known.family in LAYOUTS}
    return kinds.pop() if len(kinds) == 1 else None


def drives(family):
    return family in DRIVEN


def driven_layout(family):
    """The layout of a family whose flash tcprog drives. TC2xx is not covered
    (FLASH0 FSR layout differs); callers gate on that before asking."""
    if not drives(family):
        raise DapError("no flash profile for family %r; tcprog drives the flash of %s"
                       % (family, " and ".join(DRIVEN)))
    return LAYOUTS[family]


def device_map_problem(chip):
    """None if the chip's UCB ranges are exactly the family's UCB windows, else
    why not. Both decide about UCBs: the device map drives the
    --enable-ucb-write gate, the layout the UCB erase size. If they disagree, a
    UCB could be written without the gate or erased as a larger DFLASH sector."""
    layout = driven_layout(chip.family)
    ucbs = sorted((r.base, r.size) for r in chip.outer_ranges() if r.kind == "ucb")
    windows = sorted(layout.ucb_windows)
    if ucbs != windows:
        return ("its UCB ranges (%s) are not the %s UCB windows (%s)"
                % (", ".join("0x%08X+0x%X" % u for u in ucbs) or "none", chip.family,
                   ", ".join("0x%08X+0x%X" % w for w in windows)))
    for r in chip.memory:
        if r.kind != "ucb" and any(r.base < base + size and base < r.end
                                   for base, size in windows):
            return ("its %s range %s at 0x%08X overlaps a UCB window"
                    % (r.kind, r.name, r.base))
    return None


def _is_pflash(addr):
    return (addr & 0xFE000000) in (0x80000000, 0xA0000000)


def _is_dflash(addr, layout):
    """True if addr is in this family's DFLASH window (TC3xx 0xAFxxxxxx, TC4
    0xAExxxxxx)."""
    lo, hi = layout.dflash_window
    return lo <= addr < hi


def _is_ucb(addr, layout):
    return any(base <= addr < base + size for base, size in layout.ucb_windows)


def _log_sector_size(addr, layout):
    """Logical-sector size for the flash addr lives in: 16 KiB PFLASH, one UCB
    for a UCB address, the family's DFLASH sector for the rest of DFLASH, or
    None if addr is neither."""
    if _is_pflash(addr):
        return PFLASH_LOG_SECTOR
    if _is_ucb(addr, layout):
        return layout.ucb_size
    if _is_dflash(addr, layout):
        return layout.dflash_log_sector
    return None


def _flash_sectors(addr, length, sector):
    """(first_sector_addr, count) of the logical sectors of size `sector` that
    cover [addr, addr+length)."""
    first = addr & ~(sector - 1)
    last = (addr + length - 1) & ~(sector - 1)
    return first, (last - first) // sector + 1


def _block_span(addr, length):
    """[start, end) of the whole DAP blocks covering [addr, addr+length)."""
    return addr & ~(Dap.BLOCK - 1), (addr + length + Dap.BLOCK - 1) & ~(Dap.BLOCK - 1)


def _merge_runs(spans):
    """Merge sorted (lo, hi) spans that touch into longer runs. -> [(lo, hi)]."""
    out = []
    for lo, hi in spans:
        if out and out[-1][1] == lo:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def _first_failing(dap, blocks):
    """The first of the 1 KiB blocks that does not answer a read on its own
    (after a miss the target needs the connect handshake again)."""
    dap.connect()
    for block in blocks:
        try:
            dap.read_block(block)
        except DapError:
            dap.connect()
            return block
    return blocks[0]


def _read_region(dap, addr, length, on_progress=None):
    """Read exactly [addr, addr+length), never past the end: erased cells fail
    the read ECC, so over-reading the erased tail of a partly programmed block
    fails. Batch the whole blocks that fit, read the ragged ends word by word.
    on_progress(position) gets the address read up to after each step."""
    end = addr + length
    lo = (addr + Dap.BLOCK - 1) & ~(Dap.BLOCK - 1)   # first block fully inside
    hi = end & ~(Dap.BLOCK - 1)                       # end of the last such block
    if lo >= hi:
        out = read_words(dap, addr, length)[:length]
    else:
        out = read_words(dap, addr, lo - addr)
        if on_progress:
            on_progress(lo)
        blocks = list(range(lo, hi, Dap.BLOCK))
        for index in range(0, len(blocks), Dap.GROUP):
            group = blocks[index:index + Dap.GROUP]
            try:
                out += dap.read_blocks(group)
            except DapError:
                raise ReadFailed(_first_failing(dap, group))
            if on_progress:
                on_progress(blocks[min(index + Dap.GROUP, len(blocks)) - 1] + Dap.BLOCK)
        out += read_words(dap, hi, end - hi)
    if on_progress:
        on_progress(end)
    return bytes(out)[:length]


def read_region_sparse(dap, addr, length, on_progress=None, on_miss=None):
    """Read [addr, addr+length) tolerating erased/unreadable blocks.

    Erased flash traps with a no-response on TC3xx/TC4xx parts, and after a miss the
    target ignores reads until the connect handshake is re-sent, so every miss
    reconnects. Blocks that answer are collected; blocks that do not are left
    out. Returns (segments, missed_blocks) where segments is a list of
    (block-aligned addr, bytes) runs of contiguous answered blocks, each cropped
    to the requested [addr, addr+length) window.

    on_progress(done_blocks) is called as it goes. on_miss(block_addr), if
    given, is asked for a block that did not answer and returns the
    [(addr, bytes)] pieces it could still read; such a block is not counted as
    missed.
    """
    block = Dap.BLOCK
    start, end = _block_span(addr, length)
    block_addrs = list(range(start, end, block))
    found, missed = [], 0
    for i in range(0, len(block_addrs), Dap.GROUP):
        addrs = block_addrs[i:i + Dap.GROUP]
        data = None
        if len(addrs) > 1:
            try:
                data = dap.read_blocks(addrs)
            except DapError:
                dap.connect()
        if data is not None:
            found += [(a, data[j * block:(j + 1) * block]) for j, a in enumerate(addrs)]
        else:
            # the group hit a hole: fall back to one block at a time
            for a in addrs:
                try:
                    found.append((a, dap.read_block(a)))
                except DapError:
                    dap.connect()
                    if on_miss is None:
                        missed += 1
                    else:
                        found += on_miss(a)
        if on_progress:
            on_progress(i + len(addrs))
    lo, hi = addr, addr + length
    return [(max(a, lo), data[max(a, lo) - a:min(a + len(data), hi) - a])
            for a, data in _join_segments(found) if max(a, lo) < min(a + len(data), hi)], missed


def blankcheck_geometry(chip, addr, length):
    """(logical sector, wordline, page) of the flash [addr, addr+length) lies in,
    for the Verify Erased commands, or None where there is no hardware
    blank-check: TC2xx, and anything but PFLASH, DFLASH and the UCBs."""
    if chip is None or not drives(chip.family):
        return None
    layout = driven_layout(chip.family)
    kind = chip.kind_at(addr)
    if chip.kind_at(addr + length - 1) != kind:
        return None
    if kind == "pflash" and _is_pflash(addr):
        return PFLASH_LOG_SECTOR, PFLASH_WORDLINE, PFLASH_PAGE
    if kind == "ucb" and _is_ucb(addr, layout):
        return layout.ucb_size, DFLASH_WORDLINE, DFLASH_PAGE
    if kind == "dflash" and _is_dflash(addr, layout):
        return layout.dflash_log_sector, DFLASH_WORDLINE, DFLASH_PAGE
    return None


def blank_map(dap, addr, length, chip, sector, on_progress=None):
    """Occupancy map of [addr, addr+length) by logical sector, using the flash
    controller's hardware blank-check (no array reads, no ECC traps).

    Returns a list of (logical sector address, blank: bool), one entry per logical
    sector. A logical sector whose check the controller refuses counts as
    programmed, so the read decides. Covers TC3xx (DMU HF_*) and TC4xx (HCI_*) via
    the family profile.
    Issues only Verify-Erased (non-destructive) sequences.
    """
    first, count = _flash_sectors(addr, length, sector)
    sectors = range(first, first + count * sector, sector)
    flash = flash_driver(dap, chip)
    flash.clear_status()
    out = []
    for i, sa in enumerate(sectors):
        try:
            blank = flash.verify_erased_sectors(sa, 1)
        except VerifyRejected:
            blank = False
        out.append((sa, blank))
        if on_progress:
            on_progress(i + 1, len(sectors))
    flash.reset_to_read()
    return out


def read_region_blankcheck(dap, addr, length, chip, on_map=None, on_read=None):
    """Sparse read of [addr, addr+length) guided by the hardware blank-check:
    map occupancy by logical sector (no array reads), then read only the
    programmed data of the programmed logical sectors.

    PFLASH: erased cells fail the read ECC, so the programmed sectors are read
    by block, and a block (one wordline) that does not answer is checked with
    Verify Erased WL and, if it holds data, page by page.
    DFLASH: erased cells read as 0x00, so every wordline of a programmed sector
    is checked, a programmed wordline is read, and only its all-zero pages are
    checked with Verify Erased Page (a page holding any other byte is
    programmed). A wordline that does not read is checked page by page.
    UCBs: a programmed UCB (one logical sector) is read whole. Its blank pages
    read as 0x00, the value the UCB holds there either way, so they are not
    told apart from programmed zeros. A UCB that does not read is checked page
    by page.

    Returns (segments, unreadable). on_map(done, total) reports the blank-check
    phase in sectors, on_read(done, total) the read phase in bytes.
    """
    sector, wordline, page = blankcheck_geometry(chip, addr, length)
    occ = blank_map(dap, addr, length, chip, sector, on_progress=on_map)
    flash = flash_driver(dap, chip)
    bad_pages = []

    def programmed_pages(lo, hi):
        """The programmed pages of the wordline at lo, cropped to [lo, hi), as
        [(lo, hi)] runs; all of it if the controller refuses the check."""
        try:
            if flash.verify_erased_wordline(lo & ~(wordline - 1)):
                return []
            pages = [p for p in range(lo & ~(page - 1), hi, page)
                     if not flash.verify_erased_page(p)]
        except VerifyRejected:
            pages = list(range(lo & ~(page - 1), hi, page))
        finally:
            flash.reset_to_read()
        return [(max(a, lo), min(b, hi)) for a, b in _merge_runs((p, p + page) for p in pages)]

    def missed_block(block):
        """read_region_sparse: a PFLASH block that did not answer."""
        return read_pages(programmed_pages(block, block + wordline))

    def read_pages(runs):
        found = []
        for lo, hi in runs:
            try:
                found.append((lo, _read_region(dap, lo, hi - lo)))
            except (DapError, Fail):
                dap.connect()
                bad_pages.append((lo, hi))
        return found

    # The programmed sectors, merged and clipped to [addr, addr+length).
    windows = []
    for a, b in _merge_runs((sa, sa + sector) for sa, blank in occ if not blank):
        lo, hi = max(a, addr), min(b, addr + length)
        if hi > lo:
            windows.append((lo, hi))
    total = sum(hi - lo for lo, hi in windows)
    done = 0                                  # bytes of the windows already read

    def report(blocks):
        """read_region_sparse progress for the current window, as bytes of all."""
        on_read(done + min(blocks * Dap.BLOCK, hi - lo), total)

    def dflash_wordline(lo, hi):
        """The programmed data of [lo, hi) inside one DFLASH or UCB wordline."""
        try:
            if flash.verify_erased_wordline(lo & ~(wordline - 1)):
                return []
        except VerifyRejected:
            pass
        finally:
            flash.reset_to_read()
        try:
            data = _read_region(dap, lo, hi - lo)
        except (DapError, Fail):
            dap.connect()
            return read_pages(programmed_pages(lo, hi))
        pages = []
        try:
            for p in range(lo, hi, page):
                chunk = data[p - lo:p - lo + page]
                if any(chunk) or not flash.verify_erased_page(p):
                    pages.append(p)
        except VerifyRejected:
            pages = list(range(lo, hi, page))
        finally:
            flash.reset_to_read()
        return [(a, data[a - lo:b - lo])
                for a, b in _merge_runs((p, min(p + page, hi)) for p in pages)]

    def whole_ucb(lo, hi):
        """The data of [lo, hi) inside one programmed UCB."""
        try:
            return [(lo, _read_region(dap, lo, hi - lo))]
        except (DapError, Fail):
            dap.connect()
        found = []
        for wl in range(lo & ~(wordline - 1), hi, wordline):
            found += read_pages(programmed_pages(max(wl, lo), min(wl + wordline, hi)))
        return found

    ucb = chip.kind_at(addr) == "ucb"
    segments = []
    for lo, hi in windows:
        if ucb:
            for sa in range(lo & ~(sector - 1), hi, sector):
                segments.extend(whole_ucb(max(sa, lo), min(sa + sector, hi)))
                if on_read:
                    on_read(done + min(sa + sector, hi) - lo, total)
        elif wordline == PFLASH_WORDLINE:
            segs, _ = read_region_sparse(
                dap, lo, hi - lo,
                on_miss=missed_block,
                on_progress=report if on_read else None)
            segments.extend(segs)
        else:
            for wl in range(lo & ~(wordline - 1), hi, wordline):
                segments.extend(dflash_wordline(max(wl, lo), min(wl + wordline, hi)))
                if on_read:
                    on_read(done + min(wl + wordline, hi) - lo, total)
        done += hi - lo
    segments = _join_segments(sorted(segments))

    # Per-sector fidelity check: a sector the blank-check called programmed but
    # from which nothing read back (e.g. uncorrectable ECC) is reported, not
    # faked. Done per sector so a readable neighbour cannot mask it.
    def _has_data(lo, hi):
        return any(sa < hi and sa + len(d) > lo for sa, d in segments)

    windows = ((max(sa, addr), min(sa + sector, addr + length)) for sa, blank in occ
               if not blank)
    empty = [(lo, hi) for lo, hi in windows if hi > lo and not _has_data(lo, hi)]
    # Programmed pages that did not read back, outside the sectors already
    # reported as a whole, cropped to the window.
    bad = [(max(lo, addr), min(hi, addr + length)) for lo, hi in bad_pages
           if not any(e_lo <= lo < e_hi for e_lo, e_hi in empty)
           and max(lo, addr) < min(hi, addr + length)]
    return segments, _merge_runs(sorted(empty + bad))


def _join_segments(segments):
    """Join sorted (addr, bytes) segments that touch. -> [(addr, bytes)]"""
    out = []
    for seg_addr, data in segments:
        if out and out[-1][0] + len(out[-1][1]) == seg_addr:
            out[-1] = (out[-1][0], out[-1][1] + bytes(data))
        else:
            out.append((seg_addr, bytes(data)))
    return out


def logical_sector_ranges(chip, image):
    """The logical sector ranges a write of `image` erases, as [(first logical
    sector, count, logical sector size)], each logical sector once even if
    several segments share it. Raises ValueError
    for a segment that is not PFLASH, DFLASH or a UCB in the device map, so a
    write is refused before anything is erased."""
    layout = driven_layout(chip.family)
    sectors = set()
    for segment in image.segments:
        kind = chip.kind_at(segment.addr)
        size = _log_sector_size(segment.addr, layout) if kind in ("pflash", "dflash", "ucb") else None
        if size is None:
            raise ValueError("segment 0x%08X..0x%08X is %s, not PFLASH, DFLASH or a UCB; "
                             "tcprog only writes flash" % (segment.addr, segment.end, kind))
        first, count = _flash_sectors(segment.addr, len(segment.data), size)
        sectors.update((first + k * size, size) for k in range(count))
    runs = []
    for addr, size in sorted(sectors):
        if runs and runs[-1][2] == size and runs[-1][0] + runs[-1][1] * size == addr:
            runs[-1][1] += 1
        else:
            runs.append([addr, 1, size])
    return [tuple(run) for run in runs]


def _page_map(segments, page):
    """{page address: page bytes} of page-aligned (addr, bytes) segments."""
    return {addr + off: bytes(data[off:off + page])
            for addr, data in segments for off in range(0, len(data), page)}


def _image_pages(image, lo, hi, page):
    """The pages a write of `image` programs inside [lo, hi), padded with 0xFF to
    whole pages the way DmuFlash.program pads a segment. -> [(addr, bytes)]"""
    out = []
    for segment in image.segments:
        a, b = max(segment.addr, lo), min(segment.end, hi)
        if a >= b:
            continue
        base, end = a & ~(page - 1), (b + page - 1) & ~(page - 1)
        out.append((base, b"\xFF" * (a - base) + segment.data[a - segment.addr:b - segment.addr]
                    + b"\xFF" * (end - b)))
    return out


def unchanged_logical_sectors(dap, chip, image, plan):
    """The logical sectors of `plan` that already hold exactly what a write of
    `image` leaves there: its pages, padded as program() pads them, and no other
    programmed page. -> {logical sector address}. Read-only: the --skip-blank
    blank-check and reads; a logical sector with unreadable data counts as
    changed."""
    unchanged = set()
    for first, count, size in plan:
        have, unreadable = read_region_blankcheck(dap, first, count * size, chip)
        page = PFLASH_PAGE if _is_pflash(first) else DFLASH_PAGE
        have = _page_map(have, page)
        for lo in range(first, first + count * size, size):
            hi = lo + size
            if any(a < hi and lo < b for a, b in unreadable):
                continue
            if ({addr: data for addr, data in have.items() if lo <= addr < hi}
                    == _page_map(_image_pages(image, lo, hi, page), page)):
                unchanged.add(lo)
    return unchanged


def _program(dap, image, args, chip, plan):
    """Erase the planned logical sectors, program and verify every segment. With
    --erase changed, logical sectors that already hold the image's data are
    compared first and then neither erased nor programmed. -> the driver, whose
    `erased`/`programmed`/`unchanged` say what was done. Raises Fail when it
    stops part way (saying what was already done) or when the read-back differs."""
    flash = flash_driver(dap, chip)
    try:
        pieces = [(segment.addr, segment.data) for segment in image.segments]
        if args.erase == "changed":
            unchanged = unchanged_logical_sectors(dap, chip, image, plan)
            total = sum(count for _, count, _ in plan)
            log("tcprog: %d of %d logical sector(s) unchanged; erasing and programming %d"
                % (len(unchanged), total, total - len(unchanged)), verbose=True)
            changed_plan, changed = [], []
            for first, count, size in plan:
                sectors = range(first, first + count * size, size)
                runs = _merge_runs([(lo, lo + size) for lo in sectors if lo not in unchanged])
                flash.unchanged += _merge_runs([(lo, lo + size) for lo in sectors
                                                if lo in unchanged])
                changed_plan += [(lo, (hi - lo) // size, size) for lo, hi in runs]
                changed += runs
            plan = changed_plan
            pieces = [(max(addr, lo), data[max(addr, lo) - addr:min(addr + len(data), hi) - addr])
                      for addr, data in pieces for lo, hi in changed
                      if max(addr, lo) < min(addr + len(data), hi)]
        flash.clear_status()
        flash.clear_errors()
        flash.erase_runs(plan)
        done = 0
        log("tcprog: programming %d bytes in %d segment(s)"
            % (sum(len(data) for _, data in pieces), len(pieces)), verbose=True)
        with Progress("programmed", max(1, sum(len(data) for _, data in pieces)), " bytes",
                      (args.progress or args.progress_interval) and not args.quiet,
                      args.progress_interval) as programming:
            for addr, data in pieces:

                def on_progress(written):
                    programming.update(done + written)
                flash.program(addr, data, on_progress)
                done += len(data)
        flash.reset_to_read()
    except (DapError, KeyboardInterrupt) as error:
        raise flash.stopped(error, "write")

    if not args.no_verify:
        log("tcprog: verifying %d bytes" % image.total_bytes, verbose=True)
        mismatches = []
        for segment in image.segments:
            back = _read_region(dap, segment.addr, len(segment.data))
            diffs = [i for i in range(len(back)) if back[i] != segment.data[i]]
            if diffs:
                first, last = diffs[0], diffs[-1]
                mismatches.append("%d byte(s) differ in 0x%08X..0x%08X (first at 0x%08X: "
                                  "wrote 0x%02X, read 0x%02X)"
                                  % (len(diffs), segment.addr + first, segment.addr + last + 1,
                                     segment.addr + first, segment.data[first], back[first]))
        if mismatches:
            raise Fail("verify mismatch: %s" % "; ".join(mismatches), EX_VERIFY)

    log("tcprog: wrote %d bytes, %s" % (image.total_bytes,
                                       "not verified" if args.no_verify else "verified"),
        verbose=True)
    return flash


def read_words(dap, addr, size):
    """Read [addr, addr+size) one word at a time, reconnecting on a miss. -> bytes.
    The DAP aligns a word read down to the word boundary, so read the aligned
    words covering the range and slice; an unaligned addr is otherwise shifted."""
    start = addr & ~3
    end = (addr + size + 3) & ~3
    out = bytearray()
    for word in range(start, end, 4):
        value = dap.read_word_retry(word)
        if value is None:
            raise ReadFailed(word)
        out += value.to_bytes(4, "little")
    return bytes(out[addr - start:addr - start + size])
