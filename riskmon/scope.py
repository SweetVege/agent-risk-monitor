"""Scope policy: say outright how a class of action is handled in a given mode, without scoring.

Scoring means "the more suspicious, the closer the look". Scope policy means "this is not done
here". The two coexist: what the policy leaves at "score" is scored as before; what it rules on,
the judge cannot overturn and only a person can approve.

Each rule takes one of three values:
  score    leave it to scoring (the default)
  confirm  a person must agree: a prompt when attended, a hold for review when unattended
  deny     block it
"""
from __future__ import annotations

# rule → (the behaviour tag that triggers it, how the reason describes it)
RULES = {
    "outside_write": ("outside_write", "writing outside the workspace"),
    "whole_machine_search": ("whole_search", "searching the whole machine or home directory"),
    "outside_search": ("outside_search", "searching outside the workspace"),
    "outside_read": ("outside_workspace", "touching a path outside the workspace"),
    "unlisted_host": ("egress", "contacting a host that is not on the allowlist"),
    "protected_path": ("protected_access", "touching a protected path"),
    "unknown_program": ("unknown_program", "running a program that is not on the known list"),
}
_RANK = {"score": 0, "confirm": 1, "deny": 2}


def decide(facets: set, unattended: bool, cfg: dict) -> tuple[str, list[str]]:
    """The strictest handling among the rules this action hits, and which ones. ("score", []) when none apply."""
    mode = "unattended" if unattended else "attended"
    policy = cfg["scope"][mode]
    strictest, hits = "score", []
    for rule, (facet, label) in RULES.items():
        action = policy.get(rule, "score")
        if facet in facets and action != "score":
            hits.append((action, label))
            if _RANK[action] > _RANK[strictest]:
                strictest = action
    if strictest == "score":
        return "score", []
    # keep only the reasons that match the final handling, so a deny is not listed with "needs approval"
    outcome = "is not allowed" if strictest == "deny" else "needs a person's approval"
    where = " in an unattended run" if unattended else ""
    return strictest, [f"scope policy: {label}{where} {outcome}" for action, label in hits if action == strictest]
