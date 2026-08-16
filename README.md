# boot-err-shim

A PERC H730P whose flash has failed stops the host at POST with a prompt:

```
Disabling writes to flash as the flash part has gone bad.
Please contact technical support to resolve this issue.
Please press 'Y' to continue.
```

The machine waits there until somebody presses `Y`. Until the controller is
replaced, every reboot needs a human at the console.

This daemon is the shim. It watches the host, and when the host stops
answering it looks at the console through the iDRAC's VNC server, finds that
prompt, and presses `Y` so the boot completes.

**It is a workaround for failing hardware, not a fix.** It logs a warning when
it has to intervene more than a few times a day, because a controller that
needs rescuing that often is one somebody should be replacing.

Runs on FreeBSD (rc.d) and Linux (systemd). Python 3.11+, standard library
only — nothing to install on the target beyond the program itself.

## The part that is not obvious

VNC hands you **pixels, not text**. There is no string to search; finding that
prompt means recognising it in a framebuffer image. Two things decide whether
that is easy, and neither is knowable in advance:

- whether the iDRAC sends the console unscaled (glyphs stay pixel-exact) or
  stretched (interpolation blurs every edge);
- whether the host posts in legacy BIOS text mode or UEFI, which use different
  fonts.

So the program does not ship a font and hope. It learns yours:

> Reboot the box, let it stop at the error, and run `boot-err-shim configure`.

Knowing from the config exactly what text should be on screen, `configure`
works backwards from the pixels to a **calibration** — cell geometry, text
origin, foreground and background colours, and a bitmap for every glyph in the
message, extracted from your own hardware. Then it re-renders the message from
what it learned and diffs it against the framebuffer to prove it got it right.

`configure` never sends a keypress, so it is safe to run against the live stuck
console.

**Without a calibration the daemon refuses to press anything.** Matching
without one is guesswork, and this program declines to guess about keystrokes.

## Installing

### FreeBSD

```sh
make install-freebsd

# The service user. The rc script defaults to it and will refuse to start
# without it; `make install-freebsd` prints this line but does not run it.
pw useradd boot-err-shim -d /nonexistent -s /usr/sbin/nologin
chown -R boot-err-shim /var/db/boot-err-shim

cp /usr/local/etc/boot-err-shim.conf.sample /usr/local/etc/boot-err-shim.conf
$EDITOR /usr/local/etc/boot-err-shim.conf

# 0640 root:boot-err-shim, NOT 0600 root:wheel. The daemon runs as
# boot-err-shim and cannot read a root-owned 0600 file, which is the first
# thing that goes wrong. Group-owned means it can read the password and
# cannot rewrite its own config.
chown root:boot-err-shim /usr/local/etc/boot-err-shim.conf
chmod 640 /usr/local/etc/boot-err-shim.conf

sysrc boot_err_shim_enable=YES
service boot_err_shim start
```

`service boot_err_shim start` checks all of the above before it forks, and
names whichever one is wrong. On Linux none of this applies: the systemd unit
uses `DynamicUser` and `LoadCredential`, so there is no account to create and
the config stays root-owned `0600`.

### Updating an existing install

```sh
git pull
service boot_err_shim stop        # only if it is running
make install-freebsd              # or install-linux
service boot_err_shim start
```

`make install-*` is safe to re-run. It replaces the binary, the rc script or
unit, and the `.conf.sample`. It does **not** touch your `boot-err-shim.conf`,
your calibration, the intervention history or the snapshot ring buffer, and it
re-asserts ownership of the state directory in case `install(1)` reset it.

**Your calibration survives an update.** The format is versioned and unchanged,
which matters because re-running `configure` needs the host to be sitting at
the error again -- not something you can arrange on demand. If a future release
does change the format, the daemon says so by name and refuses to act rather
than misreading it.

### Linux (Ubuntu 26.04 and similar)

```sh
sudo make install-linux
sudo cp /etc/boot-err-shim.conf.sample /etc/boot-err-shim.conf
sudoedit /etc/boot-err-shim.conf
sudo chmod 600 /etc/boot-err-shim.conf
sudo systemctl enable --now boot-err-shim
journalctl -u boot-err-shim -f
```

### Single file, no install

Nothing outside the standard library is imported at runtime, so the whole
program bundles into one executable file:

```sh
make bundle          # produces boot-err-shim.pyz
scp boot-err-shim.pyz root@host:/usr/local/sbin/boot-err-shim
```

`make bundle` bakes the **absolute** path of the building host's interpreter
into the shebang. `#!/usr/bin/env python3` looks more portable and is worse:
rc(8) and cron run with `PATH=/sbin:/bin:/usr/sbin:/usr/bin`, FreeBSD keeps
python3 in `/usr/local/bin`, and the result is a daemon that runs perfectly by
hand and fails at boot with `env: python3: No such file or directory`.

So if you build somewhere other than the target, say where python3 lives
there:

```sh
make bundle INTERPRETER=/usr/local/bin/python3     # building for FreeBSD
```

Building on the target with `make install-freebsd` gets this right on its own,
and `service boot_err_shim start` checks the shebang resolves before it tries.

### From a checkout, with uv

```sh
uv sync
uv run boot-err-shim check-config -c ./boot-err-shim.conf
```

## Setting up the iDRAC

The daemon talks to the **iDRAC VNC server**, which is not the same thing as
the Virtual Console applet and is disabled by default.

1. iDRAC web UI → Configuration → Virtual Console → **VNC Server**.
2. Enable it, set a password, and note the port (5901 by default).
3. Leave *SSL Encryption* disabled, or set `tls = true` in the config if you
   enable it.
4. Check it: `boot-err-shim capture -o /tmp/screen.png`.

The VNC password is limited to **8 characters** by the RFB protocol. Anything
longer is silently truncated by every implementation, including this one.

## Where are the logs?

`boot-err-shim check-config` tells you, for your config, on your machine. In
general:

**Linux (systemd).** `journalctl -u boot-err-shim -f`. The unit captures
stderr, and the daemon notices journald and skips syslog so nothing appears
twice.

**FreeBSD (rc.d).** `daemon(8)` sends stderr to `/dev/null`, so syslog is the
only sink — and this catches people out:

> FreeBSD's stock `/etc/syslog.conf` routes `*.notice` and above to
> `/var/log/messages`. This program logs routine events at **INFO**, which is
> below that, so a perfectly healthy daemon looks completely silent. The first
> thing you see is a `WARNING`.

Two ways to see everything:

```sh
# either: give it its own syslog file
echo '*.info                                          /var/log/boot-err-shim.log' \
    >> /etc/syslog.conf
touch /var/log/boot-err-shim.log
service syslogd reload

# or: skip syslog and let the daemon write its own rotating file
#   log.file = "/var/log/boot-err-shim.log"    in boot-err-shim.conf
touch /var/log/boot-err-shim.log
chown boot-err-shim /var/log/boot-err-shim.log   # it runs unprivileged
```

That `chown` is not optional: `/var/log` is root-owned, and without it the
daemon cannot create the file. It says so and exits rather than starting up
half-configured.

What you should see once it is working, at `WARNING` and above:

```
boot-err-shim: WARNING ping.down host=10.0.0.50 reason=unreachable failures=3 threshold=3
boot-err-shim: WARNING key.pressed key=Y keysym=89 host=10.0.0.50
```

## Configuration

Start from `boot-err-shim.conf.sample`, which documents every key. Two things
worth knowing:

- **Unknown keys are a hard error**, not a warning. A typo you do not notice
  would otherwise mean a setting you believe is in force but is not — which,
  for a program that presses keys at a console, is not an acceptable failure
  mode.
- **The ping command is detected from the OS.** FreeBSD's `ping -W` takes
  milliseconds and Linux's takes seconds, so a command copied between the two
  silently breaks. Override it only if you must.

Durations accept plain seconds (`120`) or a suffix (`2m`, `90s`, `1h`).

## Commands

| Command | What it does |
|---|---|
| `run` | the daemon |
| `configure` | grab a frame, analyse it, write a calibration |
| `capture` | grab a frame and write a PNG, no analysis |
| `test-detect` | run the detector against a saved PNG; no hardware needed |
| `show-calibration` | print the calibration, including the learned font |
| `check-config` | validate the config, exit nonzero if it is wrong |

Useful flags: `configure --from screen.png` analyses a saved image instead of
connecting, so you can iterate on a calibration without rebooting anything.
`run --no-act` does everything except send the key. `test-detect --annotate
out.png` writes a copy of the frame with a box drawn around exactly the pixels
the detector compared.

`show-calibration --glyphs` prints the font it learned from your console. A
zero pixel delta says the glyphs are self-consistent; only looking at them
says they are the letters they claim to be, and anyone can tell a `D` from a
smear.

## What it does, in order

```
ping the host
├─ up   → sleep ping.interval
└─ down → count the failure
   ├─ below ping.threshold → sleep ping.retry_interval
   └─ at or past it        → look at the console
        connect over VNC
        ├─ failed  → log, sleep recovery.interval, ping again
        └─ opened  → capture a frame, always save it as a PNG
             ├─ prompt found     → press the key, sleep
             │                     recovery.post_fix_sleep, reset the count
             └─ prompt not found → sleep recovery.interval, ping again
```

Every examined frame is written to a ring buffer under the state directory,
whether it matched or not. Without that, a false negative is undiagnosable —
the log says "not found" and you cannot tell whether the screen differed, the
font differed, or the frame came back blank. Any of those files can be fed
straight back into `configure --from` or `test-detect`.

## Testing

Tested to the standard of `docs/testing-methodology.md` in
[calebpower/reaper](https://github.com/calebpower/reaper) — a portfolio of
oracles, each tier justified by a question cheaper tiers cannot answer. See
[docs/testing.md](docs/testing.md) for the tier map, what each one proves, and
what none of them prove.

```sh
uv run python -m unittest discover -s tests -t .   # tiers 1-5, 7-9
make test-hostile                                  # again under LANG=C
make test-e2e                                      # tier 6, in podman
make test-mutants                                  # break the code, expect red
```

`make test-mutants` is the acceptance check for the suite itself: it introduces
deliberate defects one at a time and requires each to turn the suite red. A
mutant that survives means the suite has a hole.

## Licence

MIT. Copyright (c) 2026 Caleb L. Power. See [LICENSE](LICENSE).
