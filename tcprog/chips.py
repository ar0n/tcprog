# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""The device database: one JSON per part in devices/, loaded as Chip objects."""
import json
from importlib.resources import files


def _int(x):
    if x is None:
        return None
    return x if isinstance(x, int) else int(str(x), 0)


class ChipMemRegion:
    __slots__ = ("name", "kind", "base", "size", "note")

    def __init__(self, name, kind, base, size, note=""):
        self.name, self.kind, self.base, self.size, self.note = name, kind, base, size, note

    @property
    def end(self):
        return self.base + self.size

    def contains(self, addr):
        return self.base <= addr < self.base + self.size


class Chip:
    def __init__(self, spec, source=""):
        self.name = str(spec["name"]).lower()
        self.family = str(spec.get("family", "")).lower()
        self.aliases = tuple(alias.lower() for alias in spec.get("aliases", ()))
        self.chipid_addr = _int(spec.get("chipid_addr"))
        self.chipid = _int(spec.get("chipid"))
        self.chipid_mask = _int(spec.get("chipid_mask")) or 0xFFFFFFFF
        self.memory = tuple(
            ChipMemRegion(region["name"], region.get("kind", ""), _int(region["base"]),
                          _int(region["size"]), region.get("_note", ""))
            for region in spec.get("memory", ()))
        self.bsl_load = _int(spec.get("bsl_load"))
        self.asclin_flags = _int(spec.get("asclin_flags"))
        self.asclin_flagsclear = _int(spec.get("asclin_flagsclear"))
        self.asclin_txdata = _int(spec.get("asclin_txdata"))
        self.asclin_rxdata = _int(spec.get("asclin_rxdata"))
        self.wdt_cpu0con0 = _int(spec.get("wdt_cpu0con0"))
        self.wdt_safcon0 = _int(spec.get("wdt_safcon0"))
        self.proconpf = _int(spec.get("proconpf"))
        self.procondf = _int(spec.get("procondf"))
        self.procondbg = _int(spec.get("procondbg"))
        self.hf_protect = _int(spec.get("hf_protect"))
        self.notes = spec.get("_notes", "")
        self.source = source

    def matches_chipid(self, value):
        return self.chipid is not None and (value & self.chipid_mask) == (self.chipid & self.chipid_mask)

    def slice_of(self, addr):
        for region in self.memory:
            if region.contains(addr):
                return region
        return None

    def region_of(self, addr):
        region = self.slice_of(addr)
        return region.kind if region else "?"

    def slices(self, kind):
        return [region for region in self.memory if region.kind == kind]

    def first(self, kind):
        for region in self.memory:
            if region.kind == kind:
                return region
        return None


def _load_chips():
    """Load the built-in device DB from the packaged devices/ directory."""
    by_name = {}
    devdir = files(__package__).joinpath("devices")
    for entry in sorted(devdir.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json"):
            continue
        try:
            chip = Chip(json.loads(entry.read_text()), source=entry.name)
        except (OSError, ValueError, KeyError) as error:
            raise ValueError("bad device file %s: %s" % (entry.name, error))
        by_name[chip.name] = chip
    return by_name


_CHIPS = _load_chips()


def lookup_chip(name):
    wanted = name.strip().lower()
    if wanted in _CHIPS:
        return _CHIPS[wanted]
    for chip in _CHIPS.values():
        if wanted == chip.family or wanted in chip.aliases:
            return chip
    return None


def by_chipid(value, addr=None):
    """The chip whose ID register (at `addr`, when given) reads `value`."""
    for chip in _CHIPS.values():
        if (addr is None or chip.chipid_addr == addr) and chip.matches_chipid(value):
            return chip
    return None


def chipid_addrs():
    """Distinct identity-register addresses in the DB, older families first."""
    out = []
    for chip in all_chips():
        if chip.chipid_addr is not None and chip.chipid_addr not in out:
            out.append(chip.chipid_addr)
    return out


def load_device_file(path):
    """Load a Chip from a caller-supplied device JSON file."""
    with open(path) as handle:
        return Chip(json.load(handle), source=path)


def _resolve_chip(args):
    """Resolve the target from --device (a JSON file) or --chip (a built-in name).

    -> (chip_or_None, error_or_None). A None chip with no error means neither was
    given, so the caller may still auto-detect from SCU_CHIPID.
    """
    if getattr(args, "device", None):
        try:
            return load_device_file(args.device), None
        except (OSError, ValueError, KeyError) as error:
            return None, "cannot load device file %s: %s" % (args.device, error)
    if args.chip:
        chip = lookup_chip(args.chip)
        if chip is None:
            return None, "unknown chip %r (see `tcprog list chips`)" % args.chip
        return chip, None
    return None, None


def all_chips():
    return sorted(_CHIPS.values(), key=lambda chip: (chip.family, chip.name))
