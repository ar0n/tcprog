# tcprog - AURIX TC2xx/TC3xx/TC4xx flash programmer

> **Work in progress.** Under active development and not yet stable.

A flash programmer for Infineon AURIX, for DAP probes based on FTDI chips: the
miniWiggler or other FTDI-based probes. It identifies the part, reads, writes and
erases flash, and reads and checks the UCBs and boot mode headers, across three
AURIX generations (TC2xx / 1G, TC3xx / 2G and TC4xx / 3G). It is **not** a
debugger or emulator.

## Install

Python 3.11 or newer. A Python package: run it from a source checkout with
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
`--progress` (on `read` and `write`) shows progress on stderr: on a terminal as
one line updated in place, otherwise (CI logs, pipes) as a plain line every 10 %.
`-v` logs each step (reading, blank-check, erase, programming, verifying) as a
line of its own instead.

### list

Lists attached FTDI/DAP probes, or the parts `--chip` accepts.

| Option | Meaning |
|---|---|
| `probes` | attached FTDI/DAP probes |
| `chips` | supported parts for `--chip` |

### ident

Reads the identity register and reports the part: chip name, step, HSM flag
and PFLASH/DFLASH sizes. The chip name is matched in the device database, which
holds series-level identity values (the CHID field of `SCU_CHIPID`) for TC21x,
TC22x, TC23x, TC26x, TC27x, TC29x, TC32x, TC33x/TC33xEXT, TC35x, TC36x,
TC37x/TC37xEXT and TC38x, and the TC4D7 JTAG ID; other parts (TC39x, TC49x)
are not recognised automatically, so pass `-c`. TC2xx and TC3xx use
`SCU_CHIPID` (its CHTEC field tells the generations apart); TC4xx has no
`SCU_CHIPID`, so it reads `CBS_JTAGID` (`0xFA180080`) and reports the JTAG part
number and version instead of the HSM flag. It reports identity only, not
protection state. With an unlock option it also lifts the protection and leaves
it lifted until the next reset.

| Option | Meaning |
|---|---|
| `--check NAME` | exit 80 unless the detected chip matches `NAME` |
| `--fields LIST` | comma-separated subset of fields to print |
| `--json` | result as JSON on stdout |

### read

Dumps a range or a whole region to a file (bin/srec/hex) or as a hexdump. Reads
are 1 KiB blocks, 16 per USB transfer. Reading erased PFLASH raises an
ECC error and can wedge the target, so it warns when a range falls outside a
known address range. A plain read covers exactly the requested range. A bin
file is one flat run of bytes, so whatever lies between the data (blank flash with
`--skip-blank`, the gaps between several ranges) is filled with 0x00, and a
warning says how much; srec and hex keep only the data.
`--skip-blank` reads a mostly blank device and keeps only the programmed data:
on TC3xx/TC4xx PFLASH, DFLASH and UCBs the flash controller's blank-check finds
the programmed sectors, wordlines and pages, and only those are read; a
programmed UCB is read whole. A whole TC4D7 PFLASH takes about 13 s, a TC375
PFLASH about 4 s. Blank DFLASH reads as
0x00, so this is also what tells blank DFLASH from programmed zeros. Elsewhere
(TC2xx, RAM) it keeps the blocks that answer.

| Option | Meaning |
|---|---|
| `--addr`, `--length` | range to read |
| `--region NAME[,NAME]` | read whole address ranges: a range of the device file (`pflash0`, `dflash0`, `dflash1`, `ucb0`, ...) or a kind (`pflash`: every PFLASH bank, `dflash`: DFLASH0, `ucb`: every UCB block) |
| `-o`, `--output FILE` | output file (bin/srec/hex by extension); omit to hexdump |
| `--format FMT` | override the output format |
| `--skip-blank`, `--sparse` | read only the programmed data, skipping blank flash |
| `--json` | result summary as JSON on stdout |
| `--progress` | show a progress line |

### ucb

Reads the User Configuration Blocks: each UCB's confirmation state (CONFIRMED /
UNLOCKED / ERASED, which counts as errored) and the boot mode header (BMHD)
contents and CRC. `verify` exits 81 if a confirmation word is unrecognized or a
BMHD is invalid; `export` dumps the raw BMHD UCBs: all four, and on TC3xx their
COPYs too (in a bin file the 2 KiB between ORIGs and COPYs is 0x00, with a
warning). On TC3xx and TC4xx it
judges the UCBs as the startup software does and marks the BMHD it boots from:
`verify` fails if none of BMHD0..3 is valid, and on TC3xx also if a PFLASH,
DFLASH or DBG UCB has neither a valid ORIG nor COPY. On TC3xx it also prints a
**Next boot validation** (see [Protection](#protection-and-unlocking)).

| Option | Meaning |
|---|---|
| `decode` / `verify` / `export` | action (default `decode`) |
| `-o`, `--output FILE` | `export`: output file |
| `--format FMT` | `export`: output format (default: from the `-o` extension) |
| `--json` | `decode`/`verify` result as JSON on stdout |

In the JSON, each UCB has `orig` and `copy` (`{"state": ..., "word": "0x..."}`, or
`null` where the UCB has no such code) and `in_use` (`"orig"`, `"copy"` or `null`).
`state` is one of `unlocked`, `confirmed`, `errored`, `erased`, `unknown`,
`read_protected` or `unreadable`; `word` is `null` when the code did not answer.

### write

Programs an image (requires `-c/--chip`, or `-c auto` to detect the part): erases the covered sectors (PFLASH and
DFLASH), programs it, then verifies by reading back. Programming uses the flash
controller's Write Burst (PFLASH 256 B on TC3xx, 512 B on TC4xx; DFLASH 32 B)
wherever the image covers a whole aligned burst, and single pages elsewhere. It runs the
memory-map allowlist and the UCB gate, shows the range, regions (UCBs by name)
and sector count, and asks before touching flash.

On TC3xx, PFLASH program/erase can be refused while the application runs. For
PFLASH, `write` and `erase` therefore reset the part with a halt-after-reset
request, keep CPU0 at its first instruction during the operation, and reset again
afterwards so the application restarts (`--after` chooses).

| Option | Meaning |
|---|---|
| `FIRMWARE` | image to write (`.elf` / `.hex` / `.srec` / `.bin`) |
| `-a`, `--address ADDR` | load address for a raw `.bin` |
| `--format FMT` | override the image format |
| `--erase sectors\|none` | erase the covered sectors first (default `sectors`) |
| `--no-verify` | skip read-back verification |
| `--after reset\|no-reset\|halt` | if CPU0 was halted for the operation: restart the application (default) or leave CPU0 halted |
| `--enable-ucb-write` | allow writing a UCB region |
| `-n`, `--dry-run` | parse and plan, touch nothing |
| `-y`, `--yes` | answer yes to the confirmation prompt |
| `--progress` | show a progress line |

### erase

Erases the logical sectors covering a range (requires `-c/--chip`): PFLASH
16 KiB; DFLASH 4 KiB on TC3xx, 2 KiB on TC4xx. The UCBs are erase-protected (see
"Protected ranges").
Ranges are split so that one flash command never exceeds the controller's limits
(512 KB PFLASH / 256 KB DFLASH, inside one bank), and every command's error flags
are checked, so a rejected erase fails with an error instead of reporting
success. Asks before erasing.

| Option | Meaning |
|---|---|
| `--addr`, `--length` | range to erase, rounded out to whole sectors |
| `--region NAME[,NAME]` | erase whole address ranges, named as for `read` (needs `--allow-erase-all`) |
| `--allow-erase-all` | allow erasing a whole region |
| `--enable-ucb-write` | allow erasing a UCB (only in a device file that does not mark it `erase_protected`) |
| `--after reset\|no-reset\|halt` | if CPU0 was halted for the operation: restart the application (default) or leave CPU0 halted |
| `-y`, `--yes` | answer yes to the confirmation prompt |

### Common options

Accepted by `ident`, `read`, `ucb`, `write` and `erase`:

| Option | Meaning | Default / env |
|---|---|---|
| `-c`, `--chip NAME` | target part or family (e.g. `tc375`, `tc37x`), or `auto` to detect the connected part; `ident`, `read` and `ucb` detect it when `-c` is left out, `write` and `erase` only with `-c auto` | `TCPROG_CHIP` |
| `--device FILE` | custom device file (TOML, or older JSON) instead of a built-in part; its address ranges act as the write allowlist | |
| `-p`, `--probe ID` | FTDI probe as `VID:PID` or `VID:PID:SERIAL` | `TCPROG_PROBE` |
| `--speed KHZ` | DAP clock in kHz | 1000 |
| `--connect-under-reset` | reset the target while attaching; without it the target keeps running | off |
| `--debug-unlock-pw PW` | 256-bit debug password: 8 comma-separated words (dec or `0x` hex), e.g. `1,2,3,4,5,6,7,8` | `TCPROG_DEBUG_UNLOCK_PW` |
| `--unlock-tries N` | max debug unlock attempts | 3 |
| `--pflash-unlock-pw [PW]` | lift PFLASH protection; without a value the debug password is used | `TCPROG_PFLASH_UNLOCK_PW` |
| `--dflash-unlock-pw [PW]` | lift DFLASH protection; without a value the debug password is used | `TCPROG_DFLASH_UNLOCK_PW` |
| `-q`, `--quiet` | only print errors | |
| `-v`, `--verbose` | more detail | |

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
  opens it again from the debugger side. Set the debug password before enabling
  read protection, or only application software running on the part can open
  debug again, and tcprog cannot reach it. TC4xx does not tie debug access to
  flash read protection.
- UCB changes take effect at the next reset. On TC3xx, `tcprog ucb` prints a
  **Next boot validation** that compares what the UCBs will install (PFLASH/DFLASH
  read protection, DFLASH write protection, the debug lock) with the live DMU
  registers, lists pending changes, and flags a LOCKOUT (which also fails
  `ucb verify`) when read protection would come up with no debug password.
  Run it after changing UCBs and before resetting: a protection written by
  mistake can still be undone while the part is running.

## Safety

- `--dry-run` parses and plans, touching nothing.
- `write`, `erase` and `read --skip-blank` read the target's identity first and refuse
  (exit 82) unless the connected silicon is the family `-c` or `--device` names,
  since the flash registers differ per family.
- `write` and `erase` show the range, regions (UCBs by name) and sector count and
  ask before touching flash.
- `--enable-ucb-write` is required for any write or erase that lands in a UCB
  region; without it the operation is refused (exit 82). A wrong UCB can lock you out of the
  part or stop it booting. A CONFIRMED UCB can only be changed with its password
  (Disable Protection, then erase and reprogram); without the password, and for
  UCBs that have no password such as the OTP sets, it is permanent.
- A device-file range marked `write_protected = true` is never programmed or
  erased, and one marked `erase_protected = true` is never left erased: `erase` refuses it,
  and a write must fill every sector of it that it erases. Refusals exit 82 and
  no option lifts them. The built-in UCB ranges are erase-protected. See
  "Protected ranges" below.
- BMHD validation follows the boot mode header checks documented for the startup
  software (SSW) in the user manuals, to catch a header that would not boot.

## Exit codes

A command that succeeds prints nothing unless `-v` is given. Diagnostics go to
stderr as one line each: `tcprog: error: ...` when the command fails,
`tcprog: refused: ...` when a safety check stops it (exit 82), and
`tcprog: warning: ...` when it goes on. A reason or hint follows after `; `.

| Code | Name | Meaning |
|------|------|---------|
| 0  | `EX_OK`          | success |
| 64 | `EX_USAGE`       | bad arguments / missing option |
| 65 | `EX_DATAERR`     | bad or unreadable image, or bad output format |
| 69 | `EX_UNAVAILABLE` | no probe/target found, `ftdi1` binding missing, or not supported for this part |
| 73 | `EX_CANTCREAT`   | cannot write the output file |
| 74 | `EX_IOERR`       | transfer or flash operation failed |
| 77 | `EX_NOPERM`      | debug-locked and no / wrong `--debug-unlock-pw`, or a flash-protection password was refused |
| 80 | `EX_CHECK`       | `ident --check`: detected part != expected |
| 81 | `EX_VERIFY`      | read-back verification mismatch, or `ucb verify` found a problem |
| 82 | `EX_REFUSED`     | a destructive op refused for safety (e.g. UCB write without `--enable-ucb-write`) |

## Device database

Each part is a TOML file in `tcprog/devices/`: the memory map as an explicit
**list of address ranges** (the AURIX map has gaps: PFLASH, hole, DFLASH0, hole, UCB,
hole, CFS, DF1/HSM), plus protection and watchdog registers. Values shared by a
whole family live in `tcprog/devices/family/<family>.toml`, including the base
memory map; a part file names it with `base = "tc3xx"` and overrides what differs.
A part inherits the family's ranges, overrides keys of one by giving them under
the same name (`dflash0 = { size = 0x18000 }` keeps the family's kind and base),
and removes one with `name = false`; the cached PFLASH
alias at `0x8...` is added for every PFLASH range. `tcprog list chips NAME` shows
the resulting map. A custom `--device` file works the same way; without `base` it
lists every range itself, so a write can touch exactly those. Custom device files
may still be JSON. `--chip` selects one
by name or part-number alias (e.g. `tc397`, `tc275`). Automatic detection
works for parts whose identity value is in the database (see [ident](#ident)).

For example, to drop the family's `dflash1`:

```toml
[memory]
dflash1 = false
```

### Protected ranges

A range in a device file can carry one of two flags; `read` ignores both, and no
option lifts them:

- `write_protected = true`: the range is never changed. `write` refuses image
  data in it and any sector it would erase there; `erase` refuses it.
- `erase_protected = true`: `erase` refuses the range, and `write` may rewrite it
  only with sectors the image fills completely, so no part of it is left erased.
  The built-in device files set it on every UCB range: an erased UCB, or one with
  an erased confirmation code, can lock you out of the part or stop it booting.

`write_protected` is the full lock; `erase_protected` alone still allows a
complete rewrite. A flagged range may lie inside another range, so a production device file can
keep the boot loader, the UCBs or the HSM DFLASH out of reach while the rest
stays writable. Based on the family map, it only lists what changes:

```toml
name = "tc37x-prod"
base = "tc3xx"

[memory]
bootloader = { kind = "pflash", base = 0xA0000000, size = 0x20000, write_protected = true }
ucb0       = { write_protected = true }
dflash1    = { write_protected = true }
```

The protection also covers the cached alias of a PFLASH range (`0x80000000` for
`0xA0000000`).

`--region` takes these names too (`--region bootloader`). A UCB write still needs
`--enable-ucb-write` as well.

Coverage: TC2xx `tc21x/tc22x/tc23x/tc26x/tc27x/tc29x`; TC3xx `tc32x`,
`tc33x` (incl. TC33xEXT), `tc35x/tc36x`, `tc37x` (=tc375, incl. TC37xEXT),
`tc38x/tc39x`; TC4xx `tc4dx` (TC4D7, TC4D9) and `tc49x` (TC497, TC499). The TC21x and TC22x entries assume the
TC23x register map, and `tc21x` lists no UCB block. `tc49x` lists only UCB0 (the
UCB1 size is not public), so `write` and `erase` refuse it; `read` works.

On TC35x and TC36x, PFLASH is two 2 MB banks with a reserved gap at
`0xA0200000`, so the database lists them as two address ranges (PF1 at
`0xA0300000`). `read --region pflash` and `erase --region pflash` work bank by bank.

Reduced-flash TC3xx variants (for example the 3 MB TC356TD-48F, the 4 MB TC377TX-64F
or the 8 MB TC387TP-128F) have less PFLASH than their family entry. A part file
lists their bank layouts by the PFLASH size code in `SCU_CHIPID` (FSIZE), and
after connecting tcprog uses the layout of the connected part: `write` and
`erase` refuse addresses outside it before touching the flash, and
`read --region` reads only its banks. A size the file has no layout for is
refused for PFLASH. The 8 MB TC387TP comes in two bank layouts with the same
CHIPID; tcprog uses only the ranges both have. A TC3xx part without HSM
(`SCU_CHIPID.SEC` clear, for example the TC333L) has no DF1, so its `dflash1` range
is dropped.

## Tested hardware

| Board | MCU | Probe | Tested |
|---|---|---|---|
| AURIX TC375 lite Kit (`KIT_A2G_TC375_LITE`) | SAK-TC375TP-96F300W, AA step | miniWiggler V3.1 | `ident`, debug unlock, `read`, `ucb` (incl. next boot validation), `erase`, `write` (reflashed firmware boots), UCB rewrite, DFLASH protection unlock |
| AURIX TC4D7 lite Kit (`KIT_A3G_TC4D7_LITE`) | TC4D7 | miniWiggler V3.1 | `ident`, `read` (incl. `--skip-blank`), `ucb`, `erase`, `write`, PFLASH/DFLASH protection unlock |

Other parts in the device database are untested on silicon.

## Not supported yet

- **ASC BSL transport.** Not implemented; tcprog talks to the target over DAP only.
- **TC2xx `write` / `erase`.** TC2xx flash status lives in the FLASH0 module,
  not the TC3xx/TC4xx DMU; these commands are refused on TC2xx.
- **TC4xx debug lock and unlock.** Unsupported: how a TC4xx is debug-locked and
  unlocked is not known yet, so a debug-locked TC4xx is refused (see [TC4xx](#tc4xx)).
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
- `read`, including `--skip-blank`
- `write` / `erase` on PFLASH and DFLASH
- `ucb` for the four BMHD blocks (`0xAE404800 + n*0x800`)
- PFLASH/DFLASH protection unlock (`--pflash-unlock-pw`; one UCB_RTC_FLASH
  password covers both)

Not yet:

- debug lock and unlock (unsupported; a debug-locked TC4xx is refused)
- `ucb` beyond the four BMHD blocks; the BMI is shown raw
- reading the UCB0 CHIPID, part-number and unique-ID words
- the ASC BSL transport
- other TC4xx families: TC49x has a read-only entry (`-c tc49x`); TC46x/TC48x use RRAM with a different controller

## Layout

    pyproject.toml        packaging (console_script `tcprog`, package-data devices)
    tcprog-cli            run from a checkout without installing
    tcprog/               the package
      cli.py              argparse subcommands + main
      dap.py              DAP transport over the probe's FTDI chip (connect, unlock, read/write)
      flash.py            flash driver (erase/program/verify), UCB reads, next boot validation
      chips.py            device database (Chip, lookup/detect)
      image.py            firmware image parse/emit + BMHD
      util.py             exit codes, logging, progress output, Fail
      devices/*.toml      device database (memory map + registers, one per part)
      devices/family/     per-family defaults the part files build on
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
