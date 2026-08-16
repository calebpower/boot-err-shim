"""Tier 5: real client, real sockets, fake server.

The question only this tier answers: what happens when the transport fails?

A live VNC server will produce a clean refusal on demand, and that is the easy
case. The failures that actually take daemons down are the untidy ones -- a
server that accepts and then stops talking, a frame that stops halfway, a peer
that sends one byte per second. None of those can be provoked reliably against
real hardware, and all of them are a few lines here.

This tier is why every read in rfb.py carries a deadline rather than a socket
timeout.
"""
