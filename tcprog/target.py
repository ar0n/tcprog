# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""The connection to the part: open the link, read its identity, unlock debug,
detect it, halt CPU0 for flash work, lift flash protection, reset afterwards.

The link is the DAP over the probe's FTDI chip. The flash drivers, flash reads
and UCB reads use only its memory calls (read_word, try_read_word,
read_word_retry, read_block, read_blocks, write_word, connect); opening the
link, debug unlock, halt-after-reset and the reset afterwards are the
transport-specific steps, and they all live here."""
import glob
import os

from . import preflight
from .chips import by_id, id_addrs, load_device_file, lookup_chip
from .dap import Dap, DapError, parse_password, parse_probe, _locked, _unlock_failed_msg
from .flash import driven_layout, drives, flash_driver, identity_kind
from .util import Fail, log, warn


def resolve_chip(args):
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


# USB IDs of the probes tcprog drives: FTDI MPSSE chips and the Infineon DAS
# probes (miniWiggler, the lite kits' on-board debuggers).
PROBE_KINDS = {
    (0x0403, 0x6010): "Generic FT2232 (MPSSE)",
    (0x0403, 0x6011): "Generic FT4232 (MPSSE)",
    (0x0403, 0x6014): "Generic FT232H (MPSSE)",
    (0x058b, 0x0043): "Infineon DAS (miniWiggler / on-board)",
}


def find_probes():
    """The attached probes of PROBE_KINDS, from Linux sysfs (empty elsewhere).
    -> [{vid, pid, serial, product, kind, access}], access False when this user
    may not open the USB device (no udev rule)."""
    def attr(dev_dir, name):
        try:
            with open(dev_dir + name) as handle:
                return handle.read().strip()
        except OSError:
            return None

    probes = []
    for dev_dir in sorted(glob.glob("/sys/bus/usb/devices/*/")):
        vendor, product = attr(dev_dir, "idVendor"), attr(dev_dir, "idProduct")
        if not vendor or not product:
            continue
        vid, pid = int(vendor, 16), int(product, 16)
        if (vid, pid) not in PROBE_KINDS:
            continue
        bus, dev = attr(dev_dir, "busnum"), attr(dev_dir, "devnum")
        node = "/dev/bus/usb/%03d/%03d" % (int(bus), int(dev)) if bus and dev else None
        probes.append({"vid": vid, "pid": pid, "serial": attr(dev_dir, "serial"),
                       "product": attr(dev_dir, "product"), "kind": PROBE_KINDS[(vid, pid)],
                       "access": node is None or os.access(node, os.R_OK | os.W_OK)})
    return probes


def open_link(args):
    """Open the probe and bring the DAP link up. -> Dap (caller uses `with`).
    Without a serial, more than one matching probe is refused rather than
    opening whichever comes first. Raises Fail on any error, with the right
    exit code."""
    vid, pid, serial = parse_probe(args.probe)
    matching = [p for p in find_probes() if (p["vid"], p["pid"]) == (vid, pid)
                and (serial is None or p["serial"] == serial)]
    if serial is None and len(matching) > 1:
        raise Fail("%d probes match %04x:%04x (%s); pass -p %04x:%04x:SERIAL"
                   % (len(matching), vid, pid, ", ".join(p["serial"] or "?" for p in matching),
                      vid, pid), os.EX_USAGE)
    try:
        dap = Dap(serial, vid, pid, args.speed * 1000,
                  reset_on_open=args.connect_under_reset)
    except DapError as error:
        if any(not p["access"] for p in matching):
            raise Fail("%s; no access to the probe's USB device, install the udev rule "
                       "udev/60-tcprog.rules" % error, os.EX_NOPERM)
        raise Fail(str(error), os.EX_UNAVAILABLE)
    except ImportError:
        raise Fail("the DAP needs the 'ftdi1' (libftdi1) Python binding", os.EX_UNAVAILABLE)
    if not dap.connect():
        dap.close()
        raise Fail("target did not answer; check the cable and power, and that the part "
                   "is not strapped to BSL", os.EX_UNAVAILABLE)
    return dap


def read_identity(dap, chip=None):
    """Read the identity register: the known chip's first, then each other one in
    the DB (SCU_CHIPID on TC2xx/TC3xx, CBS_JTAGID on TC4xx), so a part of another
    family than -c names is still identified instead of looking debug-locked.
    -> (addr, value), value None if none answered (then addr is the first
    register tried)."""
    addrs = id_addrs()
    if chip is not None and chip.id_addr:
        addrs = [chip.id_addr] + [addr for addr in addrs if addr != chip.id_addr]
    for addr in addrs:
        value = dap.read_word_retry(addr)
        if not _locked(value):
            return addr, value
    return addrs[0], None


def unlock_if_locked(dap, args, chip=None, halt=False, spans=None):
    """Read the identity register; if the part is debug-locked, unlock with
    --debug-unlock-pw; warn if the part is detected as another database entry
    than the named one; for a flash operation (`spans` given: the (lo, hi) it
    touches) refuse unless the silicon's family matches the chip and every span
    lies in the memory map of this exact variant; optionally halt CPU0 at reset
    (halt=True); then disable flash protection if asked. -> (id_addr,
    id_value). Raises Fail (locked=True when no password was given)."""
    identity = read_identity(dap, chip)
    addr, chipid = debug_unlock(dap, args, chip, identity)
    if chip is not None:
        warn_other_part(args, chip, addr, chipid)
    if spans is not None:
        preflight.confirm_family(chip, addr, chipid, named_part(args))
        preflight.confirm_variant(chip.for_identity(addr, chipid), spans)
    if halt:
        halt_cpu(dap, chip, _locked(identity[1]))
    disable_flash_protection(dap, args, chip)
    return addr, chipid


def debug_unlock(dap, args, chip, identity):
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
    ok, identity, tries = dap.unlock(words, args.unlock_tries, on_attempt=log_attempt)
    if not ok:
        raise Fail(_unlock_failed_msg(dap, tries), os.EX_NOPERM)
    log("tcprog: unlocked after %d attempt%s" % (tries, "" if tries == 1 else "s"), verbose=True)
    return identity


def warn_other_part(args, chip, id_addr, chipid):
    """Warn when the connected part is detected as another database entry than
    the one -c or --device names; the named one is still used."""
    found = None if chipid is None else by_id(chipid, id_addr)
    if found is None or found.name == chip.name:
        return
    warn("%s but the connected part is detected as %s (%s 0x%08X)"
         % (named_part(args), found.name, identity_kind(id_addr).upper(), chipid))


def named_part(args):
    """The part as the user named it: "-c tc4d7" or "--device prod.toml"."""
    if getattr(args, "device", None):
        return "--device %s" % args.device
    return "-c %s" % args.chip


def log_attempt(k, reached, error):
    """dap.unlock(on_attempt=...) callback: log each try under -v."""
    if error is not None:
        log("  attempt %d: error: %s" % (k, error), verbose=True)
    else:
        log("  attempt %d: %s" % (k, "reached dialogue" if reached else "missed window"),
            verbose=True)


def detected(id_addr, chipid, verbose=True):
    """The database entry for the connected part, as its identity register
    describes it (reduced-flash variant included), logged. -> Chip or None."""
    chip = by_id(chipid, id_addr)
    if chip is not None:
        chip = chip.for_identity(id_addr, chipid)
        log("tcprog: detected %s (%s 0x%08X, PFLASH %d KiB)"
            % (chip.name, identity_kind(id_addr).upper(), chipid,
               sum(r.size for r in chip.noncached_ranges("pflash")) // 1024),
            verbose=verbose)
    return chip


def detect_chip(dap, args):
    """-c auto: identify the connected part (unlocking debug if needed). Raises
    Fail if the database does not recognise it."""
    id_addr, chipid = debug_unlock(dap, args, None, read_identity(dap))
    chip = detected(id_addr, chipid, verbose=False)
    if chip is None:
        raise Fail("cannot detect the connected part (identity 0x%08X at 0x%08X); pass "
                   "-c/--chip" % (chipid, id_addr), os.EX_USAGE)
    return chip


def halt_cpu(dap, chip, was_locked):
    """TC3xx: PFLASH program/erase can be refused (PROER) once the application
    has started, so reset with Halt-After-Reset and keep CPU0 at its first
    instruction during the flash operation."""
    if was_locked:
        warn("not halting CPU0: the part was debug-locked and a halt-after-reset would lock "
             "it again; PFLASH program/erase may be refused while the application runs")
        return
    pc = dap.halt_after_reset(driven_layout(chip.family).cpu0_pc)
    if pc is None:
        warn("could not halt CPU0 at reset; PFLASH program/erase may be refused while the "
             "application runs")
    else:
        log("tcprog: CPU0 halted at reset (PC 0x%08X)" % pc, verbose=True)


def after(dap, args, halted):
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


def disable_flash_protection(dap, args, chip):
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
    flash = flash_driver(dap, chip)
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
