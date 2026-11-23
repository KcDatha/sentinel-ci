"""The six SENTINEL agents, as LangGraph node functions.

Each node takes the shared :class:`~sentinel_ci.state.SentinelCIState`
and returns a partial update. Nodes are plain functions with no graph
machinery inside, so each stage of BUILD_PLAN.md can be implemented and
tested independently of the others.
"""

from .adversarial_verification import adversarial_verification
from .codebase_analyst import codebase_analyst
from .controller import controller
from .failure_hypothesis import failure_hypothesis
from .repair import repair
from .root_cause import root_cause

NODE_NAMES = (
    "controller",
    "codebase_analyst",
    "failure_hypothesis",
    "root_cause",
    "repair",
    "adversarial_verification",
)

__all__ = [
    "NODE_NAMES",
    "controller",
    "codebase_analyst",
    "failure_hypothesis",
    "root_cause",
    "repair",
    "adversarial_verification",
]
