# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Firmware images (bincopy-backed) and the Boot Mode Header (BMHD)."""
import os
import sys
import zlib

import bincopy


# ======================================================================
# firmware images
# ======================================================================
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
    __slots__ = ("segments", "start", "loader")

    def __init__(self, segments, start=None, loader=""):
        self.segments = segments
        self.start = start
        self.loader = loader

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
    binfile = bincopy.BinFile()
    try:
        if fmt == "bin":
            if address is None:
                raise ValueError("raw .bin needs an address (use --address)")
            binfile.add_binary_file(path, address=address)
        elif fmt == "elf":
            binfile.add_elf_file(path)
        else:
            binfile.add_file(path)        # auto-detect SREC / Intel HEX / TI-TXT
    except bincopy.Error as error:
        raise ValueError("bad image %s: %s" % (path, error))
    segments = [FirmwareSegment(segment.address, segment.data) for segment in binfile.segments]
    return FirmwareImage(segments, binfile.execution_start_address, loader="bincopy")


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


def flat_binary(segments, start, end, fill=0):
    """[(addr, bytes)] as one flat image of [start, end), the bytes no segment
    covers set to `fill`. -> (bytes, [(lo, hi)] filled spans)"""
    out = bytearray([fill]) * (end - start)
    gaps, pos = [], start
    for addr, data in sorted(segments):
        out[addr - start:addr - start + len(data)] = data
        if addr > pos:
            gaps.append((pos, addr))
        pos = max(pos, addr + len(data))
    if pos < end:
        gaps.append((pos, end))
    return bytes(out), gaps


def _hexdump(data, base, out=sys.stdout):
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexs = " ".join("%02x" % b for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.write("%08x  %-47s  %s\n" % (base + off, hexs, text))


# ======================================================================
# BMHD (Boot Mode Header): parse / validate / build
# CRC = CRC-32/ISO-HDLC (zlib.crc32) over the 8 header bytes, each 32-bit
# word byte-swapped.
# ======================================================================
BMHDID = 0xB359
HWCFG_FLASH, HWCFG_ABM, HWCFG_GEN_BSL, HWCFG_ASC_BSL = 0x7, 0x6, 0x4, 0x3
VALID_HWCFG = {HWCFG_FLASH, HWCFG_ABM, HWCFG_GEN_BSL, HWCFG_ASC_BSL}


def crc32_bmhd(header):
    """CRC over whole 32-bit words, each byte-swapped (fed big-endian): 8 bytes on
    TC2xx/TC3xx, 12 on TC4xx."""
    if len(header) < 8 or len(header) % 4:
        raise ValueError("need 8 or more header bytes, whole words")
    swapped = b"".join(bytes(header[i:i + 4][::-1]) for i in range(0, len(header), 4))
    return zlib.crc32(swapped) & 0xFFFFFFFF


def _check_bmhdid(header, problems):
    if header["bmhdid"] != BMHDID:
        problems.append("BMHDID 0x%04X != 0xB359" % header["bmhdid"])


def _check_stad(stad, label, chip, problems, unknown_chip_pflash=True):
    """STAD word-aligned and in the chip's PFLASH. Without a chip, any address
    in the 0x80000000/0xA0000000 PFLASH segments counts, or (with
    unknown_chip_pflash False) the PFLASH check is skipped."""
    if stad & 0x3:
        problems.append("%s 0x%08X not word-aligned" % (label, stad))
    if chip is not None:
        in_pflash = any(rng.contains(stad) for rng in chip.ranges("pflash"))
    elif unknown_chip_pflash:
        in_pflash = (stad & 0xDF000000) == 0x80000000
    else:
        in_pflash = True
    if not in_pflash:
        problems.append("%s 0x%08X not in program flash" % (label, stad))


def _check_crc(header, key, covered, problems):
    """The CRC field `key` over the `covered` header bytes, and its inverted
    copy `key`_n where the layout has one."""
    expected = crc32_bmhd(covered)
    if header[key] != expected:
        problems.append("%s 0x%08X != 0x%08X" % (key.upper(), header[key], expected))
    if key + "_n" in header and header[key + "_n"] != (~expected & 0xFFFFFFFF):
        problems.append("%s_N invalid" % key.upper())


# The three BMHD layouts differ; every parser reports the start address as "stad".
def parse_bmhd_tc3(data):
    if len(data) < 16:
        raise ValueError("BMHD is 16 bytes")
    bmi = int.from_bytes(data[0:2], "little")
    return {"bmi": bmi, "pindis": bmi & 1, "hwcfg": (bmi >> 1) & 0x7,
            "bmhdid": int.from_bytes(data[2:4], "little"),
            "stad": int.from_bytes(data[4:8], "little"),
            "crcbmhd": int.from_bytes(data[8:12], "little"),
            "crcbmhd_n": int.from_bytes(data[12:16], "little")}


def validate_bmhd_tc3(data, chip=None):
    problems = []
    if len(data) < 16:
        return False, ["shorter than 16 bytes"]
    header = parse_bmhd_tc3(data)
    _check_bmhdid(header, problems)
    if header["hwcfg"] not in VALID_HWCFG:
        problems.append("HWCFG 0x%X not in {7,6,4,3}" % header["hwcfg"])
    if header["bmi"] & 0xF000:
        problems.append("BMI reserved bits 15:12 not zero (0x%04X)" % header["bmi"])
    _check_stad(header["stad"], "STAD", chip, problems)
    _check_crc(header, "crcbmhd", data[0:8], problems)
    return (not problems), problems


# TC2xx (AURIX 1G) Boot Mode Header, 32 bytes; the four BMHDs sit in PFLASH.
# Layout differs from TC3xx: STADABM comes
# first, then BMI|BMHDID in word 1, a CRC-checked code range (ChkStart..ChkEnd +
# CRCrange/!CRCrange), and finally CRChead/!CRChead over the first 24 bytes.
# The TC2xx BMI layout (PINDIS bit 3, HWCFG 6:4) is not the TC3xx one, so BMI is reported raw.
def parse_bmhd_tc2(data):
    if len(data) < 32:
        raise ValueError("TC2xx BMHD is 32 bytes")
    word1 = int.from_bytes(data[4:8], "little")   # BMHDID<<16 | BMI
    return {"stad": int.from_bytes(data[0:4], "little"),      # STADABM
            "bmi": word1 & 0xFFFF,
            "bmhdid": (word1 >> 16) & 0xFFFF,
            "chkstart": int.from_bytes(data[8:12], "little"),
            "chkend": int.from_bytes(data[12:16], "little"),
            "crcrange": int.from_bytes(data[16:20], "little"),
            "crcrange_n": int.from_bytes(data[20:24], "little"),
            "crchead": int.from_bytes(data[24:28], "little"),
            "crchead_n": int.from_bytes(data[28:32], "little")}


def validate_bmhd_tc2(data, chip=None):
    if len(data) < 32:
        return False, ["shorter than 32 bytes"]
    header = parse_bmhd_tc2(data)
    problems = []
    _check_bmhdid(header, problems)
    _check_stad(header["stad"], "STADABM", chip, problems)
    # CRChead covers STADABM..!CRCrange (the first 24 bytes), so it already
    # authenticates the CRCrange/!CRCrange fields; no separate inverted-pair check
    # is needed (both are left 0 when the range check is unused).
    # CRCrange itself covers the user code range, which the header alone cannot
    # recompute.
    _check_crc(header, "crchead", data[0:24], problems)
    return (not problems), problems


# TC4xx BMHD: SAL (the header's own address) | BMI | BMHDID | STAD | CRC, no
# inverted CRC; the CRC covers the first 12 bytes, SAL included. The BMI
# (bit 0 = 1, MODE 3:1, 15:4 = 0) is reported raw for now.
def parse_bmhd_tc4(data):
    if len(data) < 16:
        raise ValueError("TC4xx BMHD is 16 bytes")
    return {"sal": int.from_bytes(data[0:4], "little"),
            "bmi": int.from_bytes(data[4:6], "little"),
            "bmhdid": int.from_bytes(data[6:8], "little"),
            "stad": int.from_bytes(data[8:12], "little"),
            "crcbmhd": int.from_bytes(data[12:16], "little")}


def validate_bmhd_tc4(data, addr, chip=None):
    if len(data) < 16:
        return False, ["shorter than 16 bytes"]
    header = parse_bmhd_tc4(data)
    problems = []
    _check_bmhdid(header, problems)
    if header["sal"] != addr:
        problems.append("SAL 0x%08X != header address 0x%08X" % (header["sal"], addr))
    _check_stad(header["stad"], "STAD", chip, problems, unknown_chip_pflash=False)
    _check_crc(header, "crcbmhd", data[0:12], problems)
    return (not problems), problems
