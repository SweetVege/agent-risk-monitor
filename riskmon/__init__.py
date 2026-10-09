"""Risk monitor for autonomous agents: adapters, three scoring tiers, and a policy that maps scores to verdicts."""

DIMS = ("boundary", "deviation", "stealth", "resource")
DIM_LABELS = {
    "boundary": "boundary",
    "deviation": "off-task",
    "stealth": "stealth",
    "resource": "resource",
}
