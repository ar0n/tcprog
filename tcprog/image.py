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


class ImageSegment:
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
    """Load a firmware image -> ImageSegment. bincopy decodes the text formats (SREC /
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
    return ImageSegment(segments, binfile.execution_start_address, loader="bincopy")


def emit_srec(image, execution_start_address=None):
    binfile = bincopy.BinFile()
    for segment in image.segments:
        binfile.add_binary(bytes(segment.data), address=segment.addr)
    start = execution_start_address if execution_start_address is not None else image.start
    if start is not None:
        binfile.execution_start_address = start
    return binfile.as_srec()


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


def _emit(data, addr, fmt):
    """Serialise a read-back region to the chosen container. -> bytes."""
    if fmt == "bin":
        return bytes(data)
    binfile = bincopy.BinFile()
    binfile.add_binary(bytes(data), address=addr)
    if fmt == "srec":
        return binfile.as_srec().encode()
    if fmt == "hex":
        return binfile.as_ihex().encode()
    raise ValueError("bad output format %r" % fmt)


def _hexdump(data, base, out=sys.stdout):
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexs = " ".join("%02x" % b for b in row)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.write("%08x  %-47s  %s\n" % (base + off, hexs, text))


# ======================================================================
# BMHD (Boot Mode Header): parse / validate / build
# CRC = CRC-32/ISO-HDLC (zlib.crc32) over the 8 header bytes, each 32-bit
# word byte-swapped. Verified against a real BMHD (0x809FE5E2 / ~ = 0x7F601A1D).
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


def parse_bmhd(data):
    if len(data) < 16:
        raise ValueError("BMHD is 16 bytes")
    bmi = int.from_bytes(data[0:2], "little")
    return {"bmi": bmi, "pindis": bmi & 1, "hwcfg": (bmi >> 1) & 0x7,
            "bmhdid": int.from_bytes(data[2:4], "little"),
            "stad": int.from_bytes(data[4:8], "little"),
            "crcbmhd": int.from_bytes(data[8:12], "little"),
            "crcbmhd_n": int.from_bytes(data[12:16], "little")}


def validate_bmhd(data, chip=None):
    problems = []
    if len(data) < 16:
        return False, ["shorter than 16 bytes"]
    header = parse_bmhd(data)
    if header["bmhdid"] != BMHDID:
        problems.append("BMHDID 0x%04X != 0xB359" % header["bmhdid"])
    if header["hwcfg"] not in VALID_HWCFG:
        problems.append("HWCFG 0x%X not in {7,6,4,3}" % header["hwcfg"])
    if header["stad"] & 0x3:
        problems.append("STAD 0x%08X not word-aligned" % header["stad"])
    in_pflash = ((header["stad"] & 0xDF000000) == 0x80000000)
    if chip is not None:
        in_pflash = any(sl.contains(header["stad"]) for sl in chip.slices("pflash"))
    if not in_pflash:
        problems.append("STAD 0x%08X not in program flash" % header["stad"])
    expected = crc32_bmhd(data[0:8])
    if header["crcbmhd"] != expected:
        problems.append("CRCBMHD 0x%08X != 0x%08X" % (header["crcbmhd"], expected))
    if header["crcbmhd_n"] != (~expected & 0xFFFFFFFF):
        problems.append("CRCBMHD_N invalid")
    return (not problems), problems


# TC4xx BMHD: SAL (the header's own address) | BMI | BMHDID | STAD | CRC, no
# inverted CRC; the CRC also covers SAL. From iLLD Ifx_Ssw.h (TC4Dx), verified
# against its four sample headers and a TC4D7 lite kit. The BMI bit meanings are
# not in the iLLD, so the BMI is reported raw.
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
    if header["bmhdid"] != BMHDID:
        problems.append("BMHDID 0x%04X != 0xB359" % header["bmhdid"])
    if header["sal"] != addr:
        problems.append("SAL 0x%08X != header address 0x%08X" % (header["sal"], addr))
    if header["stad"] & 0x3:
        problems.append("STAD 0x%08X not word-aligned" % header["stad"])
    if chip is not None and not any(sl.contains(header["stad"]) for sl in chip.slices("pflash")):
        problems.append("STAD 0x%08X not in program flash" % header["stad"])
    expected = crc32_bmhd(data[0:12])
    if header["crcbmhd"] != expected:
        problems.append("CRCBMHD 0x%08X != 0x%08X" % (header["crcbmhd"], expected))
    return (not problems), problems


def build_bmhd(stad, hwcfg=HWCFG_FLASH, pindis=0, bmi_high=0, bmi=None):
    if bmi is None:
        bmi = (pindis & 1) | ((hwcfg & 0x7) << 1) | ((bmi_high & 0xFFF) << 4)
    header = (bmi.to_bytes(2, "little") + BMHDID.to_bytes(2, "little")
              + (stad & 0xFFFFFFFF).to_bytes(4, "little"))
    crc = crc32_bmhd(header)
    return header + crc.to_bytes(4, "little") + (~crc & 0xFFFFFFFF).to_bytes(4, "little")
