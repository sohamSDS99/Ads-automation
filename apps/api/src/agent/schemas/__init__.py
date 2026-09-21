"""Contracts that cross a stage boundary.

Distinct from `agent.api.schemas_*`, which are request and response shapes, and
from `agent.export.contract`, which is the Stage 01 report. What lives here is
what one stage hands the next: typed, versioned, and hashed, so the receiving
stage can say exactly what it was given.
"""
