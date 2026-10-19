# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""Everything before the first flash command, in one place: the write and erase
plans (the logical sector ranges to erase) and every refusal: the family and
variant checks, the support and device-map check, the memory-map allowlist, the
UCB gate, the write- and erase-protected ranges, and the confirmation prompt.
`write --dry-run` runs the same plan and stops."""
import os
import sys

from .chips import all_chips
from .flash import (identity_kind, _flash_sectors, _log_sector_size, device_map_problem, driven_layout,
                    drives, logical_sector_ranges)
from .ucb import ucb_name
from .util import EX_REFUSED, Fail, _size


def detected_family(id_addr, chipid, chip=None):
    """The generation of the connected silicon from its identity register:
    SCU_CHIPID.CHTEC (01 TC2xx, 10 TC3xx), or the family whose own identity
    register answered (TC4xx has no SCU_CHIPID). None if it cannot tell."""
    if identity_kind(id_addr, chip) == "chipid":
        return {1: "tc2xx", 2: "tc3xx"}.get((chipid >> 6) & 3)
    families = {known.family for known in all_chips() if known.id_addr == id_addr}
    return families.pop() if len(families) == 1 else None


def confirm_family(chip, id_addr, chipid, named):
    """Refuse to drive the flash unless the connected silicon is the family -c
    or --device names (`named`, as given: "-c tc4d7"): the flash register map
    differs per family."""
    detected = detected_family(id_addr, chipid, chip)
    if detected is None:
        raise Fail("cannot tell the connected part's family (identity 0x%08X at 0x%08X); "
                   "the flash registers differ per family" % (chipid, id_addr), EX_REFUSED)
    if detected != chip.family:
        raise Fail("%s is a %s part but the connected one is %s; the flash registers "
                   "differ per family" % (named, chip.family, detected), EX_REFUSED)


def confirm_variant(variant, spans):
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


def check_supported(chip, what):
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


def region_ranges(chip, regions):
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


def regions_label(chip, segments):
    """Comma list of the regions the segments touch, naming UCBs individually."""
    names = []
    for seg in segments:
        region = chip.kind_at(seg.addr)
        name = ucb_name(chip, seg.addr) if region == "ucb" else region
        if name not in names:
            names.append(name)
    return ", ".join(names)


def refuse_protected(chip, spans, flag):
    """Raise Fail if any (lo, hi) span overlaps a range with `flag` set
    (write_protected or erase_protected)."""
    for lo, hi in spans:
        region = chip.protected_at(lo, hi, flag)
        if region is not None:
            raise Fail("0x%08X..0x%08X overlaps the %s range %s (0x%08X..0x%08X)"
                       % (lo, hi, flag.replace("_", "-"), region.name, region.base,
                          region.end), EX_REFUSED)


def write_plan(chip, image, args, prompt=True):
    """The checks of a write, in order: family and device map, allowlist, UCB
    gate, erase plan, write- and erase-protected ranges; then, with `prompt`,
    the confirmation prompt. -> erase plan. Raises Fail."""
    check_supported(chip, "write")

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
        plan = logical_sector_ranges(chip, image)
    except ValueError as error:
        raise Fail(str(error), EX_REFUSED)

    # PROTECTED: no segment, and no logical sector the write may erase, may
    # overlap a write-protected range. A logical sector it may erase in an
    # erase-protected range must be filled completely by the image, so none of
    # it is left erased. With --erase changed the checks cover the full plan,
    # before the comparison drops unchanged logical sectors. No option lifts this.
    spans = [(segment.addr, segment.end) for segment in image.segments]
    spans += [(first, first + count * size) for first, count, size in plan]
    refuse_protected(chip, spans, "write_protected")
    for first, count, size in plan:
        for lo in range(first, first + count * size, size):
            hi = lo + size
            region = chip.protected_at(lo, hi, "erase_protected")
            filled = sum(max(0, min(hi, s.end) - max(lo, s.addr)) for s in image.segments)
            if region is not None and filled < size:
                raise Fail("the image fills %s of the logical sector 0x%08X..0x%08X; "
                           "the rest would be left erased in the erase-protected range %s"
                           % (_size(filled), lo, hi, region.name), EX_REFUSED)

    if prompt and not args.yes:
        confirm_or_abort(write_summary(chip, image, args, plan))
    return plan


def write_summary(chip, image, args, plan):
    """The plan of a write as the prompt and `write --dry-run` show it. -> [line]"""
    span_lo, span_hi = image.span
    count = sum(count for _, count, _ in plan)
    lines = ["tcprog: write to %s" % chip.name,
             "  range     0x%08X..0x%08X  %d B in %d segment(s)"
             % (span_lo, span_hi, image.total_bytes, len(image.segments)),
             "  regions   %s" % regions_label(chip, image.segments),
             "  erase     %s" % ("%d logical sector(s) first" % count if args.erase == "covered"
                                 else "the changed ones of %d logical sector(s); unchanged "
                                      "ones are skipped after comparing" % count)]
    lines += ["            0x%08X..0x%08X  %d logical sector(s)"
              % (first, first + n * size, n) for first, n, size in plan]
    lines.append("  verify    %s" % ("no" if args.no_verify else "yes"))
    lines.append("  WARNING   flash is erased and rewritten; this cannot be undone")
    if any(chip.kind_at(segment.addr) == "ucb" for segment in image.segments):
        lines.append("  WARNING   UCB write: applies at the next reset; check `tcprog ucb` first")
    return lines


def erase_plan(chip, args):
    """The checks of an erase and its confirmation prompt.
    -> [(first, count, logical sector size)], one per range. Raises Fail."""
    check_supported(chip, "erase")
    if args.region and (args.addr is not None or args.length is not None):
        raise Fail("erase takes --region or --addr/--length, not both", os.EX_USAGE)
    if args.region:
        ranges = region_ranges(chip, args.region)
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
    refuse_protected(chip, spans, "write_protected")
    refuse_protected(chip, spans, "erase_protected")
    if not args.yes:
        lines = ["tcprog: erase on %s" % chip.name]
        lines += ["  range     0x%08X..0x%08X  %d logical sector(s)"
                  % (first, first + count * sector, count) for first, count, sector in plan]
        if ucb:
            lines.append("  WARNING   UCB erase: applies at the next reset; check "
                         "`tcprog ucb` first")
        lines.append("  WARNING   the contents are erased; this cannot be undone")
        confirm_or_abort(lines)
    return plan


def confirm_or_abort(lines):
    """Show the plan and ask before touching flash. Raises Fail unless the
    answer is yes."""
    sys.stderr.write("\n".join(lines) + "\n")
    try:
        if input("continue? [y/N] ").strip().lower() not in ("y", "yes"):
            raise Fail("not confirmed; nothing was changed", EX_REFUSED)
    except EOFError:
        raise Fail("no terminal to confirm on; pass -y/--yes", EX_REFUSED)
