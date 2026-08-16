# Testing

Built to the standard of `docs/testing-methodology.md` in
[calebpower/reaper](https://github.com/calebpower/reaper): a **portfolio of
oracles**, where each tier earns its place by answering a question the cheaper
tiers cannot. Reaper itself is not integrated.

The organising fact about this program is that it presses a key at a firmware
console. Almost every mistake it can make is undone by the next iteration of
the loop; that one is not. So the testing is shaped around a single question
asked at five different scales: *can this thing press a key when it should
not?*

```
make test           tiers 1-5, 7-9, 11
make test-hostile   the same, under LANG=C with PYTHONUTF8=0
make test-e2e       tier 6, in podman, against a real VNC server
make test-mutants   break the code deliberately; every mutant must be caught
```

## The tiers

| Tier | The question only it answers | Where |
|---|---|---|
| 1 Pure unit | Is this calculation right at the boundaries? | `tests/unit/` |
| 2 Conformance | Did rendered output drift? | `tests/conformance/` |
| 3 Source-as-data | Does the code structure hold its claims? | `tests/structural/` |
| 4 Contract | Do the decision rules hold in isolation? | `tests/contract/` |
| 5 Fake server | What happens when the transport fails? | `tests/fake/` |
| 6 Full stack | Does it work against software we did not write? | `containers/` |
| 7 Seeded fuzz | Do ordinary untried inputs crash it? | `tests/fuzz/` |
| 8 Concurrency | Does the invariant survive two actors? | `tests/concurrency/` |
| 9 Simulated host | What breaks only after history accumulates? | `tests/simulation/` |
| 10 Live audit | Does it hold against a real iDRAC? | **not yet run** |
| 11 Human evidence | Does this make sense to a person? | `tests/evidence/` |

### Tier 1 — Pure unit

Boundaries rather than middles: the failure threshold at 2, 3 and 4; tolerance
at exactly its limits; the intervention window one second either side of a
day; framebuffers of zero, one and one-too-few pixels.

DES is checked against ten published NBS/FIPS validation vectors rather than
against values this implementation produced — a round trip would pass just as
happily with every S-box transposed. The PNG decoder is checked against
fixtures assembled by hand from the specification.

### Tier 2 — Conformance

The `configure` report and the log line format are compared **whole**, against
committed golden files, not by substring. Both are interfaces: an operator
reads the first, and anything alerting on the daemon parses the second.
Regenerate with `BOOT_ERR_SHIM_REGENERATE_GOLDEN=1` and read the diff.

### Tier 3 — Source-as-data

Parses this repository and asserts things that are true in two files at once
and checked in neither:

- every config key the parser accepts appears in the sample, and vice versa;
- `StateDirectory=` in the systemd unit equals `STATE_DIR_NAME` in
  `platform_.py`, and the rc script's config default equals the FreeBSD one;
- every CLI subcommand appears in the README, and the README documents no
  command that does not exist;
- **no module under `src/` imports anything outside the standard library** —
  which is what makes the single-file zipapp valid.

### Tier 4 — Contract

Two matrices over the pure decision functions, with no I/O at all.

The **decision matrix** maps `(host state, failure count)` to `(action, sleep,
reason)`. The existing rows passing unchanged is the gate on refactoring
`daemon.py`.

The **safety matrix** is exhaustive rather than representative: all sixteen
combinations of the four inputs to `may_press_key`, of which exactly one may
press. Add a fifth input and the count assertion fails rather than the new
combination quietly going uncovered.

### Tier 5 — Fake server

`tools/fake_vnc_server.py` with 26 injectable faults. A live server produces
clean refusals on demand; what takes daemons down is the untidy middle —
accept-then-hang, a frame truncated mid-rectangle, a peer sending one byte per
second. None of those can be provoked reliably against real hardware.

This tier is why every read in `rfb.py` carries a deadline rather than a
socket timeout: a timeout bounds one `recv` and does nothing about a server
that keeps resetting it forever.

### Tier 6 — Full stack, containerized

Thirteen named stages on Ubuntu 26.04 LTS, started hostile under `LANG=C`.
Run one with `make test-e2e STAGES=calibrate`.

The point is **TigerVNC**. Our client validated only against our own fake
server proves very little — both halves would agree just as happily on a
shared misreading of the protocol. Here it talks to an implementation nobody
here wrote, reading text drawn by xterm in a `-misc-fixed` bitmap font.

The console records every keystroke it receives to a file, which is the second
oracle for the claim that matters: "we sent a key" is our log talking about
itself.

### Tier 7 — Seeded fuzzing

The oracle, translated from "no 5xx, ever": no unhandled exception, every
failure a typed `ShimError` with a usable message, and the process still
working afterwards. Three surfaces — the RFB byte stream, images, and mutated
TOML.

```
BOOT_ERR_SHIM_FUZZ_SEED=12345 BOOT_ERR_SHIM_FUZZ_ITERATIONS=5000 make test
```

### Tier 8 — Concurrency

Two daemons against one console, concurrent calibration writes, and signals.
The two-daemon assertion is on the console's own record of what it received,
not on either process's exit code.

### Tier 9 — Simulated host

A seeded generator builds timelines of a flaky host and a flaky iDRAC; real
`Daemon` instances run the real decision code and the real detector against
real framebuffers, with only time and the outside world simulated. A nemesis
supplies adversarial events and a shrinker reduces a failure to something
readable.

**The checker's self-test comes first, in its own file.** Seven deliberately
broken daemons, each of which must make the corresponding invariant complain,
plus an assertion that the invariant list and the self-test list agree. An
invariant that never fires is indistinguishable from a passing suite.

```
BOOT_ERR_SHIM_SIM_SEED=99 BOOT_ERR_SHIM_SIM_TIMELINES=200 make test
```

### Tier 11 — Human evidence

Writes artefacts to `evidence/` rather than returning a verdict: the learned
font, the frame with a box drawn around exactly the pixels the detector
compared, and a near-miss for comparison.

Following the methodology's rule about colour, **contrast is computed, never
eyeballed** — through relative luminance, with hue discarded, asserted against
a floor. A console can look perfectly readable and sit at 2:1, where a
threshold placed one value out flips whole glyphs, and no amount of looking at
a PNG will tell you that.

`docs/sample-learned-font.txt` is committed output from this tier: the letters
calibration recovered from a font it had never seen.

## The mutation harness

`make test-mutants` is the methodology's acceptance check for the suite
itself. It introduces deliberate defects one at a time and requires each to
turn the suite red. **A mutant that survives means the suite has a hole**, and
several have:

- `rfb-accepts-old-protocol-versions` survived because five sanity guards were
  asserted only by exception type, and each would still have raised
  *something* — the peer hangs up and we report a lost connection. They now
  assert the message, which is what distinguishes "we refused" from "the peer
  rescued us".
- `calibration-accepts-approximate` survived because a blurred screen abandons
  each candidate grid early, so relaxing the final acceptance check never came
  into play. A screen that is right except for a few pixels closes it.
- `sleep-ignores-a-stop-request` survived because a ten-second bound on
  noticing a stop request lets a "sleep in three-second chunks" implementation
  pass.

Mutants targeting platform-specific code are gated and reported as **not
verified** on the other platform rather than counted as caught.

## What the target actually looks like

Worth stating, because two of the fixtures model things the real console does
not have, and a reader could mistake the fixture for the specification.

The PERC message is **not a dialog**. There is no prompt box, no input field
and nothing focused; the firmware is blocked in a busy-wait and the whole
machine is stopped until a scancode arrives. Two consequences follow.

**The framebuffer is completely static.** Nothing redraws, no clock ticks, no
spinner turns. That is the ideal case for exact bitmap comparison, and the
expected result on real hardware is `detail=region` at 0.0000% difference,
with the glyph decoder never running at all. The caret stage exists because
some firmware does blink one, not because this one is known to.

**Retry is not a recovery strategy here.** Elsewhere a failed match is
transient -- a screen mid-redraw, a frame captured while something moved --
and the next cycle sees something different. Against a blocked firmware the
next cycle sees the identical pixels, so a detection failure at 14:00 is still
a detection failure at 06:00 tomorrow. Anything that depends on "it will
resolve itself next time" is wrong in this environment.

The `focus_console` helper in the containerised tier is likewise a fixture
artefact: X needs a focused window and there is no window manager to assign
one. Firmware has no such notion.

## What none of this proves

Named explicitly, because a suite this large is otherwise easy to mistake for
completeness.

**There is no FreeBSD host.** Every FreeBSD-specific value is covered by
argument-injected unit tests and by structural checks against the rc script,
and none of it has ever been executed on FreeBSD. The rc script has been
syntax-checked and read. That is all.

**There is no iDRAC.** Tier 6 proves the client works against TigerVNC.
An iDRAC is not TigerVNC: it may scale the framebuffer, it may present a
different security type, and its console may be UEFI rather than text mode.
Only tier 10 closes that gap, and tier 10 additionally requires the fault to
actually occur.

**RFB over TLS is untested end to end.** TigerVNC offers TLS through VeNCrypt,
which this client does not implement; iDRAC wraps the RFB port in plain TLS
instead. Only the config gate is covered.

**The OCR engine does not exist.** `engine = "ocr"` is accepted by the config
and held in reserve for a console that cannot be calibrated. Nothing exercises
it because nothing has yet shown it is needed.

**Booting the systemd unit is not covered.** It is verified statically with
`systemd-analyze verify`, its paths are cross-checked against the code, and
the binary it names is installed and run — but rootless podman on a WSL
machine cannot give it a writable cgroup hierarchy, so it has never actually
been started as a service.
