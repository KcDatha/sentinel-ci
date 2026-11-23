"""The SENTINEL CI state graph.

Topology (the Mission Controller owns routing, per the SENTINEL
pattern)::

    controller -> codebase_analyst -> failure_hypothesis -> root_cause
    root_cause  -> failure_hypothesis (hypothesis rejected, attempts remain)
    root_cause  -> repair             (only on a confirmed root cause)
    repair      -> adversarial_verification -> controller
    controller  -> repair             (gate failed, attempts remain)
    controller  -> adversarial_verification (a resumed incident whose
                                       patch is held at the gate and
                                       which carries a local checkout)
    controller  -> END                (verified, escalated, or a
                                       proposed patch held at the
                                       verification gate with no local
                                       checkout to run it against)

When ``langgraph`` is installed, :func:`build_graph` returns a real
compiled LangGraph ``StateGraph``. When it is not, it returns a
:class:`FallbackGraph` that walks the identical topology with the
identical routing functions, so the pipeline is runnable and testable
in any environment. The routing functions below are shared by both.
"""

from __future__ import annotations

from functools import partial

from .agents import (
    adversarial_verification,
    codebase_analyst,
    controller,
    failure_hypothesis,
    repair,
    root_cause,
)
from .config import Config
from .state import SentinelCIState

END = "__end__"


# -- routing (shared by LangGraph edges and the fallback runner) --------

def route_after_controller(state: SentinelCIState) -> str:
    if state.get("status") in {"verified", "ready_for_pr", "escalated"}:
        return END
    if state.get("verification_verdict") in {"failed", "inconclusive"}:
        return "repair"
    if state.get("status") == "awaiting_verification":
        if state.get("repo_path"):
            # A patch was proposed in an earlier run and the incident
            # carries a local checkout: resume at the Stage 3 gate.
            return "adversarial_verification"
        # No checkout, so the gate cannot run: the incident stays
        # held at the Stage 2 stop, patch untrusted.
        return END
    return "codebase_analyst"


def route_after_root_cause(
    state: SentinelCIState,
    confidence_threshold: float,
    max_hypothesis_attempts: int,
) -> str:
    if state.get("status") == "escalated":
        return END
    if state.get("root_cause") and (
        state.get("root_cause_confidence", 0.0) >= confidence_threshold
    ):
        return "repair"
    if (
        state.get("root_cause_verdict") == "rejected"
        and state.get("hypothesis_attempts", 0) < max_hypothesis_attempts
    ):
        return "failure_hypothesis"
    return END


# -- fallback runner ------------------------------------------------------

class FallbackGraph:
    """The same topology without LangGraph. ``invoke`` mirrors the
    compiled graph's behaviour, including the hypothesis retry loop
    and the repair and verify loop."""

    def __init__(self, config: Config):
        self._config = config

    def invoke(self, state: SentinelCIState) -> SentinelCIState:
        cfg = self._config
        s: dict = dict(state)

        def apply(node, **kwargs) -> None:
            s.update(node(s, **kwargs))

        apply(controller, max_repair_attempts=cfg.max_repair_attempts)
        route = route_after_controller(s)
        if route == "adversarial_verification":
            # Resumed incident: the patch already exists and the gate
            # can run. Verify, let the controller judge, and fall
            # through to the repair loop if the gate failed with
            # attempts remaining.
            apply(adversarial_verification, config=cfg)
            apply(controller, max_repair_attempts=cfg.max_repair_attempts)
            route = route_after_controller(s)
        if route == "codebase_analyst":
            apply(codebase_analyst, config=cfg)
            while True:
                apply(failure_hypothesis, config=cfg)
                apply(root_cause, config=cfg)
                nxt = route_after_root_cause(
                    s, cfg.confidence_threshold, cfg.max_hypothesis_attempts
                )
                if nxt != "failure_hypothesis":
                    break
            if nxt != "repair":
                return s  # type: ignore[return-value]
        elif route != "repair":
            return s  # type: ignore[return-value]

        while True:
            apply(repair, config=cfg)
            apply(adversarial_verification, config=cfg)
            apply(controller, max_repair_attempts=cfg.max_repair_attempts)
            if route_after_controller(s) != "repair":
                break
        return s  # type: ignore[return-value]


# -- builder ---------------------------------------------------------------

def build_graph(config: Config | None = None):
    """A compiled LangGraph when available, else a FallbackGraph."""
    config = config or Config.from_env()
    try:
        from langgraph.graph import END as LG_END
        from langgraph.graph import StateGraph
    except ImportError:
        return FallbackGraph(config)

    builder = StateGraph(SentinelCIState)
    builder.add_node(
        "controller",
        partial(controller, max_repair_attempts=config.max_repair_attempts),
    )
    builder.add_node("codebase_analyst", partial(codebase_analyst, config=config))
    builder.add_node("failure_hypothesis", partial(failure_hypothesis, config=config))
    builder.add_node("root_cause", partial(root_cause, config=config))
    builder.add_node("repair", partial(repair, config=config))
    builder.add_node(
        "adversarial_verification", partial(adversarial_verification, config=config)
    )

    builder.set_entry_point("controller")
    builder.add_conditional_edges(
        "controller",
        route_after_controller,
        {
            "codebase_analyst": "codebase_analyst",
            "repair": "repair",
            "adversarial_verification": "adversarial_verification",
            END: LG_END,
        },
    )
    builder.add_edge("codebase_analyst", "failure_hypothesis")
    builder.add_edge("failure_hypothesis", "root_cause")
    builder.add_conditional_edges(
        "root_cause",
        partial(
            route_after_root_cause,
            confidence_threshold=config.confidence_threshold,
            max_hypothesis_attempts=config.max_hypothesis_attempts,
        ),
        {"repair": "repair", "failure_hypothesis": "failure_hypothesis", END: LG_END},
    )
    builder.add_edge("repair", "adversarial_verification")
    builder.add_edge("adversarial_verification", "controller")
    return builder.compile()
