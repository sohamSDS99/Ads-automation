"""Stage 04's creative nodes.

S4-P0 registers exactly two placeholders (`_dummy.py`) so a creative run can
execute end to end. S4-P4 replaces both re-exports below with the real nodes.
"""

from agent.nodes.creative._dummy import CREATIVE_DUMMY_END, CREATIVE_DUMMY_START

__all__ = ["CREATIVE_DUMMY_END", "CREATIVE_DUMMY_START"]
