"""Tier 8: does the invariant survive simultaneous actors?

The daemon is single-threaded, so the concurrency here is between *processes*
and between a process and a signal. Three invariants, and the first is the one
that matters:

**Two daemons must not both press the key.** A stray keystroke at a firmware
prompt is the worst thing this program can do, and two instances watching one
iDRAC will each independently decide the host is down. Following the
methodology, the assertion is on the resource's own state read back
afterwards -- how many key events the console actually received -- and not on
what either process reported doing.

**A signal must never leave a torn file.** SIGTERM during a calibration write
would otherwise leave something unparseable, and the daemon would then refuse
to press anything on next start: a clean shutdown turned into an outage.

**A signal must not be delayed by a sleep.** A ten-minute post-fix sleep must
not mean a ten-minute `service stop`.
"""
