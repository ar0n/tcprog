# tcprog - AURIX TC2xx/TC3xx/TC4xx flash programmer

> **Work in progress.** Under active development and not yet stable.

A flash programmer for Infineon AURIX that drives the FTDI-MPSSE DAP of a
miniWiggler / on-board debugger, or any FTDI-based programmer. Gets firmware
on/off the part and reports the target honestly, across three AURIX generations
(TC2xx / 1G, TC3xx / 2G and TC4xx / 3G). It is **not** a debugger or emulator.

## Install

Python 3.8 or newer. A Python package: run it from a source checkout with
`python -m tcprog` (or the `./tcprog-cli` launcher), or `pip install .` for a
`tcprog` command.

- **bincopy** (required) parses firmware images.
- **ftdi1**, the libftdi1 Python binding, is needed for the DAP transport. It is
  not on PyPI; install your distribution's libftdi1 Python bindings.
  `list` and `--dry-run` work without it.
- The user needs access to the probe's USB device (`058b:0043` for Infineon DAS
  probes), for example through a udev rule.

```
python -m tcprog --help
python -m tcprog list probes         # attached FTDI/DAP probes
python -m tcprog list chips          # supported parts for --chip
python -m tcprog ident -p 058b:0043:<serial>
python -m tcprog write --dry-run -c tc375 app.srec
python -m tcprog write -c tc375 -p 058b:0043:<serial> app.srec
```

## Commands

Every command except `list` accepts the [common options](#common-options).
Progress goes to stderr: on a terminal as one line updated in place, otherwise
(CI logs, pipes) as a plain line every 10 %, so logs stay short; `--no-progress`
or `-q` turn it off.

### list

Lists attached FTDI/DAP probes, or the parts `--chip` accepts.

| Option | Meaning |
|---|---|
| `probes` | attached FTDI/DAP probes |
| `chips` | supported parts for `--chip` |

### ident

Reads the identity register and reports the part: chip name, step, HSM flag
and PFLASH/DFLASH sizes. The chip name is matched in the device database, which
currently holds identity values for the TC375 and TC4D7; other parts are not
recognised automatically, so pass `-c`. TC2xx and TC3xx use
`SCU_CHIPID` (its CHTEC field tells the generations apart); TC4xx has no
`SCU_CHIPID`, so it reads `CBS_JTAGID` (`0xFA180080`) and reports the JTAG part
number and version instead of the HSM flag. It reports identity only, not
protection state.

| Option | Meaning |
|---|---|
| `--check NAME` | exit 80 unless the detected chip matches `NAME` |
| `--fields LIST` | comma-separated subset of fields to print |

### read

Dumps a range or a whole region to a file (bin/srec/hex) or as a hexdump. Reads
are 1 KiB blocks, 16 per USB transfer. Reading erased PFLASH raises an
ECC error and can wedge the target, so it warns when a range falls outside a
known memory slice. `--skip-erased` reads a mostly erased device and keeps only
what answers; on TC3xx/TC4xx PFLASH it maps the range with `blankcheck` first and
reads only the programmed sectors (a whole TC4D7 in 18 s instead of 10 min).

| Option | Meaning |
|---|---|
| `--addr`, `--length` | range to read |
| `--region pflash\|dflash` | read a whole region (needs `-c`) |
| `-o`, `--output FILE` | output file (bin/srec/hex by extension); omit to hexdump |
| `--format FMT` | override the output format |
| `--skip-erased`, `--sparse` | keep only what answers and emit a sparse image (srec/hex) |
| `--no-progress` | no progress output |

### ucb

Reads the User Configuration Blocks: each UCB's confirmation state (CONFIRMED /
UNLOCKED / ERASED, which counts as errored) and the boot mode header (BMHD)
contents and CRC. `verify` exits 81 if a confirmation word is unrecognized or a
BMHD is invalid; `export` dumps the raw BMHD blocks. On TC3xx it also prints a
**Next boot** check (see [Protection](#protection-and-unlocking)).

| Option | Meaning |
|---|---|
| `decode` / `verify` / `export` | action (default `decode`) |
| `-o`, `--output FILE` | `export`: output file |
| `--format FMT` | `export`: output format (default: from the `-o` extension) |

### write

Programs an image (requires `-c/--chip`): erases the covered sectors (PFLASH and
DFLASH), programs page by page, then verifies by reading back. It runs the
memory-map allowlist and the UCB gate, shows the range, regions (UCBs by name)
and sector count, and asks before touching flash.

On TC3xx, PFLASH program/erase can be refused while the application runs. For
PFLASH, `write` and `erase` therefore reset the part with a halt-after-reset
request, keep CPU0 at its first instruction during the operation, and reset again
afterwards so the application restarts.

| Option | Meaning |
|---|---|
| `FIRMWARE` | image to write (`.elf` / `.hex` / `.srec` / `.bin`) |
| `-a`, `--address ADDR` | load address for a raw `.bin` |
| `--format FMT` | override the image format |
| `--erase sectors\|none` | erase the covered sectors first (default `sectors`) |
| `--no-verify` | skip read-back verification |
| `--after reset\|no-reset\|halt` | after a TC3xx PFLASH write: restart the application (default) or leave CPU0 halted |
| `-n`, `--dry-run` | parse and plan, touch nothing |
| `--enable-ucb-write` | required to write a UCB region |
| `--allow-erase-all` | required for a full-chip erase |
| `-y`, `--yes` | answer yes to the confirmation prompt |
| `--no-progress` | no progress output |

### erase

Erases the logical sectors covering a range (requires `-c/--chip`): PFLASH
16 KiB; DFLASH 4 KiB on TC3xx, 2 KiB on TC4xx; one UCB at a time in a UCB region.
Ranges are split so that one flash command never exceeds the controller's limits
(512 KB PFLASH / 256 KB DFLASH, inside one bank), and every command's error flags
are checked, so a rejected erase fails with an error instead of reporting
success. Asks before erasing.

| Option | Meaning |
|---|---|
| `--addr`, `--length` | range to erase, rounded out to whole sectors |
| `--region pflash` | erase all of PFLASH (needs `--allow-erase-all`) |
| `--allow-erase-all` | required to erase a whole region |
| `-y`, `--yes` | answer yes to the confirmation prompt |

### blankcheck

Maps which PFLASH sectors are programmed with the flash controller's
Verify-Erased command (TC3xx and TC4xx): no array reads, so no ECC traps.

| Option | Meaning |
|---|---|
| `--addr`, `--length` | range to check, rounded out to whole sectors |
| `--region pflash` | check all of PFLASH |
| `--no-progress` | no progress output |

### Common options

Accepted by `ident`, `read`, `ucb`, `write`, `erase` and `blankcheck`:

| Option | Meaning | Default / env |
|---|---|---|
| `-c`, `--chip NAME` | target part or family (e.g. `tc375`, `tc37x`, `tc2xx`); optional where `ident` can detect it | `TCPROG_CHIP` |
| `--device FILE` | custom device JSON instead of a built-in part; its memory slices act as the write allowlist | |
| `-p`, `--probe ID` | FTDI probe as `VID:PID` or `VID:PID:SERIAL` | `TCPROG_PROBE` |
| `--speed KHZ` | DAP clock in kHz | 1000 |
| `--connect-under-reset` | reset the target while attaching; without it the target keeps running | off |
| `-t`, `--transport` | `dap` or `asc-bsl` (`asc-bsl` not implemented yet) | `dap` |
| `--asc-bsl-port`, `--asc-bsl-baud` | serial port and baud for `asc-bsl` (not implemented yet) | 115200 |
| `--debug-unlock-pw PW` | 256-bit debug password: 8 comma-separated words `w0,...,w7` (dec or `0x` hex) | `TCPROG_DEBUG_UNLOCK_PW` |
| `--unlock-tries N` | max debug unlock attempts | 3 |
| `--pflash-unlock-pw [PW]` | lift PFLASH protection; without a value the debug password is reused | `TCPROG_PFLASH_UNLOCK_PW` |
| `--dflash-unlock-pw [PW]` | lift DFLASH protection; without a value the debug password is reused | `TCPROG_DFLASH_UNLOCK_PW` |
| `-q`, `--quiet` | only print errors | |
| `-v`, `--verbose` | more detail (repeatable) | |
| `--json` | machine-readable result on stdout (`ident`, `read`, `ucb`, `blankcheck`, `write --dry-run`) | |

Accepted but not implemented yet: `write --region`, `--preserve`, `--before`,
`--timeout` and `--log-file`; `--asc-bsl-compress`; and the `unlock` command.

## Protection and unlocking

- `--debug-unlock-pw` opens a debug-locked TC3xx part (TC2xx uses the same exchange
  but is untested) with the 256-bit debug password (Cerberus key exchange at startup). It takes exactly 8
  comma-separated words (e.g. `1,2,3,4,5,6,7,8` or `0x00000000,0x11111111,...`)
  and usually succeeds on the first attempt; it retries up to `--unlock-tries`
  times (default 3).
- `--pflash-unlock-pw` / `--dflash-unlock-pw` lift flash protection with the DMU
  "Disable Protection" command before the operation. Given without a value they
  reuse the debug password. On TC3xx PFLASH and DFLASH have separate passwords
  (UCB_PFLASH / UCB_DFLASH); on TC4xx one UCB_RTC_FLASH password lifts the global
  read and write protection of both. A wrong password is refused, and the DMU
  then refuses every retry until the next reset.
- An unlock lasts until the next reset. Attaching leaves the target running, so
  later commands do not need the password again; `--connect-under-reset` resets
  the part while attaching.
- On TC3xx, flash **read** protection (PFLASH or DFLASH) also keeps the debug
  interface locked at startup, and only a configured debug password (UCB_DBG)
  opens it again. Set the debug password before enabling read protection, or the
  part can no longer be reached over DAP. TC4xx does not tie debug access to
  flash read protection.
- UCB changes take effect at the next reset. On TC3xx, `tcprog ucb` prints a
  **Next boot** check that compares what the UCBs will install (PFLASH/DFLASH
  read protection, DFLASH write protection, the debug lock) with the live DMU
  registers, lists pending changes, and flags a LOCKOUT (which also fails
  `ucb verify`) when read protection would come up with no debug password.
  Run it after changing UCBs and before resetting: a protection written by
  mistake can still be undone while the part is running.

## Safety

- `--dry-run` parses and plans, touching nothing.
- `write` and `erase` show the range, regions (UCBs by name) and sector count and
  ask before touching flash.
- `--enable-ucb-write` is required for any write that lands in a UCB region;
  without it the write is refused (exit 82). A wrong UCB can lock you out of the
  part or stop it booting. A CONFIRMED UCB can only be changed with its password
  (Disable Protection, then erase and reprogram); without the password, and for
  UCBs that have no password such as the OTP sets, it is permanent.
- BMHD validation follows the boot mode header checks documented for the startup
  software (SSW) in the user manuals, to catch a header that would not boot.

## Exit codes

| Code | Name | Meaning |
|------|------|---------|
| 0  | `EX_OK`          | success |
| 64 | `EX_USAGE`       | bad arguments / missing option |
| 65 | `EX_DATAERR`     | bad or unreadable image, or bad output format |
| 69 | `EX_UNAVAILABLE` | no probe/target found, `ftdi1` binding missing, or not supported for this part/transport |
| 73 | `EX_CANTCREAT`   | cannot write the output file |
| 74 | `EX_IOERR`       | transfer or flash operation failed |
| 77 | `EX_NOPERM`      | debug-locked and no / wrong `--debug-unlock-pw`, or a flash-protection password was refused |
| 80 | `EX_CHECK`       | `ident --check`: detected part != expected |
| 81 | `EX_VERIFY`      | read-back verification mismatch, or `ucb verify` found a problem |
| 82 | `EX_REFUSED`     | a destructive op refused for safety (e.g. UCB write without `--enable-ucb-write`) |

## Device database

Each part is a JSON file in `tcprog/devices/`: the memory map as an explicit
**list of slices** (the AURIX map has gaps: PFLASH, hole, DFLASH0, hole, UCB,
hole, CFS, DF1/HSM), plus protection and watchdog registers. `--chip` selects one
by name or part-number alias (e.g. `tc397`, `tc275`). Automatic detection
works only for parts whose identity value is in the database (TC375, TC4D7).

Coverage: TC2xx `tc22x/tc23x/tc26x/tc27x/tc29x`; TC3xx
`tc33x/tc35x/tc36x/tc37x(=tc375)/tc38x/tc39x`; TC4xx `tc4dx`. The TC22x and TC23x
entries assume the TC26x/27x/29x register map.

## Tested hardware

| Board | MCU | Probe | Tested |
|---|---|---|---|
| AURIX TC375 lite Kit (`KIT_A2G_TC375_LITE`) | SAK-TC375TP-96F300W, AA step | miniWiggler V3.1 | `ident`, debug unlock, `read`, `ucb` (incl. next-boot check), `blankcheck`, `erase`, `write` (reflashed firmware boots), UCB rewrite, DFLASH protection unlock |
| AURIX TC4D7 lite Kit (`KIT_A3G_TC4D7_LITE`) | TC4D7 | miniWiggler V3.1 | `ident`, `read` (incl. `--skip-erased`), `ucb`, `blankcheck`, `erase`, `write`, PFLASH/DFLASH protection unlock |

Other parts in the device database are untested on silicon.

## Not supported yet

- **ASC BSL transport.** `-t asc-bsl` is accepted but not implemented.
- **TC2xx `write` / `erase`.** TC2xx flash status lives in the FLASH0 module,
  not the TC3xx/TC4xx DMU; these commands are refused on TC2xx.
- **TC4xx debug unlock.** A debug-locked TC4xx is refused (see [TC4xx](#tc4xx)).
- **Halting a debug-locked TC3xx** for PFLASH operations: the halt-after-reset
  would lock it again, so it is skipped with a warning.
- **TC3xx DFLASH in complement-sensing mode** (2 KiB sectors); tcprog assumes the
  default single-ended 4 KiB.
- **On-board debuggers** of the lite kits: they enumerate as DAP probes, but on
  the TC375 kit the reset line is wired differently, so the debug unlock's reset
  does not take effect.

## TC4xx

Supported on TC4Dx (tested on a TC4D7 lite kit), checked against the public
AURIX TC4Dx user manual.

Works:

- `ident` (via `CBS_JTAGID`; TC4xx has no `SCU_CHIPID`)
- `read`, `--skip-erased`, `blankcheck`
- `write` / `erase` on PFLASH and DFLASH
- `ucb` for the four BMHD blocks (`0xAE404800 + n*0x800`)
- PFLASH/DFLASH protection unlock (`--pflash-unlock-pw`; one UCB_RTC_FLASH
  password covers both)

Not yet:

- debug unlock (a debug-locked TC4xx is refused)
- `ucb` beyond the four BMHD blocks; the BMI is shown raw
- reading the UCB0 CHIPID, part-number and unique-ID words
- the ASC BSL transport
- other TC4xx families (no public iLLD or tested boards yet)

## Layout

    pyproject.toml        packaging (console_script `tcprog`, package-data devices)
    tcprog-cli            run from a checkout without installing
    tcprog/               the package
      cli.py              argparse subcommands + main
      dap.py              FTDI-MPSSE DAP transport (Dap, unlock, read/write)
      flash.py            flash driver (erase/program/verify), UCB reads, next-boot check
      chips.py            device database (Chip, lookup/detect)
      image.py            firmware image parse/emit + BMHD
      util.py             exit codes, logging, progress output, Fail
      devices/*.json      device database (memory map + registers, one per part)
    LICENSE               MIT

Image parsing is delegated to bincopy: SREC, Intel HEX and TI-TXT are detected
by content; ELF and raw `.bin` (which needs `--address`) by extension or magic.

## Sources

tcprog is built from Infineon's public documentation: the AURIX TC2xx, TC3xx
and TC4Dx user manuals and data sheets, the C166S On-Chip Debug Support user
manual (V1.1, 2001), and the iLLD driver sources in Infineon's
[AURIX_code_examples](https://github.com/Infineon/AURIX_code_examples), whose
file headers permit use under the Boost Software License.
The DAP transport has no public specification; tcprog implements it from the
miniWiggler's USB traffic and the Infineon DAP (ifxdap) patches posted to the
OpenOCD mailing list, with every telegram validated against the captures.

tcprog is not affiliated with or endorsed by Infineon. AURIX, TriCore and
miniWiggler are trademarks of Infineon Technologies AG.
