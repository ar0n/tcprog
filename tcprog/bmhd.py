# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Boot Mode Headers (BMHD) of TC2xx, TC3xx and TC4xx: parse, validate, CRC."""
import zlib

BMHDID = 0xB359
HWCFG_FLASH, HWCFG_ABM, HWCFG_GEN_BSL, HWCFG_ASC_BSL = 0x7, 0x6, 0x4, 0x3
VALID_HWCFG = {HWCFG_FLASH, HWCFG_ABM, HWCFG_GEN_BSL, HWCFG_ASC_BSL}


def crc32_bmhd(header):
    """CRC-32/ISO-HDLC (zlib.crc32) over whole 32-bit words, each byte-swapped
    (fed big-endian): 8 bytes on TC2xx/TC3xx, 12 on TC4xx."""
    if len(header) < 8 or len(header) % 4:
        raise ValueError("need 8 or more header bytes, whole words")
    swapped = b"".join(bytes(header[i:i + 4][::-1]) for i in range(0, len(header), 4))
    return zlib.crc32(swapped) & 0xFFFFFFFF


def _check_bmhdid(header, problems):
    if header["bmhdid"] != BMHDID:
        problems.append("BMHDID 0x%04X != 0xB359" % header["bmhdid"])


def _check_stad(stad, label, chip, problems):
    """STAD word-aligned and in the chip's PFLASH."""
    if stad & 0x3:
        problems.append("%s 0x%08X not word-aligned" % (label, stad))
    if not any(rng.contains(stad) for rng in chip.ranges("pflash")):
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


def validate_bmhd_tc3(data, chip):
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
# Layout differs from TC3xx: STADABM comes first, then BMI|BMHDID in word 1, a
# CRC-checked code range (ChkStart..ChkEnd + CRCrange/!CRCrange), and finally
# CRChead/!CRChead over the first 24 bytes. The BMI has PINDIS at bit 3 and HWCFG
# at bits 6:4 (TC3xx: bit 0 and bits 3:1).
def parse_bmhd_tc2(data):
    if len(data) < 32:
        raise ValueError("TC2xx BMHD is 32 bytes")
    word1 = int.from_bytes(data[4:8], "little")   # BMHDID<<16 | BMI
    bmi = word1 & 0xFFFF
    return {"stad": int.from_bytes(data[0:4], "little"),      # STADABM
            "bmi": bmi, "pindis": (bmi >> 3) & 1, "hwcfg": (bmi >> 4) & 0x7,
            "bmhdid": (word1 >> 16) & 0xFFFF,
            "chkstart": int.from_bytes(data[8:12], "little"),
            "chkend": int.from_bytes(data[12:16], "little"),
            "crcrange": int.from_bytes(data[16:20], "little"),
            "crcrange_n": int.from_bytes(data[20:24], "little"),
            "crchead": int.from_bytes(data[24:28], "little"),
            "crchead_n": int.from_bytes(data[28:32], "little")}


def validate_bmhd_tc2(data, chip):
    if len(data) < 32:
        return False, ["shorter than 32 bytes"]
    header = parse_bmhd_tc2(data)
    problems = []
    _check_bmhdid(header, problems)
    # STADABM is the start address of the Alternate Boot Mode only; an internal
    # start begins at 0xA0000020 whatever STADABM holds.
    if header["hwcfg"] == HWCFG_ABM:
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
# (bit 0 = 1, MODE 3:1, 15:4 = 0) is reported raw, not decoded.
def parse_bmhd_tc4(data):
    if len(data) < 16:
        raise ValueError("TC4xx BMHD is 16 bytes")
    return {"sal": int.from_bytes(data[0:4], "little"),
            "bmi": int.from_bytes(data[4:6], "little"),
            "bmhdid": int.from_bytes(data[6:8], "little"),
            "stad": int.from_bytes(data[8:12], "little"),
            "crcbmhd": int.from_bytes(data[12:16], "little")}


def validate_bmhd_tc4(data, addr, chip):
    if len(data) < 16:
        return False, ["shorter than 16 bytes"]
    header = parse_bmhd_tc4(data)
    problems = []
    _check_bmhdid(header, problems)
    if header["sal"] != addr:
        problems.append("SAL 0x%08X != header address 0x%08X" % (header["sal"], addr))
    _check_stad(header["stad"], "STAD", chip, problems)
    _check_crc(header, "crcbmhd", data[0:12], problems)
    return (not problems), problems
