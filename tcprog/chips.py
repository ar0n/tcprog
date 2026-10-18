# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aron Szabo <aron@aron.ws>
"""The device database: one TOML file per part in devices/, loaded as Chip objects.

A part file names its family's defaults with `base = "tc3xx"` (devices/family/
tc3xx.toml) and overrides any key. The family file holds the base memory map: a
part inherits its ranges, overrides keys of one by giving them under the same name
(`dflash0 = { size = 0x18000 }`), and removes one with `name = false`. A file without `base` lists every range itself,
so a custom --device file can confine a write to exactly those.
Custom device files may also be JSON (the older format)."""
import copy
import json
import tomllib
from importlib.resources import files

_DEVICES = files(__package__).joinpath("devices")


def _int(x):
    if x is None:
        return None
    return x if isinstance(x, int) else int(str(x), 0)


class MemRange:
    """One address range of the device file. A write-protected range is never
    programmed or erased. An erase-protected range is never left erased: `erase` refuses it,
    and a write must reprogram every sector of it that it erases. Either may lie
    inside another range (a boot loader inside PFLASH)."""
    __slots__ = ("name", "kind", "base", "size", "note", "write_protected",
                 "erase_protected")

    def __init__(self, name, kind, base, size, note="", write_protected=False,
                 erase_protected=False):
        self.name, self.kind, self.base, self.size, self.note = name, kind, base, size, note
        self.write_protected, self.erase_protected = write_protected, erase_protected

    @property
    def end(self):
        return self.base + self.size

    def contains(self, addr):
        return self.base <= addr < self.base + self.size


# The keys a [memory] entry may have; any other is refused, so a misspelt
# flag cannot leave a range unprotected.
_RANGE_KEYS = {"name", "kind", "base", "size", "_note", "write_protected", "erase_protected"}


def _uncached(addr):
    """The non-cached alias (0xA...) of a cached PFLASH address (0x80000000 to
    0x81FFFFFF), which flash command parameters must use; other addresses are
    returned as they are. TC4D7 PFLASH (20 MB) runs past 0x81000000."""
    return addr | 0x20000000 if addr & 0xFE000000 == 0x80000000 else addr


# SCU_CHIPID.FSIZE (TC3xx): PFLASH size per code.
_FSIZE_BYTES = tuple(int(mb * 0x100000) for mb in
                     (0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16))


class Chip:
    def __init__(self, spec):
        self.name = str(spec["name"]).lower()
        self.family = str(spec.get("family", "")).lower()
        self.aliases = tuple(alias.lower() for alias in spec.get("aliases", ()))
        # Identity register (SCU_CHIPID, or CBS_JTAGID on TC4xx) and the value
        # it reads on this part. The old chipid_* keys are still accepted.
        self.id_addr = _int(spec.get("id_addr", spec.get("chipid_addr")))
        self.id_value = _int(spec.get("id_value", spec.get("chipid")))
        self.id_mask = _int(spec.get("id_mask", spec.get("chipid_mask"))) or 0xFFFFFFFF
        # [memory] maps each range name to {kind, base, size}; the older list form
        # (each entry with its own "name") is still accepted.
        memory = [dict(region, name=name) for name, region in _memory(spec).items()
                  if region is not False]
        for region in memory:
            unknown = set(region) - _RANGE_KEYS
            if unknown:
                raise ValueError("memory range %s: unknown key(s) %s"
                                 % (region.get("name"), ", ".join(sorted(unknown))))
        ranges = [MemRange(region["name"], region.get("kind", ""), _int(region["base"]),
                           _int(region["size"]), region.get("_note", ""),
                           bool(region.get("write_protected", False)),
                           bool(region.get("erase_protected", False)))
                  for region in memory]
        # Every PFLASH range also has a cached alias at 0x8...; it is added unless
        # the file lists a range there itself.
        listed = {r.base for r in ranges}
        ranges += [MemRange(r.name + "_cached", "pflash", r.base & ~0x20000000, r.size)
                   for r in ranges if r.kind == "pflash" and r.base >> 28 == 0xA
                   and r.base & ~0x20000000 not in listed]
        self.memory = tuple(ranges)
        # BSL loader: load address, ASCLIN0 and watchdog registers.
        self.bsl_load = _int(spec.get("bsl_load"))
        self.asclin_flags = _int(spec.get("asclin_flags"))
        self.asclin_flagsclear = _int(spec.get("asclin_flagsclear"))
        self.asclin_txdata = _int(spec.get("asclin_txdata"))
        self.asclin_rxdata = _int(spec.get("asclin_rxdata"))
        self.wdt_cpu0con0 = _int(spec.get("wdt_cpu0con0"))
        self.wdt_safcon0 = _int(spec.get("wdt_safcon0"))
        # Security evaluation: live protection registers.
        self.proconpf = _int(spec.get("proconpf"))
        self.procondf = _int(spec.get("procondf"))
        self.procondbg = _int(spec.get("procondbg"))
        self.hf_protect = _int(spec.get("hf_protect"))
        # Reduced-flash variants: SCU_CHIPID.FSIZE -> the non-cached PFLASH banks
        # [(base, size)] of that variant. [pflash_variants] keys are FSIZE codes.
        self.pflash_variants = {
            int(str(code), 16): tuple((_int(base), _int(size)) for base, size in banks)
            for code, banks in spec.get("pflash_variants", {}).items()}
        # Set on a variant whose PFLASH size the device file has no layout for.
        self.pflash_unknown = None

    def for_identity(self, id_addr, value):
        """This chip as the identity register describes it. On TC3xx,
        SCU_CHIPID.FSIZE gives the PFLASH size: a size with a layout in
        [pflash_variants] gets those banks; a size other than this file's with
        no layout gets `pflash_unknown` (its size in bytes) set. SCU_CHIPID.SEC
        clear (no HSM) drops the dflash1 range: those parts have no DF1.
        Otherwise self."""
        if self.family != "tc3xx" or value is None or id_addr != self.id_addr:
            return self
        variant = self._with_fsize((value >> 24) & 0xF)
        if not value >> 31 and any(r.name == "dflash1" for r in variant.memory):
            variant = copy.copy(variant)
            variant.memory = tuple(r for r in variant.memory if r.name != "dflash1")
        return variant

    def _with_fsize(self, fsize):
        """This chip with the PFLASH of SCU_CHIPID.FSIZE code `fsize` (see
        for_identity)."""
        size = _FSIZE_BYTES[fsize]
        if fsize in self.pflash_variants:
            banks = self.pflash_variants[fsize]
        elif size == sum(r.size for r in self.noncached_ranges("pflash")):
            return self
        else:
            variant = copy.copy(self)
            variant.pflash_unknown = size
            return variant
        pflash = [MemRange("pflash%d" % n, "pflash", base, size)
                  for n, (base, size) in enumerate(banks)]
        cached = [MemRange("pflash%d_cached" % n, "pflash", base & ~0x20000000, size)
                  for n, (base, size) in enumerate(banks)]
        variant = copy.copy(self)
        variant.memory = tuple(pflash + cached +
                               [r for r in self.memory if r.kind != "pflash" or r.write_protected
                                or r.erase_protected])
        return variant

    def matches_id(self, value):
        return self.id_value is not None and (value & self.id_mask) == (self.id_value & self.id_mask)

    def range_at(self, addr):
        for region in self.memory:
            if region.contains(addr):
                return region
        return None

    def kind_at(self, addr):
        region = self.range_at(addr)
        return region.kind if region else "?"

    def ranges(self, kind):
        return [region for region in self.memory if region.kind == kind]

    def outer_ranges(self):
        """The ranges not lying inside another range of the file."""
        return [r for r in self.memory
                if not any(o is not r and o.base <= r.base and r.end <= o.end
                           and (o.base, o.end) != (r.base, r.end) for o in self.memory)]

    def protected_at(self, lo, hi, flag):
        """The first range with `flag` (write_protected or erase_protected) set
        that overlaps [lo, hi), or None. A cached PFLASH address is the same flash
        as its non-cached one, so it is checked there."""
        lo, hi = _uncached(lo), _uncached(hi - 1) + 1
        for region in self.memory:
            if getattr(region, flag) and region.base < hi and lo < region.end:
                return region
        return None

    def first(self, kind):
        for region in self.memory:
            if region.kind == kind:
                return region
        return None

    def noncached_ranges(self, kind):
        """The address ranges of a kind, sorted by address: every PFLASH bank (its
        cached alias left out; banks can leave gaps on TC35x/TC36x), every UCB
        block, and for DFLASH only DFLASH0, not the separate HSM DFLASH."""
        outer = sorted((region for region in self.outer_ranges()
                        if region.kind == kind and region.base >> 28 != 0x8),
                       key=lambda region: region.base)
        return outer[:1] if kind == "dflash" else outer


def _memory(spec):
    """A spec's [memory] as {name: range}, from the table form or the older list
    form (each entry with its own "name")."""
    memory = spec.get("memory", {})
    if isinstance(memory, dict):
        return dict(memory)
    return {region["name"]: region for region in memory}


def _with_base(spec):
    """The part spec on top of its family defaults (`base`). Memory ranges are
    merged by name: a part's range overrides the keys it gives (e.g. only `size`)
    of the family's range, `false` removes it, and new names are added after the
    family's."""
    if "base" not in spec:
        return spec
    entry = _DEVICES.joinpath("family", "%s.toml" % spec["base"])
    if not entry.is_file():
        raise ValueError("unknown base %r" % spec["base"])
    merged = tomllib.loads(entry.read_text())
    memory = _memory(merged)
    for name, region in _memory(spec).items():
        inherited = memory.get(name)
        memory[name] = (dict(inherited, **region) if isinstance(region, dict)
                        and isinstance(inherited, dict) else region)
    merged.update(spec)
    merged["memory"] = {name: region for name, region in memory.items() if region is not False}
    return merged


def _load_chips():
    """Load the built-in device DB from the packaged devices/ directory."""
    by_name = {}
    for entry in sorted(_DEVICES.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".toml"):
            continue
        try:
            chip = Chip(_with_base(tomllib.loads(entry.read_text())))
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


def by_id(value, addr=None):
    """The chip whose ID register (at `addr`, when given) reads `value`."""
    for chip in _CHIPS.values():
        if (addr is None or chip.id_addr == addr) and chip.matches_id(value):
            return chip
    return None


def id_addrs():
    """Distinct identity-register addresses in the DB, older families first."""
    out = []
    for chip in all_chips():
        if chip.id_addr is not None and chip.id_addr not in out:
            out.append(chip.id_addr)
    return out


def load_device_file(path):
    """Load a Chip from a caller-supplied device file: TOML, or JSON by extension."""
    if path.endswith(".json"):
        with open(path) as handle:
            return Chip(_with_base(json.load(handle)))
    with open(path, "rb") as handle:
        return Chip(_with_base(tomllib.load(handle)))


def all_chips():
    return sorted(_CHIPS.values(), key=lambda chip: (chip.family, chip.name))
