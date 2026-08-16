"""Tier 4: contract tests over the decision rules.

Fast, in-process, no I/O of any kind. The question only this tier answers is
whether the rules governing the loop hold in isolation -- which is the question
you want answered before any of it is wired to a socket.

The existing rows passing unchanged is the gate on refactoring daemon.py.
"""
