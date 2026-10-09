"""L1: trajectory state.

One per session, updated incrementally. It works out the cases where each step is harmless but
the combination is not, and produces the compressed summary L2 reads, so no step has to re-read
the whole history.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from .events import ActionEvent
from .rules import L0Result

MAX_TASKS = 5
MAX_NOTABLE = 20
MAX_HOSTS = 50
MAX_APPROVED = 50
MAX_NET_STAGED = 30
MAX_OUTCOMES = 30
MAX_REGIONS = 40
WIDENING_AFTER = 3  # from which new location on the footprint counts as widening
CARRY_WEIGHT = 0.25
CARRY_CAP = 20
SIGNAL_FLOOR = 10  # actions scoring below this are not amplified by history, so an `ls` is not punished for what came before


@dataclass
class SessionState:
    session_id: str
    tasks: list = field(default_factory=list)
    step: int = 0
    carry: dict = field(default_factory=lambda: {"boundary": 0.0, "stealth": 0.0, "resource": 0.0})
    facets: dict = field(default_factory=dict)
    hosts: list = field(default_factory=list)
    staged: list = field(default_factory=list)
    net_staged: dict = field(default_factory=dict)  # written file → the unlisted hosts it refers to
    regions: list = field(default_factory=list)  # locations touched outside the workspace, in order of first appearance
    notable: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)  # outcomes of recent allowed actions: "ok" or "failed"
    drift: float = 0.0  # drift score: the recent trend of the footprint widening, kept at 0 when no drift score is installed
    drift_peak: float = 0.0
    denies: int = 0
    holds: int = 0  # actions held for review in an unattended run
    unattended: bool = False
    observe: bool = False
    scope_hosts: list = field(default_factory=list)  # extra scope declared when this run was launched
    scope_dirs: list = field(default_factory=list)
    approved: list = field(default_factory=list)  # actions a person approved; allowed outright if they recur in this session
    suspended: bool = False
    suspended_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        return cls(**d)

    def summary(self) -> str:
        """The compressed context for L2."""
        facets = ", ".join(f"{k}×{v}" for k, v in sorted(self.facets.items())) or "none"
        lines = [
            f"steps so far: {self.step}",
            f"behaviour tags: {facets}",
            f"hosts contacted: {', '.join(self.hosts[-10:]) or 'none'}",
            f"locations touched outside the workspace, in order: {', '.join(self.regions[-10:]) or 'none'}",
            f"actions blocked so far: {self.denies}",
            f"outward-reaching trend (0 = none; rises as the agent probes the system, network or paths outside its workspace): {self.drift:g}",
            "recent notable actions:",
        ]
        return "\n".join(lines + [f"  {n}" for n in self.notable[-10:]])


def add_task(st: SessionState, prompt: str) -> None:
    st.tasks = (st.tasks + [prompt[:2000]])[-MAX_TASKS:]


def combine(st: SessionState, ev: ActionEvent, l0: L0Result, cfg: dict) -> tuple[dict, list]:
    """This action's L0 scores plus the session history → effective scores per dimension, and the combinations that fired."""
    scores = dict(l0.scores)
    combos: list[str] = []
    gray = cfg["thresholds"]["gray"]

    def bump(dim: str, amount: int, note: str) -> None:
        scores[dim] = min(100, scores[dim] + amount)
        combos.append(note)

    # An agent can write its network logic into a file and then run it; at run time the hook only sees
    # `python x.py`. So running such a file counts as egress, for the judge or a person to decide.
    if ev.kind == "shell":
        for path, hosts in st.net_staged.items():
            if path in ev.target or os.path.basename(path) in ev.target:
                scores["boundary"] = max(scores["boundary"], 50)
                combos.append(f"runs {os.path.basename(path)}, written earlier, which contacts the unlisted host {', '.join(hosts)}")
                l0.facets.add("egress")
                l0.hosts = sorted(set(l0.hosts) | set(hosts))
                break

    fresh = [r for r in dict.fromkeys(l0.outside) if r not in st.regions]
    if fresh and len(st.regions) + len(fresh) >= WIDENING_AFTER:
        seen = ", ".join(st.regions[-4:]) or "none"
        bump("boundary", 15, f"footprint is widening: newly reached {', '.join(fresh)}; earlier: {seen}")

    leaving = l0.facets & {"egress", "internal_address", "large_query"}
    if leaving and (st.facets.get("protected_access") or "protected_access" in l0.facets):
        bump("boundary", 35, "contacts an unlisted host after touching a protected path")

    if ev.kind == "shell":
        for path in st.staged:
            if path in ev.target or os.path.basename(path) in ev.target:
                bump("resource", 75, f"runs {path}, written earlier, which refers to a protected path and an unlisted host")
                break

    if l0.top >= gray:
        if st.denies >= 2:
            bump("boundary", 15, "keeps attempting out-of-bounds actions after several denials")
        if st.facets.get("monitor_probe"):
            bump("stealth", 20, "out-of-bounds action after looking at the monitor's configuration")

    # Combination rules already use the history explicitly, so the carried score is not added on top.
    # The carried score is a fuzzy signal: at most it pushes an action to the top of the gray band, for
    # the judge or a person to decide. Denials and suspensions only come from explicit rules.
    amplify = min(CARRY_CAP, CARRY_WEIGHT * sum(st.carry.values()))
    if amplify >= 1 and not combos:
        ceiling = cfg["thresholds"]["deny"] - 1
        for dim, s in l0.scores.items():
            if SIGNAL_FLOOR <= s < ceiling:
                scores[dim] = min(ceiling, round(s + amplify))

    return scores, combos


def update(st: SessionState, ev: ActionEvent, l0: L0Result, verdict: str, cfg: dict, judged: dict | None = None) -> None:
    """Update whether or not the action was allowed: a denied attempt is itself a signal.

    `judged` is the score after fusing in the judge. An action the judge reviewed is carried at the
    judge's conclusion; otherwise one it found reasonable would still weigh on later actions.
    """
    st.step += 1
    decay = cfg["carry_decay"]
    carried = judged or l0.scores
    for dim in st.carry:
        st.carry[dim] = round(st.carry[dim] * decay + carried[dim], 2)
    for f in l0.facets:
        st.facets[f] = st.facets.get(f, 0) + 1
    st.hosts = sorted(set(st.hosts) | set(l0.hosts))[-MAX_HOSTS:]
    st.regions = (st.regions + [r for r in dict.fromkeys(l0.outside) if r not in st.regions])[-MAX_REGIONS:]
    if l0.staged and ev.target not in st.staged:
        st.staged.append(ev.target)
    if l0.net_hosts and ev.kind == "file_write":
        st.net_staged[ev.target] = l0.net_hosts
        for stale in list(st.net_staged)[:-MAX_NET_STAGED]:
            del st.net_staged[stale]
    if l0.top >= SIGNAL_FLOOR or verdict != "allow":
        tags = ",".join(sorted(l0.facets))
        st.notable = (st.notable + [f"#{st.step} [{verdict}] {ev.tool}: {ev.target[:160]} ({tags})"])[
            -MAX_NOTABLE:
        ]
    if verdict in ("deny", "suspend"):
        st.denies += 1
    if verdict == "hold":
        st.holds += 1


def action_key(ev: ActionEvent) -> str:
    return f"{ev.kind}:{ev.target}"


def overturn(st: SessionState, step: int, verdict: str, facets: list, carried: dict, cfg: dict) -> None:
    """A person ruled the verdict at `step` a false positive: remove the traces it left in the session state.

    A blocked action never ran, so neither its behaviour tags nor its carried score should remain.
    """
    faded = cfg["carry_decay"] ** max(0, st.step - step)
    for dim in st.carry:
        st.carry[dim] = round(max(0.0, st.carry[dim] - carried.get(dim, 0) * faded), 2)
    for f in facets:
        if st.facets.get(f, 0) <= 1:
            st.facets.pop(f, None)
        else:
            st.facets[f] -= 1
    if verdict in ("deny", "suspend"):
        st.denies = max(0, st.denies - 1)
    if verdict == "hold":
        st.holds = max(0, st.holds - 1)
    prefix = f"#{step} [{verdict}]"
    st.notable = [
        n.replace(prefix, f"#{step} [{verdict}, overturned by human reviewer]", 1) if n.startswith(prefix) else n
        for n in st.notable
    ]


def approve(st: SessionState, key: str) -> None:
    if key not in st.approved:
        st.approved = (st.approved + [key])[-MAX_APPROVED:]
