# tcprog - AURIX TC2xx/TC3xx flash programmer

> **Work in progress.** Under active development and not yet stable.

A flash programmer for Infineon AURIX that drives the FTDI-MPSSE DAP of a
miniWiggler / on-board debugger, or any FTDI-based programmer. Gets firmware
on/off the part and reports the target honestly, across both AURIX generations
(TC2xx / 1G and TC3xx / 2G). It is **not** a debugger or emulator.

The DAP protocol - the telegram layout - was reverse-engineered from captured
miniWiggler USB traffic; the IOClient response framing follows Infineon's public
C166S On-Chip Debug Support user manual (V1.1, 2001).

A Python package. Run it from a source checkout with `python -m tcprog` (or the
`./tcprog-cli` launcher), or `pip install .` for a `tcprog` command:

    python -m tcprog --help
    python -m tcprog list probes         # attached FTDI/DAP probes
    python -m tcprog list chips          # supported parts for --chip
    python -m tcprog ident -p 058b:0043:MWAJFJ9N
    python -m tcprog write --dry-run -c tc375 app.srec

## Layout

    pyproject.toml        packaging (console_script `tcprog`, package-data devices)
    tcprog-cli            run from a checkout without installing
    tcprog/               the package
      cli.py              argparse subcommands + main
      dap.py              FTDI-MPSSE DAP transport (Dap, unlock, read/write)
      flash.py            FSI flash driver (erase/program/verify) + UCB reads
      chips.py            device DB (Chip, lookup/detect)
      image.py            firmware image parse/emit + BMHD
      util.py             exit codes, logging, Fail
      devices/*.json      device database (memory map + registers, one per part)
    tools/harvest_illd.py generator for devices/ (harvests Infineon iLLD)
    LICENSE               MIT

## Image formats

Parsing is delegated to **bincopy** (`pip install bincopy`), a required
dependency. The text formats (SREC, Intel HEX, TI-TXT) are auto-detected by
content; ELF and raw `.bin` (the latter needs `--address`) are dispatched by
extension / magic.

## Device database

Each part is a JSON file in `tcprog/devices/` - the memory map as an explicit **list of
slices** (the AURIX map has gaps: PFLASH, hole, DFLASH0, hole, UCB, hole, CFS,
DF1/HSM), plus protection registers and the ASCLIN/WDT SFRs the ASC-BSL payload
needs. `--chip` selects one (by name or a part-number alias, e.g. `tc397`,
`tc275`); `ident` can detect it from `SCU_CHIPID` (TC2xx/TC3xx) or `CBS_JTAGID`
(TC4xx). The whole DB is regenerated
from Infineon iLLD by `tools/harvest_illd.py --illd-root <path>/resources`.

Coverage (production parts): TC2xx `tc22x/tc23x/tc26x/tc27x/tc29x`; TC3xx
`tc33x/tc35x/tc36x/tc37x(=tc375)/tc38x/tc39x`; TC4xx `tc4dx` (identity + memory map only:
`ident`, `read` and `ucb` (BMHD blocks); `write`/`erase` and debug unlock refuse
it).
TC2xx (1G) differs from 2G in the watchdog SFRs, the flash-protection module
(FLASH0 vs the DMU), and the UCB layout - captured per-part in the JSON.

## Safety

- `--dry-run` parses and plans, touching nothing.
- `--enable-ucb-write` is required for any write that lands in a UCB region;
  without it such a write is refused (exit 82). UCB writes are irreversible and
  can permanently brick the part. BMHD validation (`validate_bmhd`) mirrors the
  BROM's acceptance check to catch a header that would not boot.

## Exit codes

| Code | Name | Meaning |
|------|------|---------|
| 0  | `EX_OK`          | success |
| 64 | `EX_USAGE`       | bad arguments / missing option |
| 65 | `EX_DATAERR`     | bad or unreadable image, or bad output format |
| 69 | `EX_UNAVAILABLE` | no probe/target found, `ftdi1` binding missing, or command not implemented |
| 73 | `EX_CANTCREAT`   | cannot write the output file |
| 74 | `EX_IOERR`       | transfer failed mid-operation (read/write/erase) |
| 75 | `EX_TEMPFAIL`    | operation timed out (`--timeout`) |
| 77 | `EX_NOPERM`      | debug-locked (DBGIFLCK) and no / wrong `--debug-unlock-pw` |
| 80 | `EX_CHECK`       | `ident --check`: detected part != expected |
| 81 | `EX_VERIFY`      | read-back verification mismatch |
| 82 | `EX_REFUSED`     | a destructive op refused for safety (e.g. UCB write without `--enable-ucb-write`) |

## Status

`list probes`, `list chips`, image loading, `--dry-run` planning, the UCB-write
gate, the chip DB and BMHD validation work. `ident` and `read` work over the FTDI-MPSSE
DAP, including `--debug-unlock-pw` (Cerberus key exchange; the window is
stochastic, so it retries `--unlock-tries` times, default 10).

- `ident` resolves the part from `SCU_CHIPID` and reports the identity decoded
  from it: chip name (matched in the device DB), step, HSM flag, and PFLASH/DFLASH
  sizes. It reports identity only, not protection state. TC4xx has no
  `SCU_CHIPID`; there it reads `CBS_JTAGID` (`0xFA180080`) and reports the JTAG
  part number and version instead of the HSM flag (verified on a TC4D7 lite kit,
  `0x10225083`).
- `ucb [decode|verify|export]` reads and decodes the User Configuration Blocks:
  each UCB's confirmation state (CONFIRMED / UNCONFIRMED / ERASED) and the BMHD
  (boot mode header) contents + CRC. `verify` exits 81 (`EX_VERIFY`) if a UCB
  confirmation word is unrecognized or a BMHD CRC is bad. `export -o FILE` dumps
  the raw BMHD blocks (header, password, confirmation) as bin/srec/hex.
  On TC4xx only the four BMHD blocks are known (`0xAE404800 + n*0x800`, 2 KiB
  each). Their header differs from TC3xx: `SAL` (the header's own address),
  BMI, BMHDID, STAD and a CRC-32 over those 12 bytes, with no inverted CRC;
  password at +0x7D0, confirmation word and its copy at +0x7F0/+0x7F8. The BMI
  is shown raw, as its TC4xx bit layout is not in the iLLD.
- `read` dumps `--addr`/`--length` (or a whole `--region` on a known `-c/--chip`)
  to `-o` as bin/srec/hex (by extension, or `--format`), or hexdumps to stdout.
  Reads are 1 KiB blocks batched over one USB transfer; output verified
  byte-identical to the reference `dap_read.py`. Reading erased flash raises an
  ECC error and can wedge the target, so it warns when a range falls outside a
  known memory slice.
- `write` (requires `-c/--chip`) programs an image over DAP: optional sector
  erase (`--erase sectors`, PFLASH only), page program, then read-back verify.
  It runs the allowlist and UCB gates, then confirms before touching flash
  (skip with `--yes`). The FSI command sequences are transcribed from Infineon's
  iLLD `IfxFlash.h`, and the full erase+program+verify path is proven end-to-end
  on a TC375 (reflashed firmware boots and runs - "Hello World" over ASCLIN0).
- `erase` (requires `-c/--chip`) erases PFLASH 16 KiB logical sectors covering
  `--addr`/`--length`, or a whole `--region pflash` (which needs
  `--allow-erase-all`); confirms first unless `--yes`.

The `--debug-unlock-pw` takes 8 words (e.g. `1,2,3,4,5,6,7,8` or
`0x00000000,0x11111111,...`), or one value repeated 8x. `--pflash-unlock-pw` /
`--dflash-unlock-pw` disable the flash-protection domains, taking that domain's
password only when it differs from the debug access password.
