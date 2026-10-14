# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""tcprog CLI: argparse subcommands over the dap / flash / chips / image layers."""
import argparse
import glob
import json
import os
import sys

import bincopy

from . import __version__ as VERSION
from .chips import all_chips, by_chipid, chipid_addrs, _resolve_chip
from .dap import (Dap, DapError, SCU_CHIPID, parse_password, parse_probe,
                  _locked, _unlock_failed_msg)
from .flash import (Flash, PFLASH_LOG_SECTOR, _CONFIRM, _busy_mask, _flash_sectors,
                    _is_dflash, _is_pflash, _pflash_sectors, _program, _read_ucbs,
                    _sector_size, blank_map, bmhd_blocks, profile_for_family,
                    read_region_blankcheck, read_region_sparse, read_words)
from .image import load_image, _emit, _hexdump, _out_format
from .util import EX_CHECK, EX_REFUSED, EX_VERIFY, Fail, _error, log, set_verbose

DESCRIPTION = "AURIX TC2xx/TC3xx flash programmer over an FTDI-MPSSE DAP or ASC BSL."

EPILOG = r"""examples:
  tcprog list probes
  tcprog list chips
  tcprog ident --probe 058b:0043:MWAJFJ9N
  tcprog ucb -c tc375 -p 058b:0043:MWAJFJ9N --debug-unlock-pw 1,2,3,4,5,6,7,8
  tcprog write -c tc375 -p 058b:0043:MWAJFJ9N --debug-unlock-pw 1,2,3,4,5,6,7,8 app.elf
  tcprog read -p 058b:0043:MWAJFJ9N --debug-unlock-pw 1,2,3,4,5,6,7,8 --addr 0x80000000 --length 0x6c00 -o flash.bin
  tcprog read -t asc-bsl --asc-bsl-port /dev/ttyUSB0 --addr 0x80000000 --length 0x6c00 --asc-bsl-compress -o flash.bin
  tcprog write --dry-run -c tc375 app.srec

exit codes:
  0  ok                      64 usage (bad args)     65 bad image/data
  69 no probe/target/binding 73 cannot write output  74 transfer failed
  75 timeout                 77 debug-locked          80 ident --check mismatch
  81 verify mismatch         82 refused (safety, e.g. UCB write w/o flag)

environment:
  TCPROG_CHIP (target part), TCPROG_PROBE, TCPROG_TRANSPORT, TCPROG_DEBUG_UNLOCK_PW,
  TCPROG_PFLASH_UNLOCK_PW, TCPROG_DFLASH_UNLOCK_PW

Run `tcprog <command> --help` for a command's options. tcprog does not debug,
halt, or emulate. It programs flash and reports the target.
"""


def _hexint(s):
    return int(s, 0)


def _attempt_logger():
    """A dap.unlock(on_attempt=...) callback that logs each try under -v."""
    def cb(k, reached, error):
        if error is not None:
            log("  attempt %d: error: %s" % (k, error), verbose=True)
        else:
            log("  attempt %d: %s" % (k, "reached dialogue" if reached else "missed window"),
                verbose=True)
    return cb


def _decode_chipid(v):
    return {"sec": (v >> 31) & 1, "vart": (v >> 28) & 7, "fsize": (v >> 24) & 0xF,
            "ucode": (v >> 17) & 0x7F, "chid": (v >> 12) & 0xF,
            "chpk": (v >> 8) & 0xF, "chtec": (v >> 6) & 3, "chrev": v & 0x3F}


def _decode_jtagid(v):
    """IEEE 1149.1 ID: version, part number, manufacturer (Infineon = 0x083)."""
    return {"version": (v >> 28) & 0xF, "part": (v >> 12) & 0xFFFF, "manuf": (v >> 1) & 0x7FF}


def _todo(cmd):
    return _error("'%s' not implemented yet (needs the DAP/ASC-BSL transport)." % cmd,
                  os.EX_UNAVAILABLE)


# ======================================================================
# list
# ======================================================================
def _cmd_list(args):
    if args.what is None:
        args._parser.print_help()
        return os.EX_OK
    return _list_chips() if args.what == "chips" else _list_probes()


def _list_probes():
    KNOWN = {
        (0x0403, 0x6010): "Generic FT2232 (MPSSE)",
        (0x0403, 0x6011): "Generic FT4232 (MPSSE)",
        (0x0403, 0x6014): "Generic FT232H (MPSSE)",
        (0x058b, 0x0043): "Infineon DAS (miniWiggler / on-board)",
    }
    probes = []
    for dev_dir in sorted(glob.glob("/sys/bus/usb/devices/*/")):
        def read_attr(name):
            try:
                return open(dev_dir + name).read().strip()
            except OSError:
                return None
        vendor_s, product_s = read_attr("idVendor"), read_attr("idProduct")
        if not vendor_s or not product_s:
            continue
        vid, pid = int(vendor_s, 16), int(product_s, 16)
        if (vid, pid) not in KNOWN:
            continue
        probes.append((vid, pid, read_attr("serial"), read_attr("product"), KNOWN[(vid, pid)]))

    print("PROBES (use with --probe VID:PID or VID:PID:SERIAL):")
    if not probes:
        print("  none found (looked for FTDI 0403:* and Infineon 058b:0043)")
    for vid, pid, serial, product, kind in probes:
        probe_id = "%04x:%04x%s" % (vid, pid, ":" + serial if serial else "")
        print("  %-22s %s" % (probe_id, product or kind))
    return os.EX_OK


def _list_chips():
    print("%-8s %-7s %s" % ("chip", "family", "aliases"))
    for chip in all_chips():
        print("%-8s %-7s %s" % (chip.name, chip.family, ", ".join(chip.aliases)))
    print("\nAdd parts by dropping a JSON file in the devices/ dir of the package.")
    return os.EX_OK


# ======================================================================
# argument groups
# ======================================================================
def _add_conn_args(p):
    """Connection + credential options shared by ident / read / write."""
    g = p.add_argument_group("connection / transport")
    g.add_argument("-c", "--chip", default=os.environ.get("TCPROG_CHIP"),
                   help="target part or family (e.g. tc375, tc37x, tc2xx); "
                        "optional if ident detects it. Env: TCPROG_CHIP")
    g.add_argument("--device", metavar="FILE",
                   help="custom device JSON instead of a built-in part; its "
                        "memory slices act as the write allowlist")
    g.add_argument("-t", "--transport", default=os.environ.get("TCPROG_TRANSPORT", "dap"),
                   choices=("dap", "asc-bsl"), help="link to the target [default: dap]")
    g.add_argument("-p", "--probe", default=os.environ.get("TCPROG_PROBE"),
                   help="FTDI probe as VID:PID or VID:PID:SERIAL. Env: TCPROG_PROBE")
    g.add_argument("--speed", type=int, default=1000, help="DAP clock in kHz [default: 1000]")
    g.add_argument("--connect-under-reset", action="store_true", help="hold PORST while attaching")

    b = p.add_argument_group("ASC BSL transport (-t asc-bsl)")
    b.add_argument("--asc-bsl-port", help="serial port for ASC BSL")
    b.add_argument("--asc-bsl-baud", type=int, default=115200, help="ASC BSL baud [default: 115200]")

    s = p.add_argument_group("security (TC3xx protection - run ident first)")
    s.add_argument("--debug-unlock-pw", default=os.environ.get("TCPROG_DEBUG_UNLOCK_PW"),
                   help="256-bit debug/JTAG password: 8 words w0,..,w7 (dec/0x-hex), or "
                        "one value repeated 8x. Env: TCPROG_DEBUG_UNLOCK_PW")
    s.add_argument("--unlock-tries", type=int, default=10,
                   help="max stochastic unlock attempts [default: 10]")
    s.add_argument("--pflash-unlock-pw", nargs="?", const="",
                   default=os.environ.get("TCPROG_PFLASH_UNLOCK_PW"),
                   help="disable PFLASH protection; value only if it differs from the "
                        "debug password. Env: TCPROG_PFLASH_UNLOCK_PW")
    s.add_argument("--dflash-unlock-pw", nargs="?", const="",
                   default=os.environ.get("TCPROG_DFLASH_UNLOCK_PW"),
                   help="disable DFLASH protection; value only if it differs from the "
                        "debug password. Env: TCPROG_DFLASH_UNLOCK_PW")

    o = p.add_argument_group("output")
    o.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    o.add_argument("-v", "--verbose", action="count", default=0, help="more detail (repeatable)")
    o.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    return p


def _add_ident_args(p):
    g = p.add_argument_group("ident")
    g.add_argument("--check", metavar="NAME", help="exit 80 unless the detected chip matches NAME")
    g.add_argument("--fields", metavar="LIST", help="comma-separated subset of fields to print")
    return p


def _add_read_args(p):
    g = p.add_argument_group("read")
    g.add_argument("--addr", type=_hexint, help="start address to read")
    g.add_argument("--length", type=_hexint, help="number of bytes to read")
    g.add_argument("--region", choices=("pflash", "dflash"),
                   help="read a whole region (needs -c/--chip)")
    g.add_argument("-o", "--output", help="output file (bin/srec/hex by extension); omit to hexdump")
    g.add_argument("--format", choices=("srec", "hex", "bin"), default=None,
                   help="override the output format")
    g.add_argument("--skip-erased", "--sparse", dest="skip_erased", action="store_true",
                   help="tolerate erased/unreadable blocks: keep only what answers and "
                        "emit a sparse image (srec/hex). Needed to read a whole device "
                        "whose flash is mostly erased.")
    g.add_argument("--asc-bsl-compress", action="store_true", help="LZ4 the transfer (ASC BSL route)")
    g.add_argument("--no-progress", action="store_true", help="disable the progress line")
    return p


def _add_write_args(p):
    """Write-specific options (call after _add_conn_args)."""
    p.add_argument("firmware", nargs="?", help="image to write (.elf/.hex/.srec/.bin)")
    g = p.add_argument_group("image / write")
    g.add_argument("-a", "--address", type=_hexint, help="load address for a raw .bin")
    g.add_argument("--format", choices=("elf", "hex", "srec", "bin"),
                   help="override the image format")
    g.add_argument("--region", choices=("pflash", "dflash"),
                   help="target region [default: inferred from address]")
    g.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    g.add_argument("--erase", choices=("sectors", "none"), default="sectors",
                   help="erase policy [default: sectors]")
    g.add_argument("--preserve", action="append", default=[], metavar="RANGE",
                   help="do not touch this region (repeatable)")
    g.add_argument("--asc-bsl-compress", action="store_true", help="LZ4 the transfer (ASC BSL route)")

    r = p.add_argument_group("reset")
    r.add_argument("--before", choices=("default-reset", "no-reset", "porst"),
                   default="default-reset", help="reset action before [default: default-reset]")
    r.add_argument("--after", choices=("reset", "no-reset", "halt"), default="reset",
                   help="reset action after [default: reset]")

    s = p.add_argument_group("safety")
    s.add_argument("-n", "--dry-run", action="store_true", help="parse and plan, touch nothing")
    s.add_argument("--timeout", type=int, default=120, help="abort after N seconds [default: 120]")
    s.add_argument("--allow-erase-all", action="store_true", help="required for a full-chip erase")
    s.add_argument("--enable-ucb-write", action="store_true",
                   help="[EXPERIMENTAL] required to write a UCB region; IRREVERSIBLE, can brick")
    s.add_argument("--yes", action="store_true", help="do not prompt for confirmation")
    s.add_argument("--no-progress", action="store_true", help="disable the progress bar")
    s.add_argument("--log-file", help="append a full debug log here")
    return p


def _build_parser():
    """Top-level parser with a subcommand per action (proper argparse subparsers)."""
    p = argparse.ArgumentParser(
        prog="tcprog", description=DESCRIPTION,
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version="tcprog " + VERSION)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    pl = sub.add_parser("list", help="list attached probes or supported chips",
                        description="List attached probes, or supported chips.")
    pl.add_argument("what", nargs="?", choices=("probes", "chips"),
                    help="probes | chips")
    pl.set_defaults(func=_cmd_list, _parser=pl)

    pi = sub.add_parser("ident", help="connect and report the target (writes nothing)",
                        description="Connect and report the target; writes nothing.")
    _add_conn_args(pi)
    _add_ident_args(pi)
    pi.set_defaults(func=_cmd_ident)

    pr = sub.add_parser("read", help="read flash/memory to a file",
                        description="Read flash/memory to a file (or hexdump to stdout).")
    _add_conn_args(pr)
    _add_read_args(pr)
    pr.set_defaults(func=_cmd_read)

    pucb = sub.add_parser("ucb", help="read and decode the User Configuration Blocks",
                          description="Read and decode the UCBs (confirmation state + BMHD).")
    pucb.add_argument("action", nargs="?", choices=("decode", "verify", "export"),
                      default="decode",
                      help="decode (default), verify UCB integrity, or export the raw "
                           "BMHD blocks to -o")
    pucb.add_argument("-o", "--output", metavar="FILE",
                      help="export: output file; bin/srec/hex by extension or --format")
    pucb.add_argument("--format", choices=("bin", "srec", "hex"),
                      help="export: output format (default: from the -o extension)")
    _add_conn_args(pucb)
    pucb.set_defaults(func=_cmd_ucb)

    pw = sub.add_parser("write", help="program a firmware image to flash",
                        description="Program a firmware image to flash and verify it. "
                                    "Requires -c/--chip (or --device): a write is gated on "
                                    "the target's memory map, which tcprog will not guess.")
    _add_conn_args(pw)
    _add_write_args(pw)
    pw.set_defaults(func=_cmd_write)

    pe = sub.add_parser("erase", help="erase flash sectors without writing",
                        description="Erase PFLASH sectors (16 KiB logical sectors). "
                                    "Requires -c/--chip (or --device).")
    _add_conn_args(pe)
    ge = pe.add_argument_group("erase")
    ge.add_argument("--addr", type=_hexint, help="start address (rounded down to a sector)")
    ge.add_argument("--length", type=_hexint, help="bytes to erase (rounded up to sectors)")
    ge.add_argument("--region", choices=("pflash",), help="erase a whole region")
    ge.add_argument("--allow-erase-all", action="store_true",
                    help="required to erase a whole region")
    ge.add_argument("--yes", action="store_true", help="do not prompt for confirmation")
    pe.set_defaults(func=_cmd_erase)

    pbc = sub.add_parser("blankcheck", help="map programmed vs erased flash with the "
                                            "controller's hardware blank-check",
                         description="Report which PFLASH sectors are programmed, using the "
                                     "flash controller's Verify-Erased command (no array "
                                     "reads, no ECC traps). Non-destructive. TC3xx only.")
    _add_conn_args(pbc)
    gbc = pbc.add_argument_group("blankcheck")
    gbc.add_argument("--addr", type=_hexint, help="start address (rounded down to a sector)")
    gbc.add_argument("--length", type=_hexint, help="bytes to check (rounded up to sectors)")
    gbc.add_argument("--region", choices=("pflash",), help="check a whole region")
    gbc.add_argument("--no-progress", action="store_true", help="disable the progress line")
    pbc.set_defaults(func=_cmd_blankcheck)

    pu = sub.add_parser("unlock", help="present the debug password / disable protection (not yet implemented)")
    _add_conn_args(pu)
    pu.set_defaults(func=lambda a: _todo("unlock"))
    return p


# ======================================================================
# connect / unlock helpers
# ======================================================================
def _open_dap(args):
    """Open the probe and bring the DAP link up. -> Dap (caller uses `with`).
    Raises Fail on any error, with the right exit code."""
    vid, pid, serial = parse_probe(args.probe)
    try:
        dap = Dap(serial, vid, pid, args.speed * 1000)
    except DapError as error:
        raise Fail(str(error), os.EX_UNAVAILABLE)
    except ImportError:
        raise Fail("DAP needs the 'ftdi1' (libftdi1) Python binding", os.EX_UNAVAILABLE)
    if not dap.connect():
        dap.close()
        raise Fail("target did not answer (check cable/power, and that the part "
                   "is not strapped to BSL)", os.EX_UNAVAILABLE)
    return dap


def _read_identity(dap, chip=None):
    """Read the identity register: the known chip's, else each one in the DB in
    turn (SCU_CHIPID on TC2xx/TC3xx, CBS_JTAGID on TC4xx). -> (addr, value),
    value None if none answered."""
    addrs = [chip.chipid_addr] if chip is not None and chip.chipid_addr else chipid_addrs()
    for addr in addrs:
        value = dap.read_word_retry(addr)
        if not _locked(value):
            return addr, value
    return SCU_CHIPID, None


def _unlock_if_locked(dap, args, chip=None):
    """Read the identity register; if the part is debug-locked, unlock with
    --debug-unlock-pw. -> (id_addr, id_value). Raises Fail (locked=True when no
    password was given)."""
    addr, chipid = _read_identity(dap, chip)
    if not _locked(chipid):
        return addr, chipid
    if chip is not None and chip.family == "tc4xx":
        raise Fail("locked; TC4xx debug unlock is not supported (its authentication "
                   "differs from the TC2xx/TC3xx key exchange)", os.EX_NOPERM,
                   locked=not args.debug_unlock_pw)
    if not args.debug_unlock_pw:
        raise Fail("locked (DBGIFLCK); pass --debug-unlock-pw to unlock",
                   os.EX_NOPERM, locked=True)
    try:
        words = parse_password(args.debug_unlock_pw)
    except ValueError as error:
        raise Fail(str(error), os.EX_USAGE)
    if not args.quiet:
        sys.stderr.write("tcprog: locked, trying debug unlock (up to %d attempts)...\n"
                         % args.unlock_tries)
    ok, chipid, tries = dap.unlock(words, args.unlock_tries, on_attempt=_attempt_logger())
    if not ok:
        raise Fail(_unlock_failed_msg(dap, tries), os.EX_NOPERM)
    log("tcprog: unlocked after %d attempt%s" % (tries, "" if tries == 1 else "s"), verbose=True)
    return SCU_CHIPID, chipid


# ======================================================================
# ident
# ======================================================================
def _cmd_ident(args):
    if args.transport != "dap":
        return _todo("ident over %s" % args.transport)

    chip, error = _resolve_chip(args)   # fail fast on a bad --device/--chip
    if error:
        return _error(error, os.EX_USAGE)

    vid, pid, serial = parse_probe(args.probe)
    probe_label = "dap %04x:%04x%s" % (vid, pid, ":" + serial if serial else "")
    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)

    with dap:
        try:
            id_addr, chipid = _unlock_if_locked(dap, args, chip)
        except Fail as f:
            if f.locked:
                # Link is up but identity is unreadable: present and debug-locked.
                # Report that (do not just error) so a probe-only run is useful.
                if args.json:
                    print(json.dumps({"chip": None, "debugInterfaceLocked": True}))
                elif not args.quiet:
                    print("target (%s):" % probe_label)
                    print("  %-11s %s" % ("chip", "locked (identity unreadable)"))
                return _error("target present but debug-locked (DBGIFLCK); "
                             "pass --debug-unlock-pw to identify.", f.code)
            return _error(f, f.code)

        if chip is None:
            chip = by_chipid(chipid, id_addr)
        jtag = id_addr != SCU_CHIPID
        fields = _decode_jtagid(chipid) if jtag else _decode_chipid(chipid)
        step = fields["version"] if jtag else fields["chrev"]
        hsm = None if jtag else bool(fields["sec"])
        pf_kib = (chip.first("pflash").size // 1024) if chip and chip.first("pflash") else None
        df_kib = (chip.first("dflash").size // 1024) if chip and chip.first("dflash") else None

        if args.json:
            part = next((alias for alias in chip.aliases if alias.startswith("sak")),
                        None) if chip else None
            print(json.dumps({
                "chip": {
                    "id": "0x%08X" % chipid,
                    "name": chip.name if chip else None,
                    "partNumber": part,
                    "family": chip.family if chip else None,
                    "idRegister": "JTAGID" if jtag else "CHIPID",
                    "step": "0x%02X" % step,
                },
                "pflashKiB": pf_kib,
                "dflashKiB": df_kib,
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
            show("hsm", "hsm", "?" if hsm is None else "%s" % hsm)
            show("pflash", "pflash", "%s KiB" % pf_kib if pf_kib else "?")
            show("dflash", "dflash", "%s KiB" % df_kib if df_kib else "?")

        if args.check:
            detected = chip.name if chip else "unknown"
            expected = args.check.strip().lower()
            matched = chip is not None and (expected == chip.name or expected in chip.aliases)
            if not matched:
                return _error("ident --check mismatch: detected %s, expected %s"
                             % (detected, args.check), EX_CHECK)
    return os.EX_OK


# ======================================================================
# ucb
# ======================================================================
def _export_bmhds(dap, chip, args):
    """Read the raw BMHD blocks (header, password, confirmation) to -o."""
    if not args.output:
        raise Fail("ucb export needs -o FILE", os.EX_USAGE)
    fmt = args.format or _out_format(args.output)
    blocks = bmhd_blocks(chip)
    for (_n, addr, size), (_m, nxt, _s) in zip(blocks, blocks[1:]):
        if addr + size != nxt:
            raise Fail("BMHD blocks are not contiguous; export does not handle gaps",
                       os.EX_SOFTWARE)
    data = b""
    for name, addr, size in blocks:
        log("tcprog: reading %s @ 0x%08X (%d bytes)" % (name, addr, size), verbose=True)
        data += read_words(dap, addr, size)
    try:
        with open(args.output, "wb") as handle:
            handle.write(_emit(data, blocks[0][1], fmt))
    except OSError as error:
        raise Fail(error, os.EX_CANTCREAT)
    if not args.quiet:
        log("tcprog: exported %d BMHD block(s), 0x%08X..0x%08X (%d bytes, %s) -> %s"
            % (len(blocks), blocks[0][1], blocks[0][1] + len(data), len(data), fmt,
               args.output))
    return os.EX_OK


def _cmd_ucb(args):
    if args.transport != "dap":
        return _todo("ucb over %s" % args.transport)
    action = args.action or "decode"
    chip, error = _resolve_chip(args)
    if error:
        return _error(error, os.EX_USAGE)
    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)
    with dap:
        try:
            id_addr, chipid = _unlock_if_locked(dap, args, chip)
            if chip is None:
                chip = by_chipid(chipid, id_addr)
            if chip is None:
                raise Fail("unknown target; pass -c/--chip or --device", os.EX_USAGE)
            if action == "export":
                return _export_bmhds(dap, chip, args)
            ucbs = _read_ucbs(dap, chip)
        except Fail as f:
            return _error(f, f.code)

        if args.json:
            print(json.dumps([{"name": u["name"], "addr": "0x%08X" % u["addr"],
                               "state": u["state"],
                               "bmhd": None if u["bmhd"] is None else
                               {"bmi": "0x%04X" % u["bmhd"]["bmi"],
                                "hwcfg": u["bmhd"].get("hwcfg"),
                                "stad": "0x%08X" % u["bmhd"]["stad"],
                                "valid": u["bmhd_ok"]}} for u in ucbs]))
        elif action == "decode":
            print("UCBs (%s):" % chip.name)
            for u in ucbs:
                print("  %-7s @ 0x%08X  %s" % (u["name"], u["addr"], u["state"]))
                if u["bmhd"] is not None:
                    b = u["bmhd"]
                    mode = ("hwcfg=0x%X" % b["hwcfg"]) if "hwcfg" in b else ("bmi=0x%04X" % b["bmi"])
                    print("          BMHD %s stad=0x%08X crc=%s"
                          % (mode, b["stad"], "ok" if u["bmhd_ok"] else "BAD"))
                    for problem in u["bmhd_problems"]:
                        print("          ! %s" % problem)

        if action == "verify":
            problems = []
            for u in ucbs:
                if u["confirm"] is not None and u["confirm"] not in _CONFIRM:
                    problems.append("%s confirm word 0x%08X unrecognized"
                                    % (u["name"], u["confirm"]))
                if u["bmhd"] is not None and not u["bmhd_ok"]:
                    problems.append("%s: %s" % (u["name"], "; ".join(u["bmhd_problems"])))
            if problems:
                for p in problems:
                    log("tcprog: %s" % p)
                return _error("%d UCB problem(s)" % len(problems), EX_VERIFY)
            if not args.quiet:
                log("tcprog: UCBs OK")
    return os.EX_OK


# ======================================================================
# read
# ======================================================================
def _cmd_read(args):
    import time
    if args.transport != "dap":
        return _todo("read over %s" % args.transport)

    chip, error = _resolve_chip(args)
    if error:
        return _error(error, os.EX_USAGE)

    # range: explicit --addr/--length, or a whole --region on a known chip
    addr, length = args.addr, args.length
    if addr is None or length is None:
        if args.region and chip:
            seg = chip.first(args.region)
            if seg is None:
                return _error("%s has no %s region" % (chip.name, args.region), os.EX_USAGE)
            addr = seg.base if addr is None else addr
            length = seg.size if length is None else length
        else:
            return _error("read needs --addr and --length (or --region with -c/--chip)",
                         os.EX_USAGE)
    if length <= 0:
        return _error("--length must be positive", os.EX_USAGE)
    fmt = args.format or _out_format(args.output)

    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)

    with dap:
        try:
            id_addr, chipid = _unlock_if_locked(dap, args, chip)
        except Fail as f:
            return _error(f, f.code)

        if chip is None:
            chip = by_chipid(chipid, id_addr)
        if chip is not None and not args.quiet:
            if not (chip.slice_of(addr) and chip.slice_of(addr + length - 1)):
                sys.stderr.write("tcprog: warning - [0x%08X,0x%08X) is outside a known "
                                 "memory slice; reading erased flash raises an ECC error "
                                 "and can wedge the target.\n" % (addr, addr + length))

        if args.skip_erased:
            if fmt == "bin":
                return _error("--skip-erased needs a sparse container; use "
                              "--format srec or hex (bin cannot hold the gaps)",
                              os.EX_USAGE)
            total_kib = (((addr + length + Dap.BLOCK - 1) & ~(Dap.BLOCK - 1))
                         - (addr & ~(Dap.BLOCK - 1))) // 1024

            def progress(done, total, kept):
                if not args.quiet and not args.no_progress:
                    sys.stderr.write("\r  scan %d/%d KiB  kept %d KiB"
                                     % (done, total, kept // 1024))
                    sys.stderr.flush()

            # On TC3xx/TC4 PFLASH, map occupancy with the hardware blank-check and
            # read only the programmed sectors - the erased bulk is never read,
            # so there is no per-block reconnect over empty flash. Elsewhere
            # (TC2xx, non-PFLASH) fall back to the plain block scan.
            use_bc = (chip is not None and chip.family in ("tc3xx", "tc4xx")
                      and _is_pflash(addr))
            started = time.time()
            try:
                if use_bc:
                    def on_map(done, total):
                        if not args.quiet and not args.no_progress:
                            sys.stderr.write("\r  blank-check %d/%d sectors" % (done, total))
                            sys.stderr.flush()
                    segments, missed, unreadable = read_region_blankcheck(
                        dap, addr, length, on_map=on_map, family=chip.family)
                else:
                    segments, missed = read_region_sparse(
                        dap, addr, length, on_progress=progress)
                    unreadable = []
            except DapError as error:
                sys.stderr.write("\ntcprog: sparse read failed: %s\n" % error)
                return os.EX_IOERR
            if not args.quiet and not args.no_progress:
                sys.stderr.write("\n")
            elapsed = time.time() - started
            data_len = sum(len(d) for _, d in segments)
        else:
            block_size = Dap.BLOCK
            start = addr & ~(block_size - 1)
            end = (addr + length + block_size - 1) & ~(block_size - 1)
            block_addrs = list(range(start, end, block_size))
            buffer, group_size = bytearray(), 16
            started = time.time()
            try:
                for index in range(0, len(block_addrs), group_size):
                    buffer += dap.read_blocks(block_addrs[index:index + group_size])
                    if not args.quiet and not args.no_progress:
                        sys.stderr.write("\r  read %d/%d KiB"
                                         % (len(buffer) // 1024, (end - start) // 1024))
                        sys.stderr.flush()
            except DapError as error:
                if not args.quiet and not args.no_progress:
                    sys.stderr.write("\n")
                sys.stderr.write("tcprog: read failed near 0x%08X: %s\n"
                                 % (start + len(buffer), error))
                return os.EX_IOERR
            if not args.quiet and not args.no_progress:
                sys.stderr.write("\n")
            elapsed = time.time() - started
            data = bytes(buffer[addr - start: addr - start + length])

    if args.skip_erased:
        binfile = bincopy.BinFile()
        for seg_addr, seg_data in segments:
            binfile.add_binary(seg_data, address=seg_addr)
        payload = (binfile.as_ihex() if fmt == "hex" else binfile.as_srec())
        if args.output:
            try:
                with open(args.output, "w") as handle:
                    handle.write(payload)
            except OSError as error:
                return _error(error, os.EX_CANTCREAT)
        else:
            for seg_addr, seg_data in segments:
                _hexdump(seg_data, seg_addr)
        throughput = (data_len / 1024.0) / elapsed if elapsed > 0 else 0.0
        if not args.quiet:
            for seg_addr, seg_data in segments:
                sys.stderr.write("  segment 0x%08X .. 0x%08X (%d KiB)\n"
                                 % (seg_addr, seg_addr + len(seg_data), len(seg_data) // 1024))
            sys.stderr.write("tcprog: sparse read of [0x%08X,0x%08X): %d KiB kept in "
                             "%d segment(s), %d block(s) erased (%.1f KiB/s)%s\n"
                             % (addr, addr + length, data_len // 1024, len(segments),
                                missed, throughput,
                                " -> " + args.output if args.output else ""))
            for lo, hi in unreadable:
                sys.stderr.write("tcprog: warning - 0x%08X..0x%08X blank-checks as "
                                 "programmed but did not read back (uncorrectable ECC?); "
                                 "omitted from the image\n" % (lo, hi))
        return os.EX_OK

    try:
        payload = _emit(data, addr, fmt)
    except (ImportError, ValueError) as error:
        return _error(error, os.EX_DATAERR)

    if args.output:
        mode = "wb" if isinstance(payload, bytes) else "w"
        try:
            with open(args.output, mode) as handle:
                handle.write(payload)
        except OSError as error:
            return _error(error, os.EX_CANTCREAT)
    elif fmt == "bin":
        _hexdump(data, addr)
    else:
        sys.stdout.write(payload.decode() if isinstance(payload, bytes) else payload)

    throughput = (len(data) / 1024.0) / elapsed if elapsed > 0 else 0.0
    if args.json:
        print(json.dumps({"chip": chip.name if chip else "unknown",
                          "addr": "0x%08X" % addr, "length": len(data),
                          "format": fmt, "output": args.output or "-",
                          "kib_per_s": round(throughput, 1)}))
    elif not args.quiet:
        sys.stderr.write("tcprog: read %d bytes from 0x%08X (%.1f KiB/s)%s\n"
                         % (len(data), addr, throughput,
                            " -> " + args.output if args.output else ""))
    return os.EX_OK


# ======================================================================
# write / erase
# ======================================================================
def _cmd_write(args):
    if args.transport != "dap":
        return _todo("write over %s" % args.transport)
    if not args.firmware:
        return _error("no firmware image given (see `tcprog write --help`)", os.EX_USAGE)
    chip, error = _resolve_chip(args)
    if error:
        return _error(error, os.EX_USAGE)
    # A write is gated on the target's memory map, so the part must be known up
    # front - tcprog does not guess region boundaries for a destructive op.
    if chip is None:
        return _error("write requires the target: pass -c/--chip (e.g. -c tc375) "
                      "or --device FILE", os.EX_USAGE)
    try:
        image = load_image(args.firmware, fmt=args.format, address=args.address)
    except (ValueError, OSError) as error:
        return _error(error, os.EX_DATAERR)

    if args.dry_run:
        return _dry_run_plan(image, args, chip)

    # ALLOWLIST: every segment must sit inside one of the device's memory slices.
    # A custom --device file with only the permitted slices thus confines a write
    # to those regions and refuses anything else.
    outside = [segment for segment in image.segments
               if not any(region.base <= segment.addr and segment.end <= region.end
                          for region in chip.memory)]
    if outside:
        return _error("REFUSED - %d segment(s) fall outside %s's memory slices (%s). "
                     "Not writing outside the device map."
                     % (len(outside), chip.name,
                        ", ".join("0x%08X..0x%08X" % (s.addr, s.end) for s in outside)),
                     EX_REFUSED)

    # SAFETY GATE: refuse writes that touch any UCB region unless enabled.
    ucb_segments = [s for s in image.segments if chip.region_of(s.addr) == "ucb"]
    if ucb_segments and not args.enable_ucb_write:
        return _error("REFUSED - image writes to the UCB region (%s) and --enable-ucb-write "
                     "is not set. UCB writes are IRREVERSIBLE and can brick the part."
                     % ", ".join("0x%08X" % s.addr for s in ucb_segments), EX_REFUSED)

    # Confirm before touching flash (destructive, irreversible).
    span_lo, span_hi = image.span
    if not args.yes:
        sys.stderr.write("tcprog: about to %sprogram %d bytes to %s at 0x%08X..0x%08X.\n"
                         "  This ERASES and rewrites flash and cannot be undone.\n"
                         % ("erase + " if args.erase == "sectors" else "",
                            image.total_bytes, chip.name, span_lo, span_hi))
        try:
            if input("  continue? [y/N] ").strip().lower() not in ("y", "yes"):
                return _error("aborted", EX_REFUSED)
        except EOFError:
            return _error("aborted (no tty for confirmation; pass --yes)", EX_REFUSED)

    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)
    with dap:
        try:
            _unlock_if_locked(dap, args, chip)
        except Fail as f:
            return _error(f, f.code)
        try:
            fam = chip.family if chip.family == "tc4xx" else "tc3xx"
            return _program(dap, image, args, family=fam)
        except DapError as error:
            return _error("flash operation failed: %s" % error, os.EX_IOERR)


def _cmd_erase(args):
    if args.transport != "dap":
        return _todo("erase over %s" % args.transport)
    chip, error = _resolve_chip(args)
    if error:
        return _error(error, os.EX_USAGE)
    if chip is None:
        return _error("erase requires the target: pass -c/--chip or --device", os.EX_USAGE)

    addr, length, whole = args.addr, args.length, False
    if args.region:
        seg = chip.first(args.region)
        if seg is None:
            return _error("%s has no %s region" % (chip.name, args.region), os.EX_USAGE)
        addr, length, whole = seg.base, seg.size, True
    if addr is None or length is None:
        return _error("erase needs --addr and --length, or --region", os.EX_USAGE)
    fam = chip.family if chip.family == "tc4xx" else "tc3xx"
    sector = _sector_size(addr, profile_for_family(fam))
    if sector is None:
        return _error("erase supports PFLASH and DFLASH only", os.EX_USAGE)
    if whole and not args.allow_erase_all:
        return _error("erasing the whole %s region needs --allow-erase-all" % args.region,
                      EX_REFUSED)

    first, count = _flash_sectors(addr, length, sector)
    end = first + count * sector
    if not args.yes:
        sys.stderr.write("tcprog: about to ERASE %d sector(s), 0x%08X..0x%08X on %s.\n"
                         "  This cannot be undone.\n" % (count, first, end, chip.name))
        try:
            if input("  continue? [y/N] ").strip().lower() not in ("y", "yes"):
                return _error("aborted", EX_REFUSED)
        except EOFError:
            return _error("aborted (no tty for confirmation; pass --yes)", EX_REFUSED)

    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)
    with dap:
        try:
            _unlock_if_locked(dap, args, chip)
        except Fail as f:
            return _error(f, f.code)
        try:
            flash = Flash(dap, fam)
            flash.clear_status()
            log("tcprog: erasing %d sector(s) from 0x%08X..." % (count, first), verbose=True)
            flash.erase_sectors(first, count)
            flash._wait_cmd_done(addr)
            flash.reset_to_read()
        except DapError as error:
            return _error("erase failed: %s" % error, os.EX_IOERR)
    if not args.quiet:
        log("tcprog: erased %d sector(s) (0x%08X..0x%08X)" % (count, first, end))
    return os.EX_OK


def _cmd_blankcheck(args):
    if args.transport != "dap":
        return _todo("blankcheck over %s" % args.transport)
    chip, error = _resolve_chip(args)
    if error:
        return _error(error, os.EX_USAGE)
    if chip is None:
        return _error("blankcheck requires the target: pass -c/--chip or --device",
                      os.EX_USAGE)
    # Covered: TC3xx (2G DMU HF_*) and TC4 (TC4Dx HCI_*). TC2xx has a different
    # FLASH0 FSR layout and is not handled, so refuse rather than drive its flash.
    if chip.family not in ("tc3xx", "tc4xx"):
        return _error("blankcheck covers TC3xx and TC4 (the verify-error decode and "
                      "FSI register map are DMU-specific; %s is %s)"
                      % (chip.name, chip.family), os.EX_UNAVAILABLE)

    addr, length = args.addr, args.length
    if args.region:
        seg = chip.first(args.region)
        if seg is None:
            return _error("%s has no %s region" % (chip.name, args.region), os.EX_USAGE)
        addr = seg.base if addr is None else addr
        length = seg.size if length is None else length
    if addr is None or length is None:
        return _error("blankcheck needs --addr and --length, or --region pflash", os.EX_USAGE)
    if not _is_pflash(addr):
        return _error("blankcheck supports PFLASH only", os.EX_USAGE)

    try:
        dap = _open_dap(args)
    except Fail as f:
        return _error(f, f.code)
    with dap:
        try:
            id_addr, chipid = _unlock_if_locked(dap, args, chip)
        except Fail as f:
            return _error(f, f.code)
        # Cross-check the silicon before driving its flash: the FSI sequence must
        # never land on a part that only matched because -c said so. The detected
        # family also picks the register profile, so it must agree with -c.
        detected = by_chipid(chipid, id_addr)
        if detected is not None and detected.family not in ("tc3xx", "tc4xx"):
            return _error("refusing to blank-check: connected target is %s (%s), not a "
                          "TC3xx/TC4 part" % (detected.name, detected.family), EX_REFUSED)
        if detected is not None and detected.family != chip.family:
            return _error("refusing to blank-check: connected target is %s (%s) but -c "
                          "says %s (%s); the FSI register map differs per family"
                          % (detected.name, detected.family, chip.name, chip.family),
                          EX_REFUSED)
        if detected is None:
            return _error("refusing to blank-check: cannot confirm the connected part "
                          "(chipid 0x%08X unrecognised)" % (chipid or 0), EX_REFUSED)

        def progress(done, total, sa, blank):
            if not args.quiet and not args.no_progress:
                sys.stderr.write("\r  blank-check %d/%d sectors" % (done, total))
                sys.stderr.flush()

        try:
            sectors = blank_map(dap, addr, length, on_progress=progress,
                                family=chip.family)
        except DapError as error:
            if not args.quiet and not args.no_progress:
                sys.stderr.write("\n")
            return _error("blankcheck failed: %s" % error, os.EX_IOERR)
        if not args.quiet and not args.no_progress:
            sys.stderr.write("\n")

    # merge contiguous programmed sectors into ranges
    prog = [sa for sa, blank in sectors if not blank]
    ranges, step = [], PFLASH_LOG_SECTOR
    for sa in prog:
        if ranges and ranges[-1][1] == sa:
            ranges[-1][1] = sa + step
        else:
            ranges.append([sa, sa + step])
    prog_bytes = len(prog) * step

    if args.json:
        print(json.dumps({"chip": chip.name, "addr": "0x%08X" % addr, "length": length,
                          "sector": step, "sectors": len(sectors),
                          "programmed_sectors": len(prog),
                          "programmed": [{"addr": "0x%08X" % a, "end": "0x%08X" % b}
                                         for a, b in ranges]}))
    else:
        for a, b in ranges:
            print("  programmed  0x%08X .. 0x%08X  (%d KiB)" % (a, b, (b - a) // 1024))
        if not ranges:
            print("  (all %d sectors blank)" % len(sectors))
        if not args.quiet:
            sys.stderr.write("tcprog: %d/%d sectors programmed (%d KiB) in [0x%08X,0x%08X)\n"
                             % (len(prog), len(sectors), prog_bytes // 1024,
                                addr, addr + length))
    return os.EX_OK


def _dry_run_plan(image, args, chip):
    region = chip.region_of
    touches_ucb = any(region(seg.addr) == "ucb" for seg in image.segments)
    in_gap = [seg for seg in image.segments if region(seg.addr) == "?"]
    if args.json:
        print(json.dumps({
            "dry_run": True, "image": os.path.basename(args.firmware),
            "loader": image.loader, "start": image.start, "total_bytes": image.total_bytes,
            "segments": [{"addr": seg.addr, "len": len(seg.data), "region": region(seg.addr)}
                         for seg in image.segments],
            "transport": args.transport, "erase": args.erase,
            "verify": not args.no_verify, "touches_ucb": touches_ucb,
            "in_gap": bool(in_gap)}))
        return os.EX_OK
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
    print("  transport %s   erase %s   verify %s"
          % (args.transport, args.erase, "no" if args.no_verify else "yes"))
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
    return args.func(args)
