"""The planning DAG (Stage 02 PRD §11).

Empty of real nodes in S2-P0. What lives here is the two-node placeholder the
handshake needs: a plan run has to be startable, executable and observable
over SSE before there is anything worth planning, or the handshake could only
be tested against a mock of the thing it hands off to.

S2-P2 replaces `handshake.py` with nodes 2.1.1–2.1.4 and the stages that
follow. It is the only file in this package that S2-P2 deletes rather than
extends.
"""
