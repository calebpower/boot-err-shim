#!/bin/sh
# Tier 6: the full stack, in named stages, started hostile.
#
# Each stage is runnable on its own:
#
#     make test-e2e                     # every stage
#     make test-e2e STAGES="calibrate"  # just one
#
# What distinguishes this tier from the fake-server one is the oracle. Down
# there our client talks to a server we wrote, and the two would agree just as
# happily on a shared misreading of RFB. Here it talks to TigerVNC, reading
# text drawn by xterm in a -misc-fixed bitmap font, and neither of those has
# ever heard of us.
set -eu

VNC_PORT=5901
VNC_PASSWORD=secret12
WIDTH=720
HEIGHT=400
FONT=9x15
WORK=/work
STATE=/tmp/shim-state
export PYTHONPATH=$WORK/src

PASSED=0
FAILED=0
SKIPPED=0
FAILED_NAMES=""
SKIPPED_NOTES=""

# -- reporting ---------------------------------------------------------

banner() {
    printf '\n======================================================================\n'
    printf '%s\n' "$1"
    printf -- '----------------------------------------------------------------------\n'
}

ok() {
    PASSED=$((PASSED + 1))
    printf '  [ok]   %s\n' "$1"
}

fail() {
    FAILED=$((FAILED + 1))
    FAILED_NAMES="$FAILED_NAMES
  - $1"
    printf '  [FAIL] %s\n' "$1"
}

# Every narrowing states its reason, and the reason appears in the summary.
skip() {
    SKIPPED=$((SKIPPED + 1))
    SKIPPED_NOTES="$SKIPPED_NOTES
  - $1
      reason: $2"
    printf '  [skip] %s\n         reason: %s\n' "$1" "$2"
}

check() {
    if [ "$1" = "0" ]; then ok "$2"; else fail "$2"; fi
}

# -- the fake console --------------------------------------------------

THE_MESSAGE_1="Disabling writes to flash as the flash part has gone bad."
THE_MESSAGE_2="Please contact technical support to resolve this issue."
THE_MESSAGE_3="Please press 'Y' to continue."

start_vnc() {
    # $1: extra Xvnc arguments
    mkdir -p /tmp/vnc
    printf '%s\n' "$VNC_PASSWORD" | tigervncpasswd -f > /tmp/vnc/passwd 2>/dev/null
    chmod 600 /tmp/vnc/passwd

    Xvnc :1 \
        -geometry "${WIDTH}x${HEIGHT}" \
        -depth 24 \
        -rfbport "$VNC_PORT" \
        -SecurityTypes VncAuth \
        -PasswordFile /tmp/vnc/passwd \
        -localhost=0 \
        -AlwaysShared \
        -NeverShared=0 \
        ${1:-} \
        > /tmp/vnc/Xvnc.log 2>&1 &
    XVNC_PID=$!

    for _ in $(seq 1 50); do
        if xdpyinfo -display :1 >/dev/null 2>&1; then return 0; fi
        sleep 0.2
    done
    echo "Xvnc did not start; log follows:" >&2
    cat /tmp/vnc/Xvnc.log >&2
    return 1
}

#: Where the console records the first keystroke it is sent.
KEYPRESS_FILE=/tmp/vnc/keypress.txt

write_console_script() {
    # Runs inside the xterm: draw the message, then record every keystroke
    # the terminal receives, forever.
    #
    # An earlier version used `dd bs=1 count=1`, which is one-shot and gave a
    # false negative that looked exactly like the daemon failing to send a
    # key. A continuous recorder cannot be silently spent, and it also lets a
    # stage count keystrokes rather than merely detect the first.
    #
    # stty matters twice over: -echo keeps a keystroke from repainting the
    # screen and invalidating the calibrated region, and -icanon min 1 is
    # what delivers a bare 'Y' at all -- in canonical mode the tty buffers
    # until a newline arrives, which for a firmware prompt never happens.
    cat > /tmp/vnc/console.sh <<'CONSOLE'
#!/bin/sh
stty -echo -icanon min 1 time 0 2>/dev/null
printf '%s\n%s\n%s\n' "$1" "$2" "$3"
exec python3 -u -c '
import os, sys
path = sys.argv[1]
with open(path, "ab", buffering=0) as handle:
    while True:
        char = os.read(0, 1)
        if not char:
            break
        handle.write(char)
' "$KEYPRESS_FILE"
CONSOLE
    chmod +x /tmp/vnc/console.sh
}

show_message() {
    # A real terminal emulator drawing a real bitmap font. -b 0 removes the
    # inner border and +sb the scrollbar, so the text grid starts at a
    # predictable place; calibration does not require that, but it makes a
    # failure easier to look at.
    #
    # The shell inside captures the first keystroke to a file. That is the
    # second oracle for the claim that matters: "we sent a key" is our own
    # log talking about itself, while this is the console saying what it
    # actually received.
    #
    # stty does two necessary things. -echo stops the keystroke repainting
    # the screen and invalidating the calibrated region. -icanon min 1 is
    # what makes a single character arrive at all: in the default canonical
    # mode the tty buffers until a newline, so `dd count=1` blocks forever on
    # a bare 'Y' -- which looked exactly like the keystroke never being sent.
    rm -f "$KEYPRESS_FILE"
    write_console_script
    KEYPRESS_FILE="$KEYPRESS_FILE" DISPLAY=:1 xterm \
        -fn "$FONT" -bg black -fg white -b 0 +sb \
        -geometry "80x25+0+0" \
        -e /tmp/vnc/console.sh \
        "$THE_MESSAGE_1" "$THE_MESSAGE_2" "$THE_MESSAGE_3" \
        > /tmp/vnc/xterm.log 2>&1 &
    XTERM_PID=$!
    sleep 3
    focus_console
}

focus_console() {
    # There is no window manager here, so nothing assigns input focus, and a
    # key synthesised by Xvnc goes nowhere. A real console always has focus;
    # this restores that property rather than testing around its absence.
    window=$(DISPLAY=:1 xdotool search --class xterm 2>/dev/null | tail -1)
    if [ -n "$window" ]; then
        DISPLAY=:1 xdotool windowfocus "$window" 2>/dev/null || true
        # Park the pointer in the far corner. Xvnc paints the cursor into the
        # framebuffer, and at 10,10 it sat directly on top of the first line
        # of the message -- which stopped the prompt being detected at all.
        # Moved once, before calibration, so the screen stays identical
        # afterwards.
        DISPLAY=:1 xdotool mousemove $((WIDTH - 1)) $((HEIGHT - 1)) 2>/dev/null || true
        sleep 1
    fi
}

# Keystrokes are counted, never deleted.
#
# The recorder holds the file open, so removing it between checks unlinks the
# inode it is still writing into: every later keystroke lands in a file with
# no name and the check reports nothing received. That produced a false
# negative indistinguishable from the daemon failing to send the key, in a
# stage whose entire purpose is to be the independent oracle. Taking a
# baseline and comparing counts avoids touching the file at all.
keypress_count() {
    wc -c < "$KEYPRESS_FILE" 2>/dev/null | tr -d ' ' || printf '0'
}

keypress_received() {
    [ -s "$KEYPRESS_FILE" ]
}

keypress_value() {
    cat "$KEYPRESS_FILE" 2>/dev/null || printf '(none)'
}

show_other_screen() {
    DISPLAY=:1 xterm \
        -fn "$FONT" -bg black -fg white -b 0 +sb \
        -geometry "80x25+0+0" \
        -e sh -c "printf 'No boot device available.\nPress F1 to retry boot.\nPress F2 for setup utility.\n'; sleep 100000" \
        > /tmp/vnc/xterm2.log 2>&1 &
    XTERM_PID=$!
    sleep 3
    focus_console
}

stop_console() {
    [ -n "${XTERM_PID:-}" ] && kill "$XTERM_PID" 2>/dev/null || true
    [ -n "${XVNC_PID:-}" ] && kill "$XVNC_PID" 2>/dev/null || true
    XTERM_PID=""
    XVNC_PID=""
    sleep 1
}

write_config() {
    # $1: destination, $2: extra lines
    mkdir -p "$STATE"
    cat > "$1" <<CONFIG
[state]
dir = "$STATE"

[target]
host = "192.0.2.1"

[ping]
threshold      = 1
interval       = 1
retry_interval = 1
timeout        = 5

[vnc]
host     = "127.0.0.1"
port     = $VNC_PORT
password = "$VNC_PASSWORD"

[detect]
text = """
$THE_MESSAGE_1
$THE_MESSAGE_2
$THE_MESSAGE_3
"""

[recovery]
interval       = 1
post_fix_sleep = 1

[log]
syslog = "never"
${2:-}
CONFIG
    chmod 600 "$1"
}

shim() {
    python3 -m boot_err_shim.cli "$@"
}

# -- stages ------------------------------------------------------------

stage_vectors() {
    banner "vectors: the pure suite, in this hostile environment"
    printf '  locale: LANG=%s filesystem encoding=%s\n' \
        "${LANG:-unset}" "$(python3 -c 'import sys; print(sys.getfilesystemencoding())')"

    if python3 -m unittest discover -s tests -t . > /tmp/unit.log 2>&1; then
        ok "$(tail -3 /tmp/unit.log | head -1)"
    else
        tail -40 /tmp/unit.log
        fail "the unit suite does not pass under LANG=C"
    fi
}

stage_handshake() {
    banner "handshake: our client against TigerVNC"
    start_vnc
    show_message

    write_config /tmp/shim.conf
    if shim capture -c /tmp/shim.conf -o /tmp/tigervnc.png > /tmp/capture.log 2>&1; then
        cat /tmp/capture.log | sed 's/^/  /'
        ok "captured a frame from TigerVNC"
    else
        cat /tmp/capture.log | sed 's/^/  /'
        fail "could not capture from TigerVNC"
        stop_console
        return
    fi

    grep -q "framebuffer: ${WIDTH}x${HEIGHT}" /tmp/capture.log \
        && ok "geometry matches what Xvnc was told to serve" \
        || fail "geometry does not match"

    size=$(wc -c < /tmp/tigervnc.png)
    [ "$size" -gt 1000 ] \
        && ok "PNG is $size bytes" \
        || fail "PNG is implausibly small ($size bytes)"

    stop_console
}

stage_auth() {
    banner "auth: the DES bit-reversal quirk, against a real server"
    start_vnc
    show_message

    write_config /tmp/shim.conf
    shim capture -c /tmp/shim.conf -o /tmp/auth.png > /tmp/auth.log 2>&1
    check $? "TigerVNC accepted our challenge response"
    grep -q "security offered: \[2 VNC authentication\]" /tmp/auth.log \
        && ok "server offered VNC authentication" \
        || fail "unexpected security types: $(grep 'security offered' /tmp/auth.log || true)"

    # The negative: a wrong password must be refused, not silently accepted.
    sed 's/^password = .*/password = "wrongpwd"/' /tmp/shim.conf > /tmp/bad.conf
    chmod 600 /tmp/bad.conf
    if shim capture -c /tmp/bad.conf -o /tmp/bad.png > /tmp/bad.log 2>&1; then
        fail "a wrong password was accepted"
    else
        ok "a wrong password was refused"
    fi

    stop_console
}

stage_tls() {
    banner "tls: the encrypted transport"
    skip "RFB-over-TLS against TigerVNC" \
        "TigerVNC offers TLS through the VeNCrypt security type (19), which \
this client does not implement; iDRAC's SSL option is a plain TLS wrapper \
around the RFB port instead. Exercising the real thing needs an iDRAC, so \
this is covered only by the unit tests for the config gate and by tier 10."
}

stage_capture() {
    banner "capture: pixels survive the round trip"
    start_vnc
    show_message
    write_config /tmp/shim.conf

    shim capture -c /tmp/shim.conf -o /tmp/a.png > /dev/null 2>&1
    shim capture -c /tmp/shim.conf -o /tmp/b.png > /dev/null 2>&1
    if cmp -s /tmp/a.png /tmp/b.png; then
        ok "two captures of a static screen are byte-identical"
    else
        fail "captures of a static screen differ"
    fi

    python3 - <<'PY' && ok "the captured frame has both ink and background" || fail "the captured frame looks blank"
import sys
sys.path.insert(0, "/work/src")
from boot_err_shim.bitmap import binarise
from boot_err_shim.png import read_frame
b = binarise(read_frame("/tmp/a.png"))
print(f"  ink {b.ink_fraction:.2%}, fg {b.foreground}, bg {b.background}, "
      f"contrast {b.contrast:.1f}:1")
sys.exit(0 if 0.001 < b.ink_fraction < 0.5 else 1)
PY

    stop_console
}

stage_calibrate() {
    banner "calibrate: learn a font nobody here wrote"
    start_vnc
    show_message
    write_config /tmp/shim.conf

    if shim configure -c /tmp/shim.conf > /tmp/configure.log 2>&1; then
        sed 's/^/  /' /tmp/configure.log
        ok "calibrated against a -misc-fixed bitmap font over TigerVNC"
    else
        sed 's/^/  /' /tmp/configure.log
        fail "could not calibrate"
        stop_console
        return
    fi

    grep -q "0 px differ" /tmp/configure.log \
        && ok "verified exactly: the learned glyphs reproduce the screen" \
        || fail "verification was not exact"

    # Idempotence, per the methodology: anything that re-runs must be
    # assertable as unchanged.
    cp "$STATE/calibration.toml" /tmp/first.toml
    shim configure -c /tmp/shim.conf > /dev/null 2>&1
    cmp -s /tmp/first.toml "$STATE/calibration.toml" \
        && ok "a second configure produces a byte-identical calibration" \
        || fail "configure is not idempotent"

    # No backdoors: the calibration must have come through the real VNC path.
    grep -q "connecting 127.0.0.1:$VNC_PORT" /tmp/configure.log \
        && ok "the calibration came over the wire, not from a file" \
        || fail "configure did not connect"

    stop_console
}

stage_detect() {
    banner "detect: the matcher, and the screens it must refuse"
    start_vnc
    show_message
    write_config /tmp/shim.conf
    shim configure -c /tmp/shim.conf > /dev/null 2>&1
    shim capture -c /tmp/shim.conf -o /tmp/error-screen.png > /dev/null 2>&1
    stop_console

    shim test-detect -c /tmp/shim.conf /tmp/error-screen.png > /tmp/detect.log 2>&1
    check $? "the error screen matches"
    sed 's/^/  /' /tmp/detect.log

    # The negative, on a real screen rather than a synthetic one.
    start_vnc
    show_other_screen
    shim capture -c /tmp/shim.conf -o /tmp/other-screen.png > /dev/null 2>&1
    stop_console

    if shim test-detect -c /tmp/shim.conf /tmp/other-screen.png > /tmp/detect2.log 2>&1; then
        sed 's/^/  /' /tmp/detect2.log
        fail "an unrelated screen matched"
    else
        ok "an unrelated POST screen does not match"
        grep -q "screen reads" /tmp/detect2.log \
            && ok "the log says what the screen actually said" \
            || fail "no screen text was reported"
    fi
}

stage_loop() {
    banner "loop: the daemon, end to end, on a real console"
    start_vnc
    show_message
    write_config /tmp/shim.conf
    shim configure -c /tmp/shim.conf > /dev/null 2>&1

    # ping must genuinely report the target down for the loop to advance.
    if ping -c 1 -W 2 192.0.2.1 > /dev/null 2>&1; then
        fail "TEST-NET-1 answered a ping; the failure path is unreachable"
        stop_console
        return
    fi
    ok "ping reports the unroutable target as down"

    shim run -c /tmp/shim.conf --once --no-act > /tmp/noact.log 2>&1
    check $? "--no-act completed a cycle"
    grep -q "detect.match" /tmp/noact.log \
        && ok "the prompt was detected" \
        || fail "the prompt was not detected"
    grep -q "key.suppressed" /tmp/noact.log \
        && ok "--no-act suppressed the keypress" \
        || fail "--no-act did not suppress the keypress"
    grep -q "key.pressed" /tmp/noact.log \
        && fail "a key was pressed under --no-act" \
        || ok "no keypress was logged under --no-act"

    # Two oracles for the claim that matters. The log is us describing our
    # own behaviour; the console is an independent record of what actually
    # arrived. Under --no-act the second must be empty.
    keypress_received \
        && fail "the console received a keystroke under --no-act" \
        || ok "the console received nothing under --no-act"

    shim run -c /tmp/shim.conf --once > /tmp/run.log 2>&1
    check $? "a real cycle completed"
    grep -q "key.pressed" /tmp/run.log \
        && ok "our log says the key was pressed" \
        || fail "our log does not record a keypress"

    sleep 2
    if keypress_received; then
        ok "the console independently recorded a keystroke"
        [ "$(keypress_value)" = "Y" ] \
            && ok "the console received exactly 'Y'" \
            || fail "the console received '$(keypress_value)', not 'Y'"
    else
        fail "the console received nothing, though we logged a keypress"
    fi

    ok "$(ls "$STATE/snapshots" | wc -l) frame(s) in the ring buffer"

    stop_console
}

stage_concurrency() {
    banner "concurrency: two daemons, one real console"
    start_vnc
    show_message
    write_config /tmp/shim.conf
    shim configure -c /tmp/shim.conf > /dev/null 2>&1

    python3 -m unittest discover -s tests/concurrency -t . > /tmp/conc.log 2>&1
    check $? "the concurrency tier passes in this environment"
    grep -E "^(OK|FAILED|Ran )" /tmp/conc.log | sed 's/^/  /'

    # The tier-6 version of the invariant: not a fake server and an assertion
    # about exit codes, but a real console with its own record of what it
    # received while a lock was held elsewhere.
    lock=$(python3 -c "
import sys
sys.path.insert(0, '$WORK/src')
from boot_err_shim.config import load_config
print(load_config('/tmp/shim.conf').lock_path)
")
    ok "lock for this console: $lock"

    python3 - "$lock" <<'PY' > /tmp/holder.log 2>&1 &
import sys, time
sys.path.insert(0, "/work/src")
from boot_err_shim.lock import SingleInstanceLock
with SingleInstanceLock(sys.argv[1]):
    print("held", flush=True)
    time.sleep(45)
PY
    holder=$!
    sleep 3

    before=$(keypress_count)
    if shim run -c /tmp/shim.conf --once > /tmp/second.log 2>&1; then
        fail "a second daemon ran while the lock was held"
    else
        ok "the second daemon refused to start"
    fi

    sleep 2
    [ "$(keypress_count)" = "$before" ] \
        && ok "the console received nothing from the second daemon" \
        || fail "a second daemon pressed a key at a console another owned"

    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true

    # And once the holder is gone, the daemon works again -- otherwise this
    # stage would pass just as well with a permanently broken daemon.
    if kill -0 "$XTERM_PID" 2>/dev/null; then
        ok "the console process is still alive"
    else
        fail "the console process died during this stage"
    fi

    before=$(keypress_count)
    shim run -c /tmp/shim.conf --once > /tmp/third.log 2>&1
    check $? "the daemon runs once the lock is free"
    sleep 2
    if [ "$(keypress_count)" != "$before" ]; then
        ok "and the console received the keystroke"
    else
        sed 's/^/      /' /tmp/third.log
        fail "the console received nothing after the lock was released"
    fi

    stop_console
}

stage_ocr() {
    banner "ocr: the fallback engine"
    if ! command -v tesseract > /dev/null 2>&1; then
        skip "tesseract fallback" "tesseract is not installed in this image"
        return
    fi
    ok "tesseract $(tesseract --version 2>&1 | head -1 | awk '{print $2}') is available"
    skip "engine = \"ocr\" end to end" \
        "The OCR engine is declared in config but not yet implemented; it is \
held in reserve for a console that cannot be calibrated. Nothing to exercise \
until a real iDRAC shows that it is needed."
}

stage_service_linux() {
    banner "service-linux: the systemd unit"
    if [ ! -f "$WORK/init/boot-err-shim.service" ]; then
        skip "systemd unit" "init/boot-err-shim.service does not exist yet"
        return
    fi

    if command -v systemd-analyze > /dev/null 2>&1; then
        if systemd-analyze verify "$WORK/init/boot-err-shim.service" \
                > /tmp/unit.log 2>&1; then
            ok "systemd-analyze verify accepts the unit"
        else
            sed 's/^/  /' /tmp/unit.log
            fail "systemd-analyze verify rejected the unit"
        fi
    else
        skip "unit verification" "systemd-analyze is not available"
    fi

    skip "booting the unit under systemd" \
        "Requires podman --systemd=always with a writable cgroup hierarchy, \
which rootless podman on a WSL machine does not provide. The unit is checked \
statically above and by the structural tier; running it is covered only on a \
real host."
}

stage_hostile_text() {
    banner "hostile-text: a message the font cannot render"
    start_vnc
    show_message

    # Astral-plane and CJK characters in detect.text. The analyser must fail
    # with a typed error rather than crash: it cannot learn a glyph for a
    # character that is not on screen.
    write_config /tmp/hostile.conf
    python3 - <<'PY'
from pathlib import Path
p = Path("/tmp/hostile.conf")
t = p.read_text(encoding="utf-8")
t = t.replace("Please press 'Y' to continue.", "テスト \U0001f4a5 press 'Y'")
p.write_text(t, encoding="utf-8")
PY
    chmod 600 /tmp/hostile.conf

    if shim configure -c /tmp/hostile.conf -o /tmp/hostile.toml \
            > /tmp/hostile.log 2>&1; then
        fail "calibrated against text that is not on screen"
    else
        if grep -q "Traceback" /tmp/hostile.log; then
            sed 's/^/  /' /tmp/hostile.log
            fail "crashed instead of reporting a typed error"
        else
            ok "refused cleanly: $(grep -m1 'COULD NOT CALIBRATE' /tmp/hostile.log || echo 'typed error')"
        fi
    fi

    stop_console
}

stage_regressions() {
    banner "regressions: the mutation acceptance check"
    if python3 tools/mutate.py > /tmp/mutants.log 2>&1; then
        grep -E "^All |NOT VERIFIED" /tmp/mutants.log | sed 's/^/  /'
        ok "every applicable mutant was caught"
    else
        grep -E "SURVIVED|^  [a-z-]+:" /tmp/mutants.log | sed 's/^/  /'
        fail "a mutant survived"
    fi
}

# -- driver ------------------------------------------------------------

ALL_STAGES="vectors handshake auth tls capture calibrate detect loop concurrency ocr hostile-text service-linux regressions"

run_stage() {
    case "$1" in
        vectors)        stage_vectors ;;
        handshake)      stage_handshake ;;
        auth)           stage_auth ;;
        tls)            stage_tls ;;
        capture)        stage_capture ;;
        calibrate)      stage_calibrate ;;
        detect)         stage_detect ;;
        loop)           stage_loop ;;
        concurrency)    stage_concurrency ;;
        ocr)            stage_ocr ;;
        hostile-text)   stage_hostile_text ;;
        service-linux)  stage_service_linux ;;
        regressions)    stage_regressions ;;
        *)
            echo "unknown stage: $1" >&2
            echo "stages: $ALL_STAGES" >&2
            exit 2
            ;;
    esac
}

main() {
    if [ "${1:-all}" = "all" ]; then
        set -- $ALL_STAGES
    fi

    # A stage that dies must not take the run with it; the summary is the
    # point.
    for stage in "$@"; do
        rm -rf "$STATE"
        mkdir -p "$STATE"
        set +e
        run_stage "$stage"
        status=$?
        set -e
        if [ "$status" != "0" ]; then
            fail "stage $stage exited $status"
        fi
        stop_console 2>/dev/null || true
    done

    printf '\n======================================================================\n'
    printf 'passed %s, failed %s, skipped %s\n' "$PASSED" "$FAILED" "$SKIPPED"

    if [ -n "$SKIPPED_NOTES" ]; then
        printf '\nNot proved by this run:%s\n' "$SKIPPED_NOTES"
    fi
    if [ "$FAILED" != "0" ]; then
        printf '\nFailures:%s\n' "$FAILED_NAMES"
        exit 1
    fi
    exit 0
}

main "$@"
