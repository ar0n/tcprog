# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""The UCBs and boot mode headers the startup software evaluates, read and judged
per family, and the TC3xx next boot validation. Read-only."""
import os

from .flash import LAYOUT_TC2XX, LAYOUT_TC3XX, LAYOUT_TC4XX, driven_layout
from .bmhd import (parse_bmhd_tc2, parse_bmhd_tc3, parse_bmhd_tc4,
                   validate_bmhd_tc2, validate_bmhd_tc3, validate_bmhd_tc4)
from .util import Fail

# 0x43211234 is UNLOCKED on all three families; any value other than
# UNLOCKED/CONFIRMED, erased included, is ERRORED.
_UNLOCKED, _CONFIRMED, _ERASED = 0x43211234, 0x57B5327F, 0x00000000

def ucb_name(chip, addr):
    """UCB_<name> for an address in one of the chip's UCB windows."""
    layout = driven_layout(chip.family)
    for window, (base, size) in enumerate(layout.ucb_windows):
        if base <= addr < base + size:
            index = (addr - base) // layout.ucb_size
            return "UCB_" + layout.ucb_names.get(
                (window, index), layout.ucb_unnamed.format(window=window, index=index))
    return "UCB_?"


def _ucb_base(chip):
    """Base of the chip's first UCB range, where UCB index 0 sits."""
    ucb = chip.first("ucb")
    if ucb is None:
        raise Fail("no UCB region known for this target; pass -c/--chip or --device",
                   os.EX_USAGE)
    return ucb.base


def _read_header(dap, addr, size):
    """`size` bytes from addr, word by word; a word that does not answer reads
    as 0, so the header checks report it instead of the read failing."""
    return b"".join((dap.try_read_word(addr + off) or 0).to_bytes(4, "little")
                    for off in range(0, size, 4))


def bmhd_blocks(chip):
    """The BMHD blocks tcprog knows for this chip, in address order.
    -> [(name, addr, size)]. TC4xx: the four BMHD UCBs; TC3xx: the four BMHD
    ORIG UCBs, then their four COPYs; TC2xx: BMHD0 in PFLASH."""
    if chip.family == "tc4xx":
        layout = LAYOUT_TC4XX
        base = layout.ucb_windows[0][0]
        return [(name, base + index * layout.ucb_size, layout.ucb_size)
                for name, index in layout.ucbs.items()]
    if chip.family == "tc2xx":
        addr, size = LAYOUT_TC2XX.pflash_bmhd
        return [("bmhd0", addr, size)]
    layout = LAYOUT_TC3XX
    base = _ucb_base(chip)
    names = [name for name in layout.ucbs if name.startswith("bmhd")]
    return ([(name + "_orig", base + layout.ucbs[name] * layout.ucb_size, layout.ucb_size)
             for name in names] +
            [(name + "_copy", base + layout.ucbs[name] * layout.ucb_size + layout.copy_off,
              layout.ucb_size) for name in names])


def _confirm_state(word):
    """UNLOCKED / CONFIRMED, else ERRORED (the erased value 0 included)."""
    if word == _UNLOCKED:
        return "UNLOCKED"
    if word == _CONFIRMED:
        return "CONFIRMED"
    return "ERRORED"


def confirm_unknown(word):
    """True for a confirmation word that is neither UNLOCKED, CONFIRMED nor erased."""
    return _confirm_state(word) == "ERRORED" and word != _ERASED


def _orig_copy(layout, block):
    """{"orig"/"copy": (UCB address, confirmation word address)} for the UCB
    whose ORIG is at `block`."""
    copy = block + layout.copy_off
    return {"orig": (block, block + layout.confirm_off),
            "copy": (copy, copy + layout.copy_confirm_off)}


def _source(states):
    """The confirmation in use: ORIG if UNLOCKED or CONFIRMED, else COPY if so,
    else None."""
    return next((which for which in ("orig", "copy")
                 if states[which].startswith(("UNLOCKED", "CONFIRMED"))), None)


def _mark_boot(bmhds, fallback):
    """Mark the first BMHD, in order 0..3, whose UCB and header are valid as
    "boot"; with none, the startup code does `fallback`, a problem on BMHD0."""
    boot = next((u for u in bmhds if u["bmhd"] is not None and u["bmhd_ok"]), None)
    if boot is not None:
        boot["boot"] = True
    else:
        bmhds[0]["problems"].append("no valid boot mode header in BMHD0..3: the startup "
                                    + fallback)


def _read_ucbs_tc4(dap, chip):
    """TC4xx: the four BMHD UCBs (UCB_RTC_BMHD0..3) as the startup firmware
    evaluates them. Each is a single UCB with an ORIG and a COPY confirmation
    code: ORIG decides if it is UNLOCKED or CONFIRMED, else COPY does, else the
    UCB is ERRORED and skipped. The first BMHD in order 0..3 whose state and
    header are valid is marked "boot"; with none, the firmware falls back
    instead of starting the user code. Entries carry "problems" and "warnings"
    like the TC3xx ones."""
    out = []
    for name, block, _ in bmhd_blocks(chip):
        pair = _orig_copy(LAYOUT_TC4XX, block)
        codes, words = {}, {}
        for which in ("orig", "copy"):
            words[which] = dap.read_word_retry(pair[which][1])
            codes[which] = ("unreadable" if words[which] is None
                            else _confirm_state(words[which]))
        source = _source(codes)
        ucb = codes[source] if source else "ERRORED"
        entry = {"name": name, "addr": block,
                 "state": "ORIG %s, COPY %s -> %s" % (codes["orig"], codes["copy"], ucb),
                 "orig": codes["orig"], "copy": codes["copy"], "orig_word": words["orig"],
                 "copy_word": words["copy"], "source": source,
                 "bmhd": None, "problems": [], "warnings": []}
        if source is None:
            entry["state"] += " (not evaluated)"
        else:
            if source == "copy":
                entry["warnings"].append("UCB_RTC_%s ORIG confirmation code is %s; the COPY "
                                         "code is in use, reprogram the UCB"
                                         % (name.upper(), codes["orig"]))
            header = _read_header(dap, pair[source][0], 16)
            entry["bmhd"] = parse_bmhd_tc4(header)
            entry["bmhd_ok"], entry["bmhd_problems"] = validate_bmhd_tc4(header, block, chip)
        out.append(entry)
    _mark_boot(out, "firmware falls back instead of starting the user code")
    return out


def _read_ucbs_tc2(dap, chip):
    """TC2xx (AURIX 1G): UCB0/1/5 confirmation words, and the 32-byte BMHD0 from
    PFLASH (STADABM | BMI|BMHDID | range | CRC)."""
    layout = LAYOUT_TC2XX
    base = _ucb_base(chip)
    bmhd_addr, bmhd_size = layout.pflash_bmhd
    header = _read_header(dap, bmhd_addr, bmhd_size)
    ok, problems = validate_bmhd_tc2(header, chip)
    out = [{"name": "bmhd0", "addr": bmhd_addr,
            "state": "in PFLASH (no confirmation)", "orig": None, "copy": None,
            "orig_word": None, "copy_word": None, "source": None,
            "bmhd": parse_bmhd_tc2(header), "bmhd_ok": ok, "bmhd_problems": problems,
            "problems": [] if ok else ["bmhd0: %s" % "; ".join(problems)]}]
    for name, index in layout.ucbs.items():
        block = base + index * layout.ucb_size
        confirm = dap.try_read_word(block + layout.confirm_off)
        # A confirmed UCB is read-protected, but an ECC error also leaves the
        # word unreadable, so a missing answer is not proof of either.
        if confirm is None:
            state = "unreadable (read-protected or ECC error)"
        elif confirm_unknown(confirm):
            state = "0x%08X (unknown)" % confirm
        elif confirm == _ERASED:
            state = "ERASED (errored)"
        else:
            state = _confirm_state(confirm)
        out.append({"name": name, "addr": block, "state": state,
                    "orig": state, "copy": None, "orig_word": confirm, "copy_word": None,
                    "source": _source({"orig": state, "copy": ""}), "bmhd": None,
                    "problems": (["%s confirm word 0x%08X unrecognized" % (name, confirm)]
                                 if confirm is not None and confirm_unknown(confirm)
                                 else [])})
    return out


# TC3xx confirmation states installed at the last reset, 2-bit PROIN fields:
# HF_CONFIRM0 holds BMHDx ORIG at bit 2x and COPY at 16 + 2x; HF_CONFIRM1 holds
# PFLASH, DFLASH and DBG ORIG at bits 0/2/4 and COPY at 16/18/20.
HF_CONFIRM0, HF_CONFIRM1 = 0xF8040020, 0xF8040024
_PROIN = ("UNREAD", "UNLOCKED", "CONFIRMED", "ERRORED")
# Per pair: (confirm register, ORIG field bit). A CONFIRMED PFLASH/DFLASH/DBG
# UCB is read-protected; BMHD UCBs stay readable.
_TC3_PROIN = {"bmhd0": (HF_CONFIRM0, 0), "bmhd1": (HF_CONFIRM0, 2),
              "bmhd2": (HF_CONFIRM0, 4), "bmhd3": (HF_CONFIRM0, 6),
              "pflash": (HF_CONFIRM1, 0), "dflash": (HF_CONFIRM1, 2), "dbg": (HF_CONFIRM1, 4)}


def _try_read(dap, addr):
    """The word at addr, or None if it does not answer or the address is unknown."""
    return None if addr is None else dap.try_read_word(addr)


def _tc3_pair(dap, ucb_base, name, live):
    """Judge one ORIG/COPY UCB pair the way the DMU installs it at the next
    reset: ORIG if UNLOCKED or CONFIRMED, else COPY if so, else ERRORED.

    A confirmation word that does not answer is CONFIRMED (read-protected) when
    the live HF_CONFIRM field says so and protection is not disabled; otherwise
    it is unreadable, which the DMU treats like ERRORED (uncorrectable ECC).
    -> dict with orig/copy states, source ("orig", "copy", None) and its address."""
    reg, bit = _TC3_PROIN[name]
    prodis = LAYOUT_TC3XX.prodis.get(name)
    orig = ucb_base + LAYOUT_TC3XX.ucbs[name] * LAYOUT_TC3XX.ucb_size
    pair = _orig_copy(LAYOUT_TC3XX, orig)
    out = {"name": name, "addr": orig}

    def field(shift):
        """The live 2-bit PROIN state at `shift` of the confirm register, or None."""
        if live[reg] is None:
            return None
        return _PROIN[(live[reg] >> shift) & 3]

    # Protection covers ORIG and COPY together. While the ORIG is valid the
    # COPY is never evaluated and its field stays UNREAD, so the ORIG decides.
    protected = (prodis is not None and "CONFIRMED" in (field(bit), field(bit + 16))
                 and live["protect"] is not None and not live["protect"] & (1 << prodis))
    for which, shift in (("orig", bit), ("copy", bit + 16)):
        word = dap.try_read_word(pair[which][1])
        if word is not None:
            state = _confirm_state(word)
        elif protected and field(shift) == "CONFIRMED":
            state = "CONFIRMED (read-protected)"
        elif protected:
            state = "read-protected"
        elif field(shift) in ("UNREAD", "ERRORED"):
            state = "unreadable"
        else:
            # The DMU installed this UCB at the last reset but the word does not
            # answer now (e.g. destructive debug entry); its state is unknown.
            state = "unreadable (live %s)" % (field(shift) or "state unknown")
        out[which], out[which + "_word"] = state, word
    out["source"] = _source(out)
    out["block"] = pair[out["source"]][0] if out["source"] else None
    return out


def _tc3_live(dap, chip):
    """The live HF_CONFIRM0/1 and the chip's hf_protect ("protect")."""
    return {HF_CONFIRM0: dap.try_read_word(HF_CONFIRM0),
            HF_CONFIRM1: dap.try_read_word(HF_CONFIRM1),
            "protect": _try_read(dap, chip.hf_protect)}


def _read_ucbs_tc3(dap, chip):
    """TC3xx: every ORIG/COPY pair the startup software evaluates (BMHD0..3,
    PFLASH, DFLASH, DBG), each with the BMHD header it would use. The first
    valid BMHD, in order 0..3, is marked "boot". Pairs carry "problems" (the
    part will not boot as configured) and "warnings"."""
    base = _ucb_base(chip)
    live = _tc3_live(dap, chip)
    out = []
    for name in LAYOUT_TC3XX.ucbs:
        entry = _tc3_pair(dap, base, name, live)
        state = "ORIG %s, COPY %s" % (entry["orig"], entry["copy"])
        problems, warnings = [], []
        if entry["source"] is None and any(
                entry[which] == "read-protected" or entry[which].startswith("unreadable (")
                for which in ("orig", "copy")):
            state += " -> unknown"
            warnings.append("UCB_%s cannot be checked: its confirmation words do not "
                            "answer" % entry["name"].upper())
        elif entry["source"] is None:
            state += " -> ERRORED"
            # ORIG and COPY both ERRORED: the SSW exits with an error and the
            # device does not boot.
            problems.append("UCB_%s ORIG and COPY are both errored or unreadable: the "
                            "device does not boot" % entry["name"].upper())
        else:
            state += " -> %s" % entry["source"].upper()
            if entry["source"] == "copy":
                warnings.append("UCB_%s ORIG is %s; the COPY is in use, reprogram the ORIG"
                                % (entry["name"].upper(), entry["orig"]))
        entry.update({"state": state, "bmhd": None,
                      "problems": problems, "warnings": warnings})
        if entry["name"].startswith("bmhd") and entry["block"] is not None:
            header = _read_header(dap, entry["block"], 16)
            entry["bmhd"] = parse_bmhd_tc3(header)
            entry["bmhd_ok"], entry["bmhd_problems"] = validate_bmhd_tc3(header, chip)
        out.append(entry)
    _mark_boot([u for u in out if u["name"].startswith("bmhd")],
               "software does not start user code")
    return out


# TC3xx next-boot check: (name, offset of the config word in the UCB, the
# chip attribute holding the live register). The word is read from whichever
# of ORIG/COPY _tc3_pair selects.
_TC3_NEXT_BOOT = (("pflash", 0x0C0, "proconpf"),
                  ("dflash", 0x004, "procondf"),
                  ("dbg", 0x000, "procondbg"))
_RPRO, _DF_L, _OCDSDIS, _DBGIFLCK = 1 << 31, 1 << 0, 1 << 0, 1 << 1


def next_boot_report(dap, chip, ucbs):
    """TC3xx: compare the protection the UCBs will install at the next reset with
    the live DMU registers, using the ORIG/COPY pairs read_ucbs judged. -> list
    of (level, message), level "ok" / "warn" / "lockout". Read-only. Empty for
    other families."""
    if chip.family != "tc3xx":
        return []
    pairs = {u["name"]: u for u in ucbs}
    nxt, live, notes = {}, {}, []
    for name, off, reg in _TC3_NEXT_BOOT:
        live[name] = _try_read(dap, getattr(chip, reg))
        entry = pairs[name]
        value = None
        if entry["source"] is None:
            notes.append(("warn", "UCB_%s has no readable valid ORIG or COPY; its next-boot "
                          "setting cannot be checked" % name.upper()))
        elif entry[entry["source"]].endswith("(read-protected)"):
            # A CONFIRMED UCB cannot change without its password, so the setting
            # installed at the last reset is what the next reset installs again.
            value = live[name]
            notes.append(("warn", "UCB_%s is CONFIRMED (read-protected); its current "
                          "setting stays at the next reset and is hard to undo"
                          % name.upper()))
        else:
            value = dap.try_read_word(entry["block"] + off)
        nxt[name] = value
    out = list(notes)
    fields = (("pflash", _RPRO, "PFLASH read protection"),
              ("dflash", _RPRO, "DFLASH read protection"),
              ("dflash", _DF_L, "DFLASH write protection"),
              ("dbg", _DBGIFLCK, "debug lock (DBGIFLCK)"),
              ("dbg", _OCDSDIS, "OCDS lock (OCDSDIS)"))
    for name, bit, label in fields:
        if nxt[name] is None or live[name] is None:
            continue
        now, then = bool(live[name] & bit), bool(nxt[name] & bit)
        if now != then:
            out.append(("warn", "%s changes at the next reset: %s -> %s"
                        % (label, "on" if now else "off", "on" if then else "off")))
    if nxt["dbg"] is not None and nxt["dbg"] & _OCDSDIS:
        out.append(("warn", "after the next reset OCDS is disabled (OCDSDIS): no breakpoints "
                    "or core halt without the UCB_DBG password, so TC3xx PFLASH write/erase "
                    "runs without halting CPU0"))
    def no_password_lock(state):
        """Read protection on and no debug password: debug stays locked."""
        return bool((state["pflash"] | state["dflash"]) & _RPRO
                    and not state["dbg"] & _DBGIFLCK)

    if None not in (nxt["pflash"], nxt["dflash"], nxt["dbg"]):
        rpro = (nxt["pflash"] | nxt["dflash"]) & _RPRO
        already = None not in (live["pflash"], live["dflash"], live["dbg"]) \
            and no_password_lock(live)
        if no_password_lock(nxt) and already:
            out.append(("warn", "flash read protection with no debug password is already in "
                        "effect: after a reset only application software can open debug, "
                        "not tcprog"))
        elif no_password_lock(nxt):
            out.append(("lockout", "after the next reset flash read protection keeps debug "
                        "locked and no debug password is configured: only application "
                        "software can open it again, not tcprog. Do not reset; undo the "
                        "UCB change first."))
        elif rpro:
            out.append(("warn", "after the next reset debug opens only with the UCB_DBG "
                        "password (--debug-unlock-pw)"))
    if not out:
        out.append(("ok", "no protection change pending at the next reset"))
    return out


def read_ucbs(dap, chip):
    """Read and judge the UCBs the startup software evaluates. -> list of dicts
    with the same keys on every family: name, addr, state, orig, copy,
    orig_word, copy_word, source, bmhd, bmhd_ok, bmhd_problems, boot, and
    "problems" (`ucb verify` fails on them) and "warnings". On TC4xx only the
    BMHD blocks are read."""
    if chip.family == "tc4xx":
        entries = _read_ucbs_tc4(dap, chip)
    elif chip.family == "tc2xx":
        entries = _read_ucbs_tc2(dap, chip)
    else:
        entries = _read_ucbs_tc3(dap, chip)
    for entry in entries:
        for key, empty in (("bmhd_ok", None), ("bmhd_problems", []), ("boot", False),
                           ("problems", []), ("warnings", [])):
            entry.setdefault(key, empty)
    return entries
