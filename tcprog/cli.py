# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""tcprog CLI: argparse subcommands over the dap / flash / chips / image layers."""
import argparse
import glob
import json
import os
import sys
import time

import bincopy

from . import __version__ as VERSION
from .chips import all_chips, by_id, id_addrs, load_device_file, lookup_chip
from .dap import (Dap, DapError, SCU_CHIPID, parse_password, parse_probe,
                  _locked, _unlock_failed_msg)
from .flash import (Flash, ReadFailed, _block_span, _flash_sectors,
                    _read_region, confirm_unknown, device_map_problem, driven_layout,
                    drives, erase_plan, _is_pflash, _program,
                    _read_ucbs, _log_sector_size, bmhd_blocks, next_boot_report,
                    blankcheck_geometry, read_region_blankcheck, read_region_sparse,
                    read_words, ucb_name)
from .image import flat_binary, load_image, _hexdump, _out_format
from .util import (EX_CHECK, EX_REFUSED, EX_VERIFY, Fail, Progress, _error, log, set_verbose,
                   warn)

DESCRIPTION = """AURIX TC2xx/TC3xx/TC4xx flash programmer for DAP probes based on FTDI chips
(e.g. miniWiggler).

commands:
  list         list attached probes or supported chips
  ident        connect and report the target (writes nothing)
  read         read flash or memory to a file
  ucb          read, verify or export the User Configuration Blocks
  write        program a firmware image to flash
  erase        erase flash sectors

common options (ident, read, ucb, write, erase):
  target    -c/--chip, --device
  probe     -p/--probe, --speed, --connect-under-reset
  unlock    --debug-unlock-pw, --unlock-tries, --pflash-unlock-pw, --dflash-unlock-pw
  output    -q/--quiet, -v/--verbose"""

EPILOG = """examples:
  tcprog list probes
  tcprog ident -p 058b:0043:<serial>
  tcprog write -c tc375 -p 058b:0043:<serial> app.srec
  tcprog read -c tc375 -p 058b:0043:<serial> --region pflash --skip-blank -o dump.srec

exit codes:
  0 ok   64 usage   65 bad image   69 no probe/target   73 cannot write output
  74 transfer failed   77 debug-locked   80 ident --check mismatch
  81 verify mismatch   82 refused for safety

environment:
  TCPROG_CHIP, TCPROG_PROBE, TCPROG_DEBUG_UNLOCK_PW,
  TCPROG_PFLASH_UNLOCK_PW, TCPROG_DFLASH_UNLOCK_PW

Run `tcprog <command> --help` for a command's options."""

BANNER = "tcprog %s ( https://github.com/ar0n/tcprog )" % VERSION

_AFTER_HELP = ("if CPU0 was halted for the operation: reset restarts the application, "
               "no-reset/halt leave CPU0 halted [default: reset]")


_HELP_COLUMN = 30


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose help lists the command's own options first and the
    options shared by all target commands last, indented under "common options".
    Groups marked `common` are the shared ones; `examples` lines end the help."""
    examples = ()

    def _get_formatter(self):
        return self.formatter_class(prog=self.prog, max_help_position=_HELP_COLUMN)

    def error(self, message):
        """Bad arguments: the usage line, then `tcprog: error: ...` like every other
        error, and EX_USAGE."""
        self.print_usage(sys.stderr)
        self.exit(os.EX_USAGE, "tcprog: error: %s\n" % message)

    def format_help(self):
        common = [group for group in self._action_groups if getattr(group, "common", False)]
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        for group in self._action_groups:
            if group in common:
                continue
            formatter.start_section(group.title)
            formatter.add_text(group.description)
            formatter.add_arguments(group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        formatter._action_max_length = _HELP_COLUMN       # same help column everywhere
        text = formatter.format_help()
        if common:
            nested = self._get_formatter()
            nested._width -= 2
            nested._max_help_position -= 2
            for group in common:
                nested.start_section(group.title)
                nested.add_arguments(group._group_actions)
                nested.end_section()
            nested._action_max_length = _HELP_COLUMN
            body = nested.format_help().strip("\n")
            text += "\ncommon options:\n" + "\n".join(
                "  " + line for line in body.split("\n") if line) + "\n"
        if self.examples:
            text += "\nexamples:\n" + "".join("  %s\n" % line for line in self.examples)
        return BANNER + "\n" + text


def _add_common_args(p):
    """The options shared by ident, read, ucb, write and erase."""
    def group(title):
        g = p.add_argument_group(title)
        g.common = True
        return g

    t = group("target")
    t.add_argument("-c", "--chip", default=os.environ.get("TCPROG_CHIP"),
                   help="target part or family (e.g. tc375, tc37x, tc4d7), or auto to detect it. "
                        "Env: TCPROG_CHIP")
    t.add_argument("--device", metavar="FILE",
                   help="custom device file (TOML, or older JSON); its address ranges "
                        "act as the write allowlist")
    b = group("probe")
    b.add_argument("-p", "--probe", default=os.environ.get("TCPROG_PROBE"),
                   help="FTDI probe as VID:PID or VID:PID:SERIAL (e.g. 058b:0043:<serial>). "
                        "Env: TCPROG_PROBE")
    b.add_argument("--speed", type=int, default=1000, metavar="KHZ",
                   help="DAP clock in kHz [default: 1000]")
    b.add_argument("--connect-under-reset", action="store_true",
                   help="reset the target while attaching")
    u = group("unlock")
    u.add_argument("--debug-unlock-pw", default=os.environ.get("TCPROG_DEBUG_UNLOCK_PW"),
                   metavar="PW",
                   help="256-bit debug password, 8 words (e.g. 1,2,3,4,5,6,7,8). "
                        "Env: TCPROG_DEBUG_UNLOCK_PW")
    u.add_argument("--unlock-tries", type=int, default=3, metavar="N",
                   help="max debug unlock attempts [default: 3]")
    u.add_argument("--pflash-unlock-pw", nargs="?", const="", metavar="PW",
                   default=os.environ.get("TCPROG_PFLASH_UNLOCK_PW"),
                   help="lift PFLASH protection; without a value the debug password is "
                        "used. Env: TCPROG_PFLASH_UNLOCK_PW")
    u.add_argument("--dflash-unlock-pw", nargs="?", const="", metavar="PW",
                   default=os.environ.get("TCPROG_DFLASH_UNLOCK_PW"),
                   help="lift DFLASH protection, as above. Env: TCPROG_DFLASH_UNLOCK_PW")
    o = group("output")
    o.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    o.add_argument("-v", "--verbose", action="store_true", help="more detail")
    return p


def _regions(text):
    """--region value: a comma list of address range names. -> [name]"""
    names = list(dict.fromkeys(name.strip() for name in text.split(",")))
    if "" in names:
        raise argparse.ArgumentTypeError("empty region name in %r" % text)
    return names


def _hexint(s):
    return int(s, 0)


def _log_attempt(k, reached, error):
    """dap.unlock(on_attempt=...) callback: log each try under -v."""
    if error is not None:
        log("  attempt %d: error: %s" % (k, error), verbose=True)
    else:
        log("  attempt %d: %s" % (k, "reached dialogue" if reached else "missed window"),
            verbose=True)


def _decode_chipid(v):
    """SCU_CHIPID. CHTEC (bits 7:6) names the generation: 01 TC2xx, 10 TC3xx.
    The upper fields differ: TC2xx has SEC at bit 30 and an 8-bit CHID, TC3xx
    SEC at bit 31 and VART/CHPK."""
    chtec = (v >> 6) & 3
    common = {"fsize": (v >> 24) & 0xF, "ucode": (v >> 17) & 0x7F,
              "chtec": chtec, "chrev": v & 0x3F}
    if chtec == 1:
        return dict(common, sec=(v >> 30) & 1, sp=(v >> 28) & 3, eea=(v >> 16) & 1,
                    chid=(v >> 8) & 0xFF)
    return dict(common, sec=(v >> 31) & 1, vart=(v >> 28) & 7, eea=(v >> 16) & 1,
                chid=(v >> 12) & 0xF, chpk=(v >> 8) & 0xF)


def _decode_jtagid(v):
    """IEEE 1149.1 ID: version, part number, manufacturer with the fixed lowest
    bit (Infineon = 0x083)."""
    return {"version": (v >> 28) & 0xF, "part": (v >> 12) & 0xFFFF, "manuf": v & 0xFFF}


# ======================================================================
# list
# ======================================================================
def _cmd_list(args):
    if args.what is None:
        args._parser.print_help()
        return os.EX_OK
    if args.what == "chips":
        return _show_chip(args.name) if args.name else _list_chips()
    return _list_probes()


def _list_probes():
    KNOWN = {
        (0x0403, 0x6010): "Generic FT2232 (MPSSE)",
        (0x0403, 0x6011): "Generic FT4232 (MPSSE)",
        (0x0403, 0x6014): "Generic FT232H (MPSSE)",
        (0x058b, 0x0043): "Infineon DAS (miniWiggler / on-board)",
    }
    def read_attr(dev_dir, name):
        try:
            with open(dev_dir + name) as handle:
                return handle.read().strip()
        except OSError:
            return None

    probes = []
    for dev_dir in sorted(glob.glob("/sys/bus/usb/devices/*/")):
        vendor_s, product_s = read_attr(dev_dir, "idVendor"), read_attr(dev_dir, "idProduct")
        if not vendor_s or not product_s:
            continue
        vid, pid = int(vendor_s, 16), int(product_s, 16)
        if (vid, pid) not in KNOWN:
            continue
        probes.append((vid, pid, read_attr(dev_dir, "serial"), read_attr(dev_dir, "product"),
                       KNOWN[(vid, pid)]))

    print("PROBES (use with --probe VID:PID or VID:PID:SERIAL):")
    if not probes:
        print("  none found (looked for FTDI 0403:6010/6011/6014 and Infineon 058b:0043)")
    for vid, pid, serial, product, kind in probes:
        probe_id = "%04x:%04x%s" % (vid, pid, ":" + serial if serial else "")
        print("  %-22s %s" % (probe_id, product or kind))
    return os.EX_OK


def _list_chips():
    print("%-8s %-7s %s" % ("chip", "family", "aliases"))
    for chip in all_chips():
        print("%-8s %-7s %s" % (chip.name, chip.family, ", ".join(chip.aliases)))
    print("\nAdd parts by dropping a TOML file in the devices/ dir of the package, or "
          "pass one with --device.")
    return os.EX_OK


def _show_chip(name):
    """One part's memory map as tcprog uses it, inherited ranges included."""
    chip = lookup_chip(name)
    if chip is None:
        return _error("unknown chip %r (see `tcprog list chips`)" % name, os.EX_USAGE)
    print("%s (%s)%s" % (chip.name, chip.family,
                        "  aliases: " + ", ".join(chip.aliases) if chip.aliases else ""))
    for r in sorted(chip.memory, key=lambda r: r.base):
        flags = [flag for flag in ("write_protected", "erase_protected") if getattr(r, flag)]
        print(("  %-14s %-6s 0x%08X..0x%08X  %8s  %s"
               % (r.name, r.kind, r.base, r.end, _size(r.size), " ".join(flags))).rstrip())
    for code, banks in sorted(chip.pflash_variants.items()):
        print("  FSIZE %X: PFLASH %s" % (code, ", ".join(
            "0x%08X..0x%08X" % (base, base + size) for base, size in banks)))
    return os.EX_OK


# ======================================================================
# argument groups
# ======================================================================
def _build_parser():
    """Top-level parser with one subcommand per action."""
    p = _Parser(
        prog="tcprog", usage="tcprog <command> [options]", description=DESCRIPTION,
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version="tcprog " + VERSION)
    sub = p.add_subparsers(dest="command", parser_class=_Parser, help=argparse.SUPPRESS)

    pl = sub.add_parser("list", usage="tcprog list probes\n       tcprog list chips [NAME]",
                        description="List attached probes, or the parts --chip accepts. "
                                    "With a NAME, show that part's memory map.")
    pl.add_argument("what", nargs="?", choices=("probes", "chips"),
                    help="probes: attached FTDI/DAP probes; chips: supported parts")
    pl.add_argument("name", nargs="?", help="chips: a part name or alias")
    pl.set_defaults(func=_cmd_list, _parser=pl)

    pi = sub.add_parser("ident", usage="tcprog ident [common options] [ident options]",
                        description="Connect and report the target; writes nothing. With an "
                                    "unlock option it also lifts the protection and leaves it "
                                    "lifted until the next reset.")
    g = pi.add_argument_group("ident options")
    g.add_argument("--check", metavar="NAME", help="exit 80 unless the detected chip is NAME")
    g.add_argument("--fields", metavar="LIST", help="comma-separated subset of fields to print")
    g.add_argument("--json", action="store_true", help="result as JSON on stdout")
    _add_common_args(pi)
    pi.examples = (
        'tcprog ident -p 058b:0043:<serial>',
        'tcprog ident -p 058b:0043:<serial> --json',
        'tcprog ident -p 058b:0043:<serial> --check tc375',
)
    pi.set_defaults(func=_cmd_ident)

    pr = sub.add_parser("read", usage="tcprog read [common options] [read options]",
                        description="Read flash or memory to a file, or hexdump it.")
    g = pr.add_argument_group("read options")
    g.add_argument("--addr", type=_hexint, help="start address")
    g.add_argument("--length", type=_hexint, metavar="LEN", help="number of bytes")
    g.add_argument("--region", type=_regions, metavar="NAME[,NAME]",
                   help="read whole address ranges by name (e.g. pflash,dflash0,ucb)")
    g.add_argument("-o", "--output", metavar="FILE",
                   help="output file, bin/srec/hex by extension; omit to hexdump")
    g.add_argument("--format", choices=("srec", "hex", "bin"),
                   help="override the output format")
    g.add_argument("--skip-blank", "--sparse", dest="skip_blank", action="store_true",
                   help="read only the programmed data, skipping blank flash")
    g.add_argument("--skip-erased", dest="skip_blank", action="store_true",
                   help=argparse.SUPPRESS)       # the earlier name
    g.add_argument("--json", action="store_true", help="result summary as JSON on stdout")
    g.add_argument("--progress", action="store_true", help="show a progress line")
    _add_common_args(pr)
    pr.examples = (
        'tcprog read -c tc375 -p 058b:0043:<serial> --addr 0xA0000000 --length 0x100',
        'tcprog read -c tc375 -p 058b:0043:<serial> --addr 0xA0000000 --length 0x10000 -o app.bin',
        'tcprog read -c tc375 -p 058b:0043:<serial> --region pflash,dflash0 --skip-blank -o flash.srec',
)
    pr.set_defaults(func=_cmd_read)

    pucb = sub.add_parser("ucb",
                          usage="tcprog ucb [common options] [decode|verify|export] [ucb options]",
                          description="Read the UCBs: confirmation states, the boot mode "
                                      "headers and, on TC3xx, the protection the next reset "
                                      "installs.")
    pucb.add_argument("action", nargs="?", choices=("decode", "verify", "export"),
                      default="decode",
                      help="decode (default), verify (exit 81 on a problem), or export the "
                           "raw BMHD blocks to -o")
    g = pucb.add_argument_group("ucb options")
    g.add_argument("-o", "--output", metavar="FILE", help="export: output file")
    g.add_argument("--format", choices=("bin", "srec", "hex"),
                   help="export: output format [default: from the -o extension]")
    g.add_argument("--json", action="store_true", help="decode/verify result as JSON on stdout")
    _add_common_args(pucb)
    pucb.examples = (
        'tcprog ucb -c tc375 -p 058b:0043:<serial>',
        'tcprog ucb verify -c tc375 -p 058b:0043:<serial>',
        'tcprog ucb export -c tc375 -p 058b:0043:<serial> -o bmhd.srec',
)
    pucb.set_defaults(func=_cmd_ucb)

    pw = sub.add_parser("write", usage="tcprog write [common options] [write options] FIRMWARE",
                        description="Program a firmware image to flash and verify it. Needs "
                                    "-c/--chip or --device: a write is gated on the target's "
                                    "memory map.")
    pw.add_argument("firmware", nargs="?", metavar="FIRMWARE",
                    help="image to write (.elf, .hex, .srec, .bin)")
    g = pw.add_argument_group("write options")
    g.add_argument("-a", "--address", type=_hexint, metavar="ADDR",
                   help="load address for a raw .bin")
    g.add_argument("--format", choices=("elf", "hex", "srec", "bin"),
                   help="override the image format")
    g.add_argument("--erase", choices=("sectors", "none"), default="sectors",
                   help="erase the covered sectors first [default: sectors]")
    g.add_argument("--no-verify", action="store_true", help="skip the read-back verification")
    g.add_argument("--after", choices=("reset", "no-reset", "halt"), default="reset",
                   help=_AFTER_HELP)
    g.add_argument("--enable-ucb-write", action="store_true", help="allow writing a UCB region")
    g.add_argument("-n", "--dry-run", action="store_true", help="parse and plan, touch nothing")
    g.add_argument("-y", "--yes", action="store_true", help="answer yes to the confirmation prompt")
    g.add_argument("--progress", action="store_true", help="show a progress line")
    _add_common_args(pw)
    pw.examples = (
        'tcprog write -c tc375 -p 058b:0043:<serial> app.srec',
        'tcprog write -n -c tc375 app.srec',
        'tcprog write -c tc4d7 -p 058b:0043:<serial> --pflash-unlock-pw 1,2,3,4,5,6,7,8 app.srec',
)
    pw.set_defaults(func=_cmd_write)

    pe = sub.add_parser("erase", usage="tcprog erase [common options] [erase options]",
                        description="Erase flash sectors (PFLASH 16 KiB, DFLASH 4 KiB on TC3xx "
                                    "/ 2 KiB on TC4xx). Needs -c/--chip or "
                                    "--device.")
    g = pe.add_argument_group("erase options")
    g.add_argument("--addr", type=_hexint, help="start address, rounded down to a sector")
    g.add_argument("--length", type=_hexint, metavar="LEN",
                   help="bytes to erase, rounded up to whole sectors")
    g.add_argument("--region", type=_regions, metavar="NAME[,NAME]",
                   help="erase whole address ranges by name (e.g. pflash,dflash0)")
    g.add_argument("--allow-erase-all", action="store_true", help="allow erasing a whole region")
    g.add_argument("--enable-ucb-write", action="store_true", help="allow erasing a UCB")
    g.add_argument("--after", choices=("reset", "no-reset", "halt"), default="reset",
                   help=_AFTER_HELP)
    g.add_argument("-y", "--yes", action="store_true", help="answer yes to the confirmation prompt")
    _add_common_args(pe)
    pe.examples = (
        'tcprog erase -c tc375 -p 058b:0043:<serial> --addr 0xA0500000 --length 0x4000',
        'tcprog erase -c tc375 -p 058b:0043:<serial> --region pflash --allow-erase-all',
)
    pe.set_defaults(func=_cmd_erase)

    return p


# ======================================================================
# connect / unlock helpers
# ======================================================================
def _resolve_chip(args):
    """The target from --device (a TOML/JSON file) or --chip (a built-in name), or
    None when neither is given or with `-c auto`, so the caller may still detect
    it from the identity register. Raises Fail on a bad file or name."""
    if getattr(args, "device", None):
        try:
            return load_device_file(args.device)
        except (OSError, ValueError, KeyError) as error:   # TOMLDecodeError is a ValueError
            raise Fail("cannot load device file %s: %s" % (args.device, error), os.EX_USAGE)
    if args.chip and args.chip.lower() != "auto":
        chip = lookup_chip(args.chip)
        if chip is None:
            raise Fail("unknown chip %r (see `tcprog list chips`)" % args.chip, os.EX_USAGE)
        return chip
    return None


def _open_dap(args):
    """Open the probe and bring the DAP link up. -> Dap (caller uses `with`).
    Raises Fail on any error, with the right exit code."""
    vid, pid, serial = parse_probe(args.probe)
    try:
        dap = Dap(serial, vid, pid, args.speed * 1000,
                  reset_on_open=args.connect_under_reset)
    except DapError as error:
        raise Fail(str(error), os.EX_UNAVAILABLE)
    except ImportError:
        raise Fail("the DAP needs the 'ftdi1' (libftdi1) Python binding", os.EX_UNAVAILABLE)
    if not dap.connect():
        dap.close()
        raise Fail("target did not answer; check the cable and power, and that the part "
                   "is not strapped to BSL", os.EX_UNAVAILABLE)
    return dap


def _read_identity(dap, chip=None):
    """Read the identity register: the known chip's, else each one in the DB in
    turn (SCU_CHIPID on TC2xx/TC3xx, CBS_JTAGID on TC4xx). -> (addr, value),
    value None if none answered."""
    addrs = [chip.id_addr] if chip is not None and chip.id_addr else id_addrs()
    for addr in addrs:
        value = dap.read_word_retry(addr)
        if not _locked(value):
            return addr, value
    return SCU_CHIPID, None


def _unlock_if_locked(dap, args, chip=None, halt=False, confirm=False, spans=None):
    """Read the identity register; if the part is debug-locked, unlock with
    --debug-unlock-pw; with confirm=True refuse unless the silicon's family
    matches the chip, and unless every (lo, hi) in `spans` lies in the memory
    map of this exact variant; optionally halt CPU0 at reset (halt=True); then
    disable flash protection if asked. -> (id_addr, id_value). Raises Fail
    (locked=True when no password was given)."""
    identity = _read_identity(dap, chip)
    addr, chipid = _debug_unlock(dap, args, chip, identity)
    if confirm:
        _confirm_family(chip, addr, chipid)
        if spans:
            _confirm_variant(chip.for_identity(addr, chipid), spans)
    if halt:
        _halt_cpu(dap, chip, _locked(identity[1]))
    _disable_flash_protection(dap, args, chip)
    return addr, chipid


def _detected_family(id_addr, chipid):
    """The generation of the connected silicon from its identity register:
    SCU_CHIPID.CHTEC (01 TC2xx, 10 TC3xx), or the family whose own identity
    register answered (TC4xx has no SCU_CHIPID). None if it cannot tell."""
    if id_addr == SCU_CHIPID:
        return {1: "tc2xx", 2: "tc3xx"}.get((chipid >> 6) & 3)
    families = {chip.family for chip in all_chips() if chip.id_addr == id_addr}
    return families.pop() if len(families) == 1 else None


def _confirm_family(chip, id_addr, chipid):
    """Refuse to drive the flash unless the connected silicon is the family -c
    or --device names: the flash register map differs per family."""
    detected = _detected_family(id_addr, chipid)
    if detected is None:
        raise Fail("cannot tell the connected part's family (identity 0x%08X at 0x%08X); "
                   "the flash registers differ per family" % (chipid, id_addr), EX_REFUSED)
    if detected != chip.family:
        raise Fail("the connected part is %s but %s is %s; the flash registers differ per "
                   "family" % (detected, chip.name, chip.family), EX_REFUSED)


def _confirm_variant(variant, spans):
    """Refuse unless every (lo, hi) span lies in one address range of the
    connected variant's memory map (a reduced-flash part has less PFLASH than
    its family entry)."""
    for lo, hi in spans:
        inside = any(r.base <= lo and hi <= r.end for r in variant.memory)
        if variant.pflash_unknown is not None and variant.kind_at(lo) == "pflash":
            raise Fail("this %s variant has %d KiB PFLASH and the device file has no "
                       "layout for that size"
                       % (variant.name, variant.pflash_unknown // 1024), EX_REFUSED)
        if not inside:
            raise Fail("0x%08X..0x%08X is outside the memory map of this %s variant"
                       % (lo, hi, variant.name), EX_REFUSED)


def _check_supported(chip, what):
    """Raise Fail unless tcprog drives this chip's flash (the TC3xx/TC4xx DMU)
    and its device map agrees with the flash profile about the UCBs. TC2xx keeps
    flash status in its FLASH0 module, and a device file without a known family
    names no register map."""
    if not drives(chip.family):
        raise Fail("%s is not supported on %s (family %s); tcprog drives the "
                   "TC3xx/TC4xx flash controller only" % (what, chip.name, chip.family),
                   os.EX_UNAVAILABLE)
    problem = device_map_problem(chip)
    if problem:
        raise Fail("%s: %s" % (chip.name, problem), EX_REFUSED)


def _halt_cpu(dap, chip, was_locked):
    """TC3xx: PFLASH program/erase can be refused (PROER) once the application
    has started, so reset with Halt-After-Reset and keep CPU0 at its first
    instruction during the flash operation."""
    if was_locked:
        warn("not halting CPU0: the part was debug-locked and a halt-after-reset would lock "
             "it again; PFLASH program/erase may be refused while the application runs")
        return
    pc = dap.halt_after_reset()
    if pc is None:
        warn("could not halt CPU0 at reset; PFLASH program/erase may be refused while the "
             "application runs")
    else:
        log("tcprog: CPU0 halted at reset (PC 0x%08X)" % pc, verbose=True)


def _after(dap, args, halted):
    """--after: reset (default) restarts the application; no-reset / halt leave
    CPU0 halted."""
    if halted and args.after == "reset":
        try:
            dap.pulse_reset()
        except DapError as error:
            warn("could not reset the target afterwards (%s); CPU0 may still be halted, reset "
                 "or power-cycle it" % error)
            return
        log("tcprog: reset, application restarted", verbose=True)


def _disable_flash_protection(dap, args, chip):
    """--pflash-unlock-pw / --dflash-unlock-pw: Disable Protection with that
    flash's password (or the debug password when given without a value). On
    TC4xx one UCB_RTC_FLASH password covers both."""
    wanted = [(which, value) for which, value in (("pflash", args.pflash_unlock_pw),
                                                  ("dflash", args.dflash_unlock_pw))
              if value is not None]
    if not wanted:
        return
    if chip is None:
        raise Fail("flash unlock needs the target; pass -c/--chip", os.EX_USAGE)
    if not drives(chip.family):
        raise Fail("flash unlock is not supported on %s yet" % chip.family,
                   os.EX_UNAVAILABLE)
    flash = Flash(dap, chip)
    done = set()
    for which, value in wanted:
        text = value or args.debug_unlock_pw
        if not text:
            raise Fail("--%s-unlock-pw needs a value; there is no --debug-unlock-pw to reuse"
                       % which, os.EX_USAGE)
        try:
            words = parse_password(text)
        except ValueError as error:
            raise Fail(str(error), os.EX_USAGE)
        key = flash.p.dp_selector[which]
        if key in done:
            continue
        try:
            already = flash.disable_protection(which, words)
        except DapError as error:
            raise Fail(str(error), os.EX_NOPERM)
        done.add(key)
        if not already:
            log("tcprog: %s protection disabled" % which.upper(), verbose=True)


def _debug_unlock(dap, args, chip, identity):
    """Unlock the debug interface if the identity read (addr, value) shows it is
    locked. -> (id_addr, id_value) as read after the unlock."""
    addr, chipid = identity
    if not _locked(chipid):
        return addr, chipid
    if chip is not None and chip.family == "tc4xx":
        raise Fail("debug-locked; debug unlock is not supported on TC4xx", os.EX_NOPERM,
                   locked=not args.debug_unlock_pw)
    if not args.debug_unlock_pw:
        raise Fail("debug-locked (DBGIFLCK); pass --debug-unlock-pw to unlock",
                   os.EX_NOPERM, locked=True)
    try:
        words = parse_password(args.debug_unlock_pw)
    except ValueError as error:
        raise Fail(str(error), os.EX_USAGE)
    if not args.quiet:
        log("tcprog: debug-locked, trying debug unlock (up to %d attempts)" % args.unlock_tries)
    ok, chipid, tries = dap.unlock(words, args.unlock_tries, on_attempt=_log_attempt)
    if not ok:
        raise Fail(_unlock_failed_msg(dap, tries), os.EX_NOPERM)
    log("tcprog: unlocked after %d attempt%s" % (tries, "" if tries == 1 else "s"), verbose=True)
    return SCU_CHIPID, chipid


# ======================================================================
# ident
# ======================================================================
def _cmd_ident(args):

    chip = _resolve_chip(args)   # fail fast on a bad --device/--chip

    vid, pid, serial = parse_probe(args.probe)
    probe_label = "dap %04x:%04x%s" % (vid, pid, ":" + serial if serial else "")
    dap = _open_dap(args)

    with dap:
        try:
            id_addr, chipid = _unlock_if_locked(dap, args, chip)
        except Fail as f:
            if f.locked:
                # Link is up but identity is unreadable: present and debug-locked.
                # Reported as such, rather than as a bare error, so a probe-only
                # run is still useful.
                if args.json:
                    print(json.dumps({"chip": None, "debug_interface_locked": True}))
                elif not args.quiet:
                    print("target (%s):" % probe_label)
                    print("  %-11s %s" % ("chip", "locked (identity unreadable)"))
                return _error("target present but debug-locked (DBGIFLCK); pass "
                             "--debug-unlock-pw to identify it", f.code)
            raise

        if chip is None:
            chip = _detected(id_addr, chipid)
        else:
            chip = chip.for_identity(id_addr, chipid)
        jtag = id_addr != SCU_CHIPID
        fields = _decode_jtagid(chipid) if jtag else _decode_chipid(chipid)
        step = fields["version"] if jtag else fields["chrev"]
        hsm = None if jtag else bool(fields["sec"])
        pf_kib = (sum(b.size for b in chip.noncached_ranges("pflash")) // 1024 or None) if chip else None
        df_kib = (chip.first("dflash").size // 1024) if chip and chip.first("dflash") else None

        if args.json:
            part = next((alias for alias in chip.aliases if alias.startswith("sak")),
                        None) if chip else None
            print(json.dumps({
                "tcprog": VERSION,
                "chip": {
                    "id": "0x%08X" % chipid,
                    "name": chip.name if chip else None,
                    "part_number": part,
                    "family": chip.family if chip else None,
                    "id_register": "JTAGID" if jtag else "CHIPID",
                    "step": "0x%02X" % step,
                },
                "pflash_kib": pf_kib,
                "dflash_kib": df_kib,
                "hsm": hsm,
            }))
        else:
            wanted = args.fields.split(",") if args.fields else None

            def show(key, label, value):
                if wanted is None or key in wanted:
                    print("    %-8s %s" % (label, value))
            kind = "jtagid" if jtag else "chipid"
            print("target (%s), %s: 0x%08X" % (probe_label, kind, chipid))
            if wanted is None:
                print("  identity (decoded from the %s):" % kind)
            show("chip", "chip", "%s (%s)" % (chip.name if chip else "unknown",
                                              chip.family if chip else "?"))
            if jtag:
                show("part", "part", "0x%04X" % fields["part"])
            show("step", "step", "0x%02X" % step)
            show("hsm", "hsm", "n/a" if jtag else "%s" % hsm)    # TC4xx: CSRM, not in the JTAG ID
            show("pflash", "pflash", "%s KiB" % pf_kib if pf_kib else "?")
            show("dflash", "dflash", "%s KiB" % df_kib if df_kib else "?")

        if args.check:
            detected = chip.name if chip else "unknown"
            expected = args.check.strip().lower()
            matched = chip is not None and (expected == chip.name or expected in chip.aliases)
            if not matched:
                return _error("ident --check: detected %s, expected %s"
                             % (detected, args.check), EX_CHECK)
    return os.EX_OK


# ======================================================================
# ucb
# ======================================================================
def _image_payload(segments, fmt, start, end, quiet=False):
    """[(addr, bytes)] as the bytes of a bin, srec or hex file. A bin file is
    one flat run from start to end: what no segment covers is 0x00, with a
    warning naming how much."""
    if fmt == "bin":
        data, gaps = flat_binary(segments, start, end)
        if gaps and not quiet:
            warn("the bin file (0x%08X..0x%08X) has %s in %d gap(s) filled with 0x00"
                 % (start, end, _size(sum(hi - lo for lo, hi in gaps)), len(gaps)))
        return data
    binfile = bincopy.BinFile()
    for addr, data in segments:
        binfile.add_binary(bytes(data), address=addr)
    return (binfile.as_ihex() if fmt == "hex" else binfile.as_srec()).encode()


def _export_bmhds(dap, chip, args):
    """Read the raw BMHD blocks (header, password, confirmation) to -o. A bin
    file fills the gaps between blocks (TC3xx ORIG and COPY) with 0x00."""
    if not args.output:
        raise Fail("ucb export needs -o FILE", os.EX_USAGE)
    fmt = args.format or _out_format(args.output)
    blocks = bmhd_blocks(chip)
    segments = []
    for name, addr, size in blocks:
        log("tcprog: reading %s @ 0x%08X (%d bytes)" % (name, addr, size), verbose=True)
        segments.append((addr, read_words(dap, addr, size)))
    payload = _image_payload(segments, fmt, min(addr for addr, _ in segments),
                             max(addr + len(data) for addr, data in segments), args.quiet)
    try:
        with open(args.output, "wb") as handle:
            handle.write(payload)
    except OSError as error:
        raise Fail(error, os.EX_CANTCREAT)
    log("tcprog: exported %d BMHD block(s) (%s) -> %s"
        % (len(blocks), ", ".join("%s 0x%08X" % (name, addr) for name, addr, _ in blocks),
           args.output), verbose=True)
    return os.EX_OK


def _confirm_json(state, word):
    """One confirmation code for --json: {"state": token, "word": "0x..." or None}.
    Tokens: unlocked, confirmed, errored, erased, unknown, read_protected,
    unreadable. None where the UCB has no such code."""
    if state is None:
        return None
    token = state.split(" ")[0].lower().replace("-", "_")
    return {"state": "unknown" if token.startswith("0x") else token,
            "word": None if word is None else "0x%08X" % word}


def _cmd_ucb(args):
    action = args.action
    chip = _resolve_chip(args)
    dap = _open_dap(args)
    with dap:
        id_addr, chipid = _unlock_if_locked(dap, args, chip)
        if chip is None:
            chip = _detected(id_addr, chipid)
        if chip is None:
            raise Fail("unknown target; pass -c/--chip or --device", os.EX_USAGE)
        if action == "export":
            return _export_bmhds(dap, chip, args)
        ucbs = _read_ucbs(dap, chip)
        boot = next_boot_report(dap, chip)

        if args.json:
            print(json.dumps([{"name": u["name"], "addr": "0x%08X" % u["addr"],
                               "orig": _confirm_json(u["orig"], u["orig_word"]),
                               "copy": _confirm_json(u["copy"], u["copy_word"]),
                               "in_use": u["source"],
                               "bmhd": None if u["bmhd"] is None else
                               {"bmi": "0x%04X" % u["bmhd"]["bmi"],
                                "hwcfg": u["bmhd"].get("hwcfg"),
                                "stad": "0x%08X" % u["bmhd"]["stad"],
                                "valid": u["bmhd_ok"], "boot": bool(u.get("boot"))},
                               "problems": u.get("problems", []),
                               "warnings": u.get("warnings", [])} for u in ucbs]))
        elif action == "decode":
            print("UCBs (%s):" % chip.name)
            for u in ucbs:
                print("  %-7s @ 0x%08X  %s" % (u["name"], u["addr"], u["state"]))
                if u["bmhd"] is not None:
                    b = u["bmhd"]
                    mode = ("hwcfg=0x%X" % b["hwcfg"]) if "hwcfg" in b else ("bmi=0x%04X" % b["bmi"])
                    print("          BMHD %s stad=0x%08X crc=%s%s"
                          % (mode, b["stad"], "ok" if u["bmhd_ok"] else "BAD",
                             "  <- boots from this header" if u.get("boot") else ""))
                    for problem in u["bmhd_problems"]:
                        print("          ! %s" % problem)
                for message in u.get("problems", []) + u.get("warnings", []):
                    print("          ! %s" % message)
            if boot:
                print("Next boot validation:")
                for level, message in boot:
                    print("  %-7s %s" % (level.upper(), message))

        if action == "verify":
            problems = ["next boot validation: %s" % m for level, m in boot if level == "lockout"]
            for u in ucbs:
                if "problems" in u:
                    # TC3xx/TC4xx: judged per ORIG/COPY pair; only the BMHD
                    # that boots has to be valid. The rest is TC2xx.
                    problems += u["problems"]
                    for warning in u["warnings"]:
                        warn(warning)
                    continue
                if u["confirm"] is not None and confirm_unknown(u["confirm"]):
                    problems.append("%s confirm word 0x%08X unrecognized"
                                    % (u["name"], u["confirm"]))
                if u["bmhd"] is not None and not u["bmhd_ok"]:
                    problems.append("%s: %s" % (u["name"], "; ".join(u["bmhd_problems"])))
            if problems:
                for p in problems:
                    _error(p, EX_VERIFY)
                return _error("ucb verify found %d problem(s)" % len(problems), EX_VERIFY)
            log("tcprog: UCBs OK", verbose=True)
    return os.EX_OK


# ======================================================================
# read
# ======================================================================
def _region_ranges(chip, regions):
    """[(base, size)] of the named regions, sorted, adjacent or overlapping ones
    merged. A name is an address range of the device file (dflash1, ucb, dspr0)
    or a kind: pflash is every PFLASH bank, dflash is DFLASH0."""
    spans = []
    for region in regions:
        found = ([r for r in chip.memory if r.name == region]
                 or chip.noncached_ranges(region))
        if not found:
            known = [r.name for r in chip.memory]
            known += [kind for kind in ("pflash", "dflash", "ucb") if kind not in known]
            raise Fail("%s has no region %r (choose from %s)"
                       % (chip.name, region, ", ".join(known)), os.EX_USAGE)
        spans += [(r.base, r.end) for r in found]
    ranges = []
    for lo, hi in sorted(spans):
        if ranges and lo <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(hi, ranges[-1][1]))
        else:
            ranges.append((lo, hi))
    return [(lo, hi - lo) for lo, hi in ranges]


def _range_label(chip, addr, length):
    """`PFLASH 0xA0000000..0xA0600000`: the kind of memory and the range."""
    kind = chip.kind_at(addr) if chip is not None else "?"
    return "%s 0x%08X..0x%08X" % ("memory" if kind == "?" else kind.upper(), addr,
                                  addr + length)


def _read_range(dap, args, chip, addr, length):
    """Read one address range. -> (segments, unreadable, missed_bytes): the
    [(addr, bytes)] that were read, the (lo, hi) spans that are programmed but
    unreadable, and the bytes a plain block scan could not read."""
    where = _range_label(chip, addr, length)
    show = args.progress and not args.quiet
    start, end = _block_span(addr, length)
    if not args.skip_blank:
        # Exactly the requested range: reading past its end can hit erased
        # flash, which fails the read ECC.
        log("tcprog: %s: reading %s" % (where, _size(length)), verbose=True)
        with Progress(where + ": read", (end - start) // 1024, " KiB", show) as reading:

            def on_progress(position):
                reading.update(-(-(position - start) // 1024))
            data = _read_region(dap, addr, length, on_progress=on_progress)
        return [(addr, data)], [], 0

    # On TC3xx/TC4xx flash (PFLASH, DFLASH, UCBs), map occupancy with the
    # hardware blank-check and read only the programmed pages. Elsewhere (TC2xx,
    # RAM, CFS) keep the blocks that answer a plain read.
    geometry = blankcheck_geometry(chip, addr, length)
    if geometry is not None:
        sectors = _flash_sectors(addr, length, geometry[0])[1]
        log("tcprog: %s: blank-checking %d logical sector(s), then reading the programmed "
            "pages" % (where, sectors), verbose=True)
        # The read phase's size is known once the blank-check is done.
        reading = []

        def on_read(done, total):
            if not reading:
                progress.finish()
                reading.append(Progress(where + ": read", -(-total // 1024), " KiB", show))
            reading[0].update(-(-done // 1024))
        with Progress(where + ": blank-check", sectors, " logical sectors", show) as progress:
            try:
                segments, unreadable = read_region_blankcheck(
                    dap, addr, length, chip,
                    on_map=lambda done, total: progress.update(done), on_read=on_read)
            finally:
                if reading:
                    reading[0].finish()
        return segments, unreadable, 0

    log("tcprog: %s: reading, keeping the blocks that answer" % where, verbose=True)
    with Progress(where + ": read", (end - start) // 1024, " KiB", show) as progress:
        segments, missed = read_region_sparse(
            dap, addr, length, on_progress=progress.update)
    return segments, [], missed * Dap.BLOCK


def _read_failed(chip, addr):
    """Why a plain read stopped at addr, and what to do about it."""
    kind = chip.kind_at(addr) if chip is not None else "?"
    if kind == "pflash":
        why = ("blank or unreadable PFLASH (blank PFLASH fails the ECC check); use "
               "--skip-blank to read only the programmed data")
    elif kind in ("dflash", "ucb"):
        why = ("%s that is blank, unreadable or read-protected; use --skip-blank to read "
               "only the programmed data" % kind.upper())
    else:
        why = "no answer from the target"
    return "read failed at 0x%08X: %s" % (addr, why)


def _cmd_read(args):

    chip = _resolve_chip(args)

    # ranges: explicit --addr/--length, or the whole --region(s) of the chip
    # (named, or detected after connecting)
    addr, length = args.addr, args.length
    if (addr is None or length is None) and not args.region:
        return _error("read needs --addr and --length, or --region", os.EX_USAGE)
    if length is not None and length <= 0:
        return _error("--length must be positive", os.EX_USAGE)
    fmt = args.format or _out_format(args.output)

    dap = _open_dap(args)

    with dap:
        id_addr, chipid = _unlock_if_locked(dap, args, chip)
        if chip is None:
            chip = _detected(id_addr, chipid)
        else:
            chip = chip.for_identity(id_addr, chipid)
        if addr is None or length is None:
            if chip is None:
                raise Fail("cannot detect the connected part for --region; pass -c/--chip",
                           os.EX_USAGE)
            ranges = _region_ranges(chip, args.region)
            if addr is not None or length is not None:
                if len(ranges) > 1:
                    raise Fail("--addr/--length with --region needs a region of one "
                               "address range", os.EX_USAGE)
                ranges = [(ranges[0][0] if addr is None else addr,
                           ranges[0][1] if length is None else length)]
        else:
            ranges = [(addr, length)]
        if chip is not None and not args.quiet:
            for base, size in ranges:
                if not (chip.range_at(base) and chip.range_at(base + size - 1)):
                    warn("0x%08X..0x%08X is outside a known address range; reading there "
                         "can fail or wedge the target" % (base, base + size))
        if args.skip_blank and any(blankcheck_geometry(chip, base, size)
                                    for base, size in ranges):
            # The blank-check drives the flash controller: confirm the family.
            _confirm_family(chip, id_addr, chipid)

        results = []
        started = time.time()
        for base, size in ranges:
            try:
                results.append(_read_range(dap, args, chip, base, size))
            except ReadFailed as error:
                return _error(_read_failed(chip, error.addr), os.EX_IOERR)
            except DapError as error:
                return _error("read failed: %s" % error, os.EX_IOERR)
        elapsed = time.time() - started

    segments = [segment for result in results for segment in result[0]]
    data_len = sum(len(data) for _, data in segments)
    if not args.output and fmt == "bin":
        for seg_addr, seg_data in segments:
            _hexdump(seg_data, seg_addr)
    else:
        payload = _image_payload(segments, fmt, ranges[0][0], sum(ranges[-1]), args.quiet)
        if not args.output:
            sys.stdout.write(payload.decode())
        else:
            try:
                with open(args.output, "wb") as handle:
                    handle.write(payload)
            except OSError as error:
                return _error(error, os.EX_CANTCREAT)

    throughput = (data_len / 1024.0) / elapsed if elapsed > 0 else 0.0
    unreadable = [span for result in results for span in result[1]]
    missed = sum(result[2] for result in results)
    # On TC2xx (no flash driver) a block that fails the scan is blank or
    # unreadable. On TC3xx/TC4xx the scan covers only ranges without a
    # blank-check (RAM, CFS), so a failed block there is unreadable.
    driven = chip is not None and drives(chip.family)
    missed_kind = "unreadable" if driven else "blank or unreadable"
    bad = sum(hi - lo for lo, hi in unreadable) + (missed if driven else 0)
    if args.json:
        print(json.dumps({"chip": chip.name if chip else "unknown",
                          "ranges": [{"addr": "0x%08X" % base, "length": size}
                                     for base, size in ranges],
                          "segments": len(segments), "length": data_len,
                          "unreadable": bad,
                          "format": fmt, "output": args.output or "-",
                          "kib_per_s": round(throughput, 1)}))
    elif not args.quiet:
        # Success is silent; the report comes with -v. Data missing from the
        # image is always reported.
        lines = []
        for (base, size), (got, gone, _) in zip(ranges if args.skip_blank else [], results):
            lines.append("tcprog: %s (%s)" % (_range_label(chip, base, size), _size(size)))
            rows = [("segment", lo, len(data)) for lo, data in got]
            rows += [("unreadable", lo, hi - lo) for lo, hi in gone]
            for name, lo, n in sorted(rows, key=lambda row: row[1]):
                lines.append("  %-10s  0x%08X..0x%08X  %8s" % (name, lo, lo + n, _size(n)))
            if not rows:
                lines.append("  blank")
        summary = "tcprog: read %s" % _size(data_len)
        if args.skip_blank:
            summary += " in %d segment(s)" % len(segments)
        summary += " at %.1f KiB/s" % throughput
        if bad:
            summary += ", %s unreadable" % _size(bad)
        if args.output:
            summary += " -> %s" % args.output
        lines.append(summary)
        log("\n".join(lines), verbose=True)
    if not args.quiet:
        for lo, hi in unreadable:
            warn("0x%08X..0x%08X (%s) is programmed but unreadable; not in the image"
                 % (lo, hi, _size(hi - lo)))
        if missed:
            warn("%s %s; not in the image" % (_size(missed), missed_kind))
    return os.EX_OK


# ======================================================================
# write / erase
# ======================================================================
def _confirm_or_abort(lines):
    """Show the plan and ask before touching flash. Raises Fail unless the
    answer is yes."""
    sys.stderr.write("\n".join(lines) + "\n")
    try:
        if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            raise Fail("not confirmed; nothing was changed", EX_REFUSED)
    except EOFError:
        raise Fail("no terminal to confirm on; pass -y/--yes", EX_REFUSED)


def _size(n):
    """A byte count for reports: 256 B, 27 KiB, 1.5 KiB."""
    if n < 1024:
        return "%d B" % n
    return ("%d KiB" % (n // 1024)) if not n % 1024 else ("%.1f KiB" % (n / 1024.0))


def _detected(id_addr, chipid, verbose=True):
    """The database entry for the connected part, as its identity register
    describes it (reduced-flash variant included), logged. -> Chip or None."""
    chip = by_id(chipid, id_addr)
    if chip is not None:
        chip = chip.for_identity(id_addr, chipid)
        log("tcprog: detected %s (%s 0x%08X, PFLASH %d KiB)"
            % (chip.name, "CHIPID" if id_addr == SCU_CHIPID else "JTAGID", chipid,
               sum(r.size for r in chip.noncached_ranges("pflash")) // 1024),
            verbose=verbose)
    return chip


def _detect_chip(dap, args):
    """-c auto: identify the connected part (unlocking debug if needed). Raises
    Fail if the database does not recognise it."""
    id_addr, chipid = _debug_unlock(dap, args, None, _read_identity(dap))
    chip = _detected(id_addr, chipid, verbose=False)
    if chip is None:
        raise Fail("cannot detect the connected part (identity 0x%08X at 0x%08X); pass "
                   "-c/--chip" % (chipid, id_addr), os.EX_USAGE)
    return chip


def _write_plan(chip, image, args):
    """The checks of a write, in order: family and device map, allowlist, UCB
    gate, erase plan, write- and erase-protected ranges; then the confirmation prompt.
    -> erase plan. Raises Fail."""
    _check_supported(chip, "write")

    # ALLOWLIST: every segment must sit inside one of the device's address ranges.
    # A custom --device file with only the permitted ranges thus confines a write
    # to those regions and refuses anything else.
    outside = [segment for segment in image.segments
               if not any(region.base <= segment.addr and segment.end <= region.end
                          for region in chip.memory)]
    if outside:
        raise Fail("%d segment(s) fall outside %s's memory map (%s)"
                   % (len(outside), chip.name,
                      ", ".join("0x%08X..0x%08X" % (s.addr, s.end) for s in outside)),
                   EX_REFUSED)

    # SAFETY GATE: refuse writes that touch any UCB region unless enabled.
    ucb_segments = [s for s in image.segments if chip.kind_at(s.addr) == "ucb"]
    if ucb_segments and not args.enable_ucb_write:
        raise Fail("the image writes to the UCB region (%s) and --enable-ucb-write is not "
                   "set; a wrong UCB can lock you out of the part or stop it booting"
                   % ", ".join("0x%08X" % s.addr for s in ucb_segments), EX_REFUSED)

    # Every segment must be flash; the erase is planned before anything is
    # erased, so a bad segment is refused first.
    try:
        plan = erase_plan(chip, image)
    except ValueError as error:
        raise Fail(str(error), EX_REFUSED)

    # PROTECTED: no segment, and no sector the write erases, may overlap a
    # write-protected range. A sector the write erases in an erase-protected
    # range must be filled completely by the image, so none of it is left
    # erased. No option lifts this.
    spans = [(segment.addr, segment.end) for segment in image.segments]
    if args.erase == "sectors":
        spans += [(first, first + count * size) for first, count, size in plan]
    _refuse_protected(chip, spans, "write_protected")
    if args.erase == "sectors":
        for first, count, size in plan:
            for lo in range(first, first + count * size, size):
                hi = lo + size
                region = chip.protected_at(lo, hi, "erase_protected")
                filled = sum(max(0, min(hi, s.end) - max(lo, s.addr)) for s in image.segments)
                if region is not None and filled < size:
                    raise Fail("the image fills %s of the sector 0x%08X..0x%08X; "
                               "the rest would be left erased in the erase-protected range %s"
                               % (_size(filled), lo, hi, region.name), EX_REFUSED)

    if not args.yes:
        span_lo, span_hi = image.span
        lines = ["tcprog: write to %s" % chip.name,
                 "  range     0x%08X..0x%08X  %d B in %d segment(s)"
                 % (span_lo, span_hi, image.total_bytes, len(image.segments)),
                 "  regions   %s" % _regions_label(chip, image.segments),
                 "  erase     %s" % ("%d sector(s) first" % sum(count for _, count, _ in plan)
                                     if args.erase == "sectors" else "none"),
                 "  WARNING   flash is erased and rewritten; this cannot be undone"]
        if ucb_segments:
            lines.append("  WARNING   UCB write: applies at the next reset; check `tcprog ucb` first")
        _confirm_or_abort(lines)
    return plan


def _cmd_write(args):
    if not args.firmware:
        return _error("no firmware image given; see `tcprog write --help`", os.EX_USAGE)
    auto = (args.chip or "").lower() == "auto" and not args.device
    chip = _resolve_chip(args)
    # A write is gated on the target's memory map, so the part is named up front,
    # or detected (-c auto) before anything is planned or written.
    if chip is None and not auto:
        return _error("write needs the target; pass -c/--chip (e.g. -c tc375, or -c auto "
                      "to detect it) or --device FILE", os.EX_USAGE)
    try:
        image = load_image(args.firmware, fmt=args.format, address=args.address)
    except (ValueError, OSError) as error:
        return _error(error, os.EX_DATAERR)

    if args.dry_run:
        if auto:
            return _error("--dry-run does not connect; it needs a chip name, not -c auto",
                          os.EX_USAGE)
        return _dry_run_plan(image, args, chip)

    plan = None if auto else _write_plan(chip, image, args)
    dap = _open_dap(args)
    with dap:
        if auto:
            chip = _detect_chip(dap, args)
            plan = _write_plan(chip, image, args)
        halt = (driven_layout(chip.family).halt_for_pflash
                and any(_is_pflash(seg.addr) for seg in image.segments))
        _unlock_if_locked(dap, args, chip, halt=halt, confirm=True,
                          spans=[(segment.addr, segment.end) for segment in image.segments])
        try:
            return _program(dap, image, args, chip, plan)
        except DapError as error:
            return _error("write failed: %s" % error, os.EX_IOERR)
        finally:
            _after(dap, args, halt)


def _refuse_protected(chip, spans, flag):
    """Raise Fail if any (lo, hi) span overlaps a range with `flag` set
    (write_protected or erase_protected)."""
    for lo, hi in spans:
        region = chip.protected_at(lo, hi, flag)
        if region is not None:
            raise Fail("0x%08X..0x%08X overlaps the %s range %s (0x%08X..0x%08X)"
                       % (lo, hi, flag.replace("_", "-"), region.name, region.base,
                          region.end), EX_REFUSED)


def _erase_plan(chip, args):
    """The checks of an erase and its confirmation prompt.
    -> [(first, count, sector size)], one run per range. Raises Fail."""
    _check_supported(chip, "erase")
    if args.region and (args.addr is not None or args.length is not None):
        raise Fail("erase takes --region or --addr/--length, not both", os.EX_USAGE)
    if args.region:
        ranges = _region_ranges(chip, args.region)
    elif args.addr is None or args.length is None:
        raise Fail("erase needs --addr and --length, or --region", os.EX_USAGE)
    else:
        ranges = [(args.addr, args.length)]
    layout = driven_layout(chip.family)
    plan = []
    for addr, length in ranges:
        sector = _log_sector_size(addr, layout)
        if sector is None or _log_sector_size(addr + length - 1, layout) != sector:
            raise Fail("0x%08X..0x%08X is not PFLASH, DFLASH or a UCB; erase supports only "
                       "those" % (addr, addr + length), os.EX_USAGE)
        plan.append(_flash_sectors(addr, length, sector) + (sector,))
    if args.region and not args.allow_erase_all:
        raise Fail("erasing whole regions (%s) needs --allow-erase-all"
                   % ",".join(args.region), EX_REFUSED)
    spans = [(first, first + count * sector) for first, count, sector in plan]
    # SAFETY GATE: as for a write, a UCB is erased only with --enable-ucb-write.
    ucb = any(region.kind == "ucb" and region.base < hi and lo < region.end
              for lo, hi in spans for region in chip.memory)
    if ucb and not args.enable_ucb_write:
        raise Fail("the erase covers a UCB and --enable-ucb-write is not set; an erased UCB "
                   "can lock you out of the part or stop it booting",
                   EX_REFUSED)
    _refuse_protected(chip, spans, "write_protected")
    _refuse_protected(chip, spans, "erase_protected")
    if not args.yes:
        lines = ["tcprog: erase on %s" % chip.name]
        lines += ["  range     0x%08X..0x%08X  %d sector(s)"
                  % (first, first + count * sector, count) for first, count, sector in plan]
        if ucb:
            lines.append("  WARNING   UCB erase: applies at the next reset; check "
                         "`tcprog ucb` first")
        lines.append("  WARNING   the contents are erased; this cannot be undone")
        _confirm_or_abort(lines)
    return plan


def _cmd_erase(args):
    auto = (args.chip or "").lower() == "auto" and not args.device
    chip = _resolve_chip(args)
    if chip is None and not auto:
        return _error("erase needs the target; pass -c/--chip (or -c auto to detect it) "
                      "or --device FILE", os.EX_USAGE)

    plan = None if auto else _erase_plan(chip, args)
    dap = _open_dap(args)
    with dap:
        if auto:
            chip = _detect_chip(dap, args)
            plan = _erase_plan(chip, args)
        spans = [(first, first + count * sector) for first, count, sector in plan]
        halt = (driven_layout(chip.family).halt_for_pflash
                and any(_is_pflash(first) for first, _, _ in plan))
        _unlock_if_locked(dap, args, chip, halt=halt, confirm=True, spans=spans)
        try:
            flash = Flash(dap, chip)
            flash.clear_status()
            flash.clear_errors()
            for first, count, sector in plan:
                log("tcprog: erasing %d sector(s) from 0x%08X" % (count, first), verbose=True)
                flash.erase_range(first, count, sector)
            flash.reset_to_read()
            log("tcprog: erased %d sector(s) (%s)"
                % (sum(count for _, count, _ in plan),
                   ", ".join("0x%08X..0x%08X" % span for span in spans)), verbose=True)
        except DapError as error:
            return _error("erase failed: %s" % error, os.EX_IOERR)
        finally:
            _after(dap, args, halt)
    return os.EX_OK


def _regions_label(chip, segments):
    """Comma list of the regions the segments touch, naming UCBs individually."""
    names = []
    for seg in segments:
        region = chip.kind_at(seg.addr)
        name = ucb_name(chip, seg.addr) if region == "ucb" else region
        if name not in names:
            names.append(name)
    return ", ".join(names)


def _dry_run_plan(image, args, chip):
    region = chip.kind_at
    touches_ucb = any(region(seg.addr) == "ucb" for seg in image.segments)
    in_gap = [seg for seg in image.segments if region(seg.addr) == "?"]
    print("tcprog: dry-run (nothing will be written)")
    print("  image     %s  (%s via %s)" % (os.path.basename(args.firmware),
                                           args.format or "auto", image.loader))
    if image.start is not None:
        print("  start     0x%08X" % image.start)
    print("  segments:")
    for seg in image.segments:
        print("    0x%08X  0x%08X  %8d B  %s"
              % (seg.addr, len(seg.data), len(seg.data), region(seg.addr)))
    low, high = image.span
    print("  total     %d B in %d segment(s), span 0x%08X..0x%08X"
          % (image.total_bytes, len(image.segments), low, high))
    print("  erase %s   verify %s" % (args.erase, "no" if args.no_verify else "yes"))
    if touches_ucb:
        print("  WARNING   image covers a UCB region; a real write is REFUSED "
              "without --enable-ucb-write (bricking risk).")
    if in_gap:
        print("  WARNING   %d segment(s) fall in an address GAP for %s - wrong --chip "
              "or bad image?" % (len(in_gap), chip.name))
    return os.EX_OK


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return os.EX_OK
    set_verbose(getattr(args, "verbose", 0))
    log(BANNER, verbose=True)
    try:
        return args.func(args)
    except Fail as f:
        return _error(f, f.code)
