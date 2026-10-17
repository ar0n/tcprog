# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""DAP transport (FTDI-MPSSE). Protocol ported from the proven dap_read/dap_proto,
which reverse-engineered the telegram layout, CRC6 and command ids from captured
miniWiggler USB traffic; the IOClient response framing follows Infineon's public
C166S On-Chip Debug Support user manual (V1.1, 2001). ftdi1 (libftdi1) is imported
lazily so list/chips/dry-run need no USB.
"""
DEFAULT_VID, DEFAULT_PID = 0x058B, 0x0043
_CMD_W, _CMD_R, _CMD_FLUSH = 0x19, 0x28, 0x87
_ADBUS_VAL, _ADBUS_DIR, _ACBUS_DIR = 0x80, 0xDB, 0xF7
# ACBUS1 is nSRST (active low): IDLE keeps it released, IDLE_RESET asserts it.
_ACBUS_IDLE, _ACBUS_IDLE_RESET, _ACBUS_TX, _ACBUS_RX = 0x57, 0x55, 0x67, 0x57
CMD_WRITEREG, CMD_WRITEMEM, CMD_READMEM, CMD_DAPISC, CMD_READREG, CMD_SET_IO_CLIENT = 8, 9, 10, 17, 26, 28
REG_READ32 = 0x5
SCU_CHIPID = 0xF0036140          # target identity register (TC2xx/TC3xx)
CBS_JTAGID_TC4 = 0xFA180080      # TC4xx has no SCU_CHIPID; identity is the JTAG ID
# Block read: the READMEM low byte is (size & 0x3FC) | 1, so a 1024-byte read is
# 0x400 & 0x3FC = 0 | 1 = 1 (the "IOADDR" selector); its reply is 1228 bytes.
_REG_BLOCK, _RESP_BLOCK = 0x1, 1228


class DapError(RuntimeError):
    pass


def _locked(chipid):
    """True if SCU_CHIPID could not be read (debug interface closed)."""
    return chipid is None or chipid in (0, 0xFFFFFFFF)


def _crc6(bits):
    c = 0x3F
    for bit in bits:
        b = (bit ^ c ^ (c >> 1)) & 1
        c = ((b << 5) | (c >> 1)) & 0x3F
    return (c ^ (c >> 1)) & 0x3F


def _telegram(cmd, arg, arglen):
    body = [1]
    for n, width in ((cmd, 5), (arglen, 6), (arg, arglen)):
        body += [(n >> k) & 1 for k in range(width)]
    bits = body + [(_crc6(body) >> k) & 1 for k in range(6)]
    if bits[-1] == 1:
        bits.append(0)
    bits = [0] * ((-len(bits)) % 8) + bits
    return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))


def _readmem(addr, size_or_reg):
    return _telegram(CMD_READMEM, ((addr & 0xFFFFFFFF) << 8) | (size_or_reg & 0xFF), 40)


def _writemem_word(addr, value):
    """A 4-byte write: the WRITEMEM header telegram + its 40-bit payload telegram.

    Bit 0 of the low byte is clear (that is what makes it a write). Payload is
    `01` + 32 data bits LSB-first + six zero bits. From dap_proto.writemem_word.
    """
    head = _telegram(CMD_WRITEMEM, ((addr & 0xFFFFFFFC) << 8) | 0x04, 40)
    bits = [0, 1] + [(value >> k) & 1 for k in range(32)] + [0] * 6
    payload = bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(len(bits) // 8))
    return head, payload


def _read_rwdata(reply, count):
    bits = [(b >> k) & 1 for b in reply for k in range(8)]
    off, out = 0, bytearray()
    for _ in range(count):
        while off < len(bits) and not bits[off]:
            off += 1
        if off >= len(bits):
            return None
        off += 1
        if off + 32 > len(bits):
            return None
        out += sum(bits[off + k] << k for k in range(32)).to_bytes(4, "little")
        off += 32
    return bytes(out)


_CONNECT = (
    (_telegram(CMD_SET_IO_CLIENT, 1, 3), 3),
    (_telegram(CMD_WRITEREG, 0xC10, 16), 7),
    (_readmem(0xF0036140, REG_READ32), 16),
    (_telegram(CMD_SET_IO_CLIENT, 1, 3), 3),
    (_telegram(CMD_WRITEREG, 0xC10, 16), 7),
    (_readmem(0xAF400A00, 0x0D) + bytes(23), 26),
    (_telegram(CMD_DAPISC, 0xF00, 16), 5),
    (_telegram(CMD_DAPISC, 0x4ABBAF530400, 48), 5),
    (_telegram(CMD_SET_IO_CLIENT, 1, 3), 3),
    (_telegram(CMD_READREG, 0x4B, 7), 9),
)
_IO_SELECT = (
    (_telegram(CMD_SET_IO_CLIENT, 1, 3), 3),
    (_telegram(CMD_WRITEREG, 0xC10, 16), 7),
)


# --- debug unlock (Cerberus key exchange) --------------------------------
# The startup software offers a debug-password exchange at every reset. Writing
# CMD_KEY_EXCHANGE into the COMDATA mailbox opens it; eight further words are the
# 256-bit password. Acceptance is proven by a memory read succeeding afterwards,
# not by any status word (a wrong password also has all eight words taken). The
# window is short and entry is stochastic, so callers retry. See
# ../bootrom/NOTES-cerberus-unlock.md.
CMD_KEY_EXCHANGE = 0x76D6E24A
REG_COMDATA = 0x4
IOINFO_DIALOGUE = 0x00380080          # startup software listening for the key
_DAPISC16 = _telegram(CMD_DAPISC, 0x0F00, 16)
_DAPISC48 = _telegram(CMD_DAPISC, 0x4ABBAF530400, 48)
_OJCONF_503 = _telegram(CMD_WRITEREG, 0x503E, 20)
_SEL = _telegram(CMD_SET_IO_CLIENT, 1, 3)
_POLL = _telegram(CMD_READREG, 0x4B, 7)   # IOINFO


def _comdata_write(value):
    """WRITEREG telegram: argument is (value << 4) | register."""
    return _telegram(CMD_WRITEREG, ((value & 0xFFFFFFFF) << 4) | REG_COMDATA, 36)


def parse_password(text):
    """'V' (repeated 8x) or 8 comma-separated values -> list of 8 ints."""
    parts = [part.strip() for part in str(text).split(",") if part.strip()]
    if len(parts) == 1:
        return [int(parts[0], 0)] * 8
    if len(parts) != 8:
        raise ValueError("password: give 1 value (repeated 8x) or 8 comma-separated")
    return [int(part, 0) for part in parts]


def _unlock_failed_msg(dap, tries):
    """A diagnostic (no `tcprog:` prefix) that separates a bad password from a
    bad boot mode / timing."""
    reached = getattr(dap, "reached_count", 0)
    if reached:
        return ("unlock failed after %d attempts - reached the SSW dialogue %d time(s) "
                "but the password was refused; check the password." % (tries, reached))
    return ("unlock failed after %d attempts - never reached the SSW dialogue; the part "
            "is likely strapped to BSL (DAP unlock needs flash boot)." % tries)


def parse_probe(text):
    """'VID:PID', 'VID:PID:SERIAL' or 'SERIAL' -> (vid, pid, serial)."""
    if not text:
        return DEFAULT_VID, DEFAULT_PID, None
    parts = text.split(":")
    if len(parts) == 1:
        return DEFAULT_VID, DEFAULT_PID, parts[0] or None
    vid = int(parts[0], 16)
    pid = int(parts[1], 16)
    serial = parts[2] if len(parts) > 2 and parts[2] else None
    return vid, pid, serial


class Dap:
    def __init__(self, serial=None, vid=DEFAULT_VID, pid=DEFAULT_PID, hz=1_000_000,
                 reset_on_open=False):
        """Open the probe. The target keeps running unless reset_on_open, which
        holds nSRST while the link is set up; the first transfer releases it."""
        import ftdi1 as ftdi
        self._ftdi = ftdi
        self.ctx = ftdi.new()
        if self.ctx is None:
            raise DapError("ftdi_new failed")
        if ftdi.set_interface(self.ctx, ftdi.INTERFACE_A) < 0:
            raise DapError("set_interface failed")
        rc = ftdi.usb_open_desc(self.ctx, vid, pid, None, serial)
        if rc < 0:
            raise DapError("cannot open %04x:%04x serial=%r: rc %d (%s)"
                           % (vid, pid, serial, rc, ftdi.get_error_string(self.ctx)))
        for fn, a in ((ftdi.usb_reset, ()), (ftdi.set_latency_timer, (1,)),
                      (ftdi.set_bitmode, (0, ftdi.BITMODE_RESET)),
                      (ftdi.set_bitmode, (0, ftdi.BITMODE_MPSSE))):
            if fn(self.ctx, *a) < 0:
                raise DapError("ftdi setup failed")
        self._flush()
        div = max(0, int(round(60e6 / (2 * hz))) - 1)
        self._w(bytes((0x8A, 0x97, 0x8D)) + bytes((0x86, div & 0xFF, (div >> 8) & 0xFF))
                + bytes((0x80, _ADBUS_VAL, _ADBUS_DIR))
                + bytes((0x82, _ACBUS_IDLE_RESET if reset_on_open else _ACBUS_IDLE,
                         _ACBUS_DIR)))
        self.hz = 60e6 / (2 * (div + 1))

    def _flush(self):
        ftdi = self._ftdi
        (ftdi.tcioflush if hasattr(ftdi, "tcioflush") else ftdi.usb_purge_buffers)(self.ctx)

    def _w(self, data):
        if self._ftdi.write_data(self.ctx, bytes(data)) != len(data):
            raise DapError("short write")

    def _r(self, n, timeout=2.0):
        import time
        out, deadline = bytearray(), time.time() + timeout
        while len(out) < n:
            rc, buf = self._ftdi.read_data(self.ctx, n - len(out))
            if rc < 0:
                raise DapError("read_data rc %d" % rc)
            out += bytes(buf[:rc])
            if rc == 0 and time.time() > deadline:
                raise DapError("timeout: %d/%d bytes" % (len(out), n))
        return bytes(out)

    @staticmethod
    def _frame(tx, nrx):
        cmd = bytearray((0x82, _ACBUS_TX, _ACBUS_DIR))
        if tx:
            cmd += bytes((_CMD_W, (len(tx) - 1) & 0xFF, ((len(tx) - 1) >> 8) & 0xFF)) + tx
        cmd += bytes((0x82, _ACBUS_RX, _ACBUS_DIR))
        if nrx:
            cmd += bytes((_CMD_R, (nrx - 1) & 0xFF, ((nrx - 1) >> 8) & 0xFF))
        return cmd

    def exchange(self, tx, nrx):
        self._w(self._frame(tx, nrx) + bytes((_CMD_FLUSH,)))
        return self._r(nrx) if nrx else b""

    def connect(self):
        seen = [self.exchange(tx, nrx) for tx, nrx in _CONNECT]
        return any(seen[7])

    def read_word(self, addr):
        for tx, nrx in _IO_SELECT:
            self.exchange(tx, nrx)
        resp = self.exchange(_readmem(addr, REG_READ32), 16)
        data = _read_rwdata(resp, 1)
        if data is None:
            raise DapError("no response for 0x%08X" % addr)
        return int.from_bytes(data, "little")

    def try_read_word(self, addr):
        try:
            return self.read_word(addr)
        except DapError:
            return None

    def read_word_retry(self, addr, tries=2):
        """try_read_word, re-sending the connect handshake before each retry: on a
        TC4D7 a failed read leaves later reads unanswered until it is re-sent."""
        for attempt in range(tries):
            if attempt:
                self.connect()
            value = self.try_read_word(addr)
            if value is not None:
                return value
        return None

    def write_word(self, addr, value):
        """Write one 32-bit word (WRITEMEM). Returns the two telegram replies."""
        for tx, nrx in _IO_SELECT:
            self.exchange(tx, nrx)
        head, payload = _writemem_word(addr, value)
        return self.exchange(head, 7), self.exchange(payload, 2)

    def pulse_reset(self, hold=0.010, settle=0.0005):
        """Assert then release the target reset (nSRST).

        On the miniWiggler V3, nSRST is ACBUS1, active low: 0x55 asserts, 0x57
        releases. The dialogue has to be entered within a couple of ms of the
        release, so the caller batches the key exchange right after this.
        """
        import time
        self._w(bytes((0x82, _ACBUS_RX & ~0x02, _ACBUS_DIR)))
        time.sleep(hold)
        self._w(bytes((0x82, _ACBUS_RX, _ACBUS_DIR)))
        time.sleep(settle)

    def _batched(self, steps):
        """Run (telegram, nrx) steps in ONE USB transfer; split the replies.

        The key exchange must ride in the same transfer as the OJCONF write with
        no poll between them, or the startup software finishes its COMDATA check
        and moves on before the words arrive.
        """
        blk = bytearray()
        for tx, n in steps:
            blk += self._frame(tx, n)
        blk += bytes((_CMD_FLUSH,))
        self._w(blk)
        raw = self._r(sum(n for _, n in steps))
        out, off = [], 0
        for _, n in steps:
            out.append(raw[off:off + n])
            off += n
        return out

    def _unlock_attempt(self, words, hold=0.010, settle=0.0):
        """One unlock attempt. -> (unlocked, chipid). Sets self.reached_dialogue.

        The SSW's COMDATA dialogue has to be entered within a couple of ms of
        the reset release, so the reset uses a short settle and the whole key
        exchange rides in one USB transfer (no round trip between the OJCONF
        write and the key word). Timing matches dap_unlock.attempt_until_evaluated.
        """
        self.pulse_reset(hold, settle)
        steps = [(_DAPISC16, 5), (_DAPISC48, 5), (_SEL, 3), (_POLL, 9),
                 (_OJCONF_503, 7), (_comdata_write(CMD_KEY_EXCHANGE), 7)]
        steps += [(_POLL, 9)] * 10
        out = self._batched(steps)

        def io(reply):
            v = _read_rwdata(reply, 1)
            return int.from_bytes(v, "little") if v else None

        self.reached_dialogue = any(io(x) == IOINFO_DIALOGUE for x in out[6:])
        if not self.reached_dialogue:
            return False, None                      # never reached the window
        for w in words:
            self._batched([(_POLL, 9), (_comdata_write(w), 7)])
        self._batched([(_SEL, 3), (_POLL, 9)])
        chipid = self.try_read_word(0xF0036140)
        return (chipid not in (None, 0, 0xFFFFFFFF)), chipid

    def unlock(self, words, tries=10, settle=0.0, on_attempt=None):
        """Retry the key exchange until it unlocks. -> (unlocked, chipid, tries).

        Reaching the window is stochastic and a correct password unlocks only a
        fraction of the attempts that reach it, so several tries are needed
        before a candidate can be called wrong. `on_attempt(k, reached, error)`
        is called once per try (error is a string on a transport failure, else None).
        """
        self.reached_count = 0
        for k in range(1, tries + 1):
            try:
                ok, chipid = self._unlock_attempt(words, settle=settle)
            except DapError as error:
                if on_attempt:
                    on_attempt(k, False, str(error))
                continue
            reached = getattr(self, "reached_dialogue", False)
            if reached:
                self.reached_count += 1
            if on_attempt:
                on_attempt(k, reached, None)
            if ok:
                return True, chipid, k
        return False, None, tries

    BLOCK = 1024

    def read_block(self, addr):
        """1024 bytes from a 1 KiB-aligned address."""
        if addr & (self.BLOCK - 1):
            raise DapError("block read must be 1KiB-aligned: 0x%08X" % addr)
        for tx, nrx in _IO_SELECT:
            self.exchange(tx, nrx)
        resp = self.exchange(_readmem(addr, _REG_BLOCK), _RESP_BLOCK)
        data = _read_rwdata(resp, self.BLOCK // 4)
        if data is None or not any(resp):
            raise DapError("no response for block 0x%08X" % addr)
        return data

    def read_blocks(self, addrs):
        """Several 1 KiB blocks in one USB transfer (front-selects per block)."""
        steps = []
        for a in addrs:
            if a & (self.BLOCK - 1):
                raise DapError("block read must be 1KiB-aligned: 0x%08X" % a)
            steps += list(_IO_SELECT)
            steps.append((_readmem(a, _REG_BLOCK), _RESP_BLOCK))
        out = self._batched(steps)
        data = bytearray()
        for reply, (_, n) in zip(out, steps):
            if n != _RESP_BLOCK:
                continue
            d = _read_rwdata(reply, self.BLOCK // 4)
            if d is None or not any(reply):
                raise DapError("no response in batched block read")
            data += d
        return bytes(data)

    def close(self):
        if self.ctx is not None:
            self._ftdi.set_bitmode(self.ctx, 0, self._ftdi.BITMODE_RESET)
            self._ftdi.usb_close(self.ctx)
            self._ftdi.free(self.ctx)
            self.ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
