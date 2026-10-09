"""The risk engine: L0 → L1 → (L2) → verdict."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import rules, scope, state, transcript
from .events import ActionEvent
from .optional import Judge, drift
from .store import Store

L1_WEIGHT, L2_WEIGHT = 0.4, 0.6


@dataclass
class Decision:
    verdict: str  # allow | ask | hold | deny | suspend
    score: int
    scores: dict
    tier: str  # which tier reached the verdict
    event_id: int = 0
    findings: list = field(default_factory=list)
    combos: list = field(default_factory=list)
    rationale: str = ""
    latency_ms: float = 0.0
    judge_tokens: tuple = (0, 0)
    judge_failed: bool = False  # the judge should have been asked but could not be (timeout, rate limit, ...)
    would: str = ""  # in observe mode, the verdict this action would have received

    def reason(self) -> str:
        """The explanation for a person. Never send it back to the monitored agent."""
        parts = self.combos + self.findings
        if self.rationale:
            parts.append(f"judge: {self.rationale}")
        return "; ".join(parts) or "nothing unusual"


class Engine:
    def __init__(self, cfg: dict, store: Store | None = None, judge=None):
        self.cfg = cfg
        self.store = store or Store()
        self.judge = judge if judge is not None else Judge(cfg)
        self._locks: dict = defaultdict(threading.Lock)

    def _verdict(self, score: int) -> str:
        t = self.cfg["thresholds"]
        if score >= t["suspend"]:
            return "suspend"
        if score >= t["deny"]:
            return "deny"
        if score >= t["ask"]:
            return "ask"
        return "allow"

    def record_task(self, session_id: str, prompt: str) -> state.SessionState:
        with self._locks[session_id]:
            st = self.store.load_state(session_id)
            if not st.suspended:  # prompts sent while suspended are blocked by the hook and are not tasks
                state.add_task(st, prompt)
                self.store.save_state(st, time.time())
            return st

    def resume(self, session_id: str) -> None:
        with self._locks[session_id]:
            st = self.store.load_state(session_id)
            st.suspended, st.suspended_reason, st.denies, st.holds = False, "", 0, 0
            self.store.save_state(st, time.time())

    def record_outcome(self, session_id: str, tool_use_id: str, ok: bool, output: str, transcript_path: str = "") -> bool:
        """Record how an action turned out once it has run. Recorded only; it does not affect scoring.

        Successful output is stored as a length: it may be the contents of a file just read and does
        not belong in the audit log. Failed output keeps a head and tail excerpt, which is what telling
        whether an agent keeps hitting a wall requires.
        """
        row = self.store.find_event(session_id, tool_use_id) if tool_use_id else None
        if row is None:
            return False
        outcome = {"status": "ok" if ok else "failed", "size": len(output)}
        if not ok:
            outcome["excerpt"] = output if len(output) <= 600 else output[:300] + "\n…\n" + output[-300:]
        detail = {**row["detail"], "outcome": outcome}
        if not detail.get("said"):
            # the transcript was not flushed when the action was scored; by now it usually is
            detail["said"] = transcript.said_before(transcript_path, tool_use_id)
            detail["said_at"] = "post" if detail["said"] else ""
        with self._locks[session_id]:
            self.store.set_event_detail(row["id"], detail)
            st = self.store.load_state(session_id)
            st.outcomes = (st.outcomes + [outcome["status"]])[-state.MAX_OUTCOMES :]
            self.store.save_state(st, time.time())
        return True

    def mark_false_positive(self, event_id: int) -> None:
        """A person marks an ask, deny or suspension as a false positive: undo its traces in the session and approve the same action for a retry in this session."""
        row = self.store.get_event(event_id)
        if row is None:
            raise KeyError(event_id)
        detail = row["detail"]
        if row["verdict"] == "allow":
            raise ValueError("an allowed action does not need marking")
        if detail.get("review"):
            raise ValueError("this event is already marked")
        if detail.get("carried") is None:
            raise ValueError("this action was only blocked because the session was suspended; mark the event that caused the suspension, or resume the session")
        if "monitor_tamper" in detail.get("facets", []):
            raise ValueError("actions that touch the monitor itself cannot be approved; change its configuration yourself")
        with self._locks[row["session_id"]]:
            st = self.store.load_state(row["session_id"])
            state.overturn(st, row["step"], row["verdict"], detail["facets"], detail["carried"], self.cfg)
            state.approve(st, f"{row['kind']}:{row['target']}")
            if row["verdict"] == "suspend":
                st.suspended, st.suspended_reason = False, ""
            self.store.save_state(st, time.time())
            self.store.set_event_detail(event_id, {**detail, "review": "false_positive"})

    def _session_cfg(self, st: state.SessionState) -> dict:
        """The policy plus the extra hosts and directories declared when this run was launched."""
        if not (st.scope_hosts or st.scope_dirs):
            return self.cfg
        extra = {**self.cfg["scope"], "extra_dirs": self.cfg["scope"]["extra_dirs"] + st.scope_dirs}
        return {**self.cfg, "allow_hosts": self.cfg["allow_hosts"] + st.scope_hosts, "scope": extra}

    def evaluate(self, ev: ActionEvent) -> Decision:
        t0 = time.perf_counter()
        thresholds = self.cfg["thresholds"]
        with self._locks[ev.session_id]:
            st = self.store.load_state(ev.session_id)
            st.unattended = st.unattended or ev.unattended
            # scope declared at launch is taken from the first action only; later events cannot widen it
            if st.step == 0 and not (st.scope_hosts or st.scope_dirs):
                st.scope_hosts, st.scope_dirs = ev.scope_hosts, ev.scope_dirs
            if st.step == 0:
                st.observe = ev.observe  # likewise only taken at the start of the run
            cfg = self._session_cfg(st)
            l0 = rules.assess(ev, cfg)
            said = transcript.said_before(ev.transcript_path, ev.tool_use_id)

            carried = None  # what this step adds to the carried score; actions while suspended do not update state
            if st.suspended:
                d = Decision("suspend", 100, dict(l0.scores), "L0", findings=["session is suspended, waiting for a person to resume it"])
            elif state.action_key(ev) in st.approved and "monitor_tamper" not in l0.facets:
                # behaviour tags are recorded as usual (the action really runs this time) but nothing is scored
                carried = dict.fromkeys(st.carry, 0)
                d = Decision("allow", 0, {**l0.scores, "deviation": 0}, "HUMAN", findings=["a person approved this action"])
                state.update(st, ev, l0, "allow", self.cfg, carried)
            else:
                scores, combos = state.combine(st, ev, l0, cfg)
                scores["deviation"] = 0
                d = Decision("allow", max(scores.values()), scores, "L1" if l0.top else "L0", 0, l0.findings, combos)
                forced, why = scope.decide(l0.facets, st.unattended, cfg)

                # Scores above the deny line skip the judge: a model must not turn a clear violation into an allow.
                # Neither is it asked about what the scope policy rules on: that is a boundary a person set
                gray = drift.gray_threshold(st.drift, cfg)  # a session with high drift is reviewed at lower scores
                if gray < thresholds["gray"] and gray <= d.score < thresholds["gray"]:
                    d.combos.append(f"sent for review because the session's footprint is widening (drift {st.drift:g})")
                if forced == "score" and gray <= d.score < thresholds["deny"] and self.judge.available:
                    jr = self.judge.evaluate(st.tasks, st.summary(), ev, combos + l0.findings)
                    if jr:
                        for dim in ("boundary", "stealth", "resource"):
                            scores[dim] = round(L1_WEIGHT * scores[dim] + L2_WEIGHT * jr.scores[dim])
                        # only the judge can tell an action is off-task; on its own that escalates to a confirmation at most
                        scores["deviation"] = round(L2_WEIGHT * jr.scores["deviation"])
                        d.score, d.tier, d.rationale = max(scores.values()), "L2", jr.rationale
                        d.judge_tokens = (jr.input_tokens, jr.output_tokens)
                    else:
                        d.judge_failed = True

                d.verdict = self._verdict(d.score)
                if d.judge_failed and d.verdict == "allow":
                    # an action that should have been reviewed and was not must not count as reviewed
                    d.verdict = "ask"
                    d.findings.append("the judge gave no answer, so a person has to confirm")
                if forced != "score":
                    d.tier, d.findings = "SCOPE", why + d.findings
                    if forced == "deny" and d.verdict in ("allow", "ask"):
                        d.verdict = "deny"
                    elif forced == "confirm" and d.verdict == "allow":
                        d.verdict = "ask"
                if st.unattended and d.verdict == "ask":
                    # nobody can answer a prompt. Hold the action for review and let the agent do something else
                    d.verdict = "hold"
                if st.observe and d.verdict == "hold":
                    # for research: see how far it goes when nothing stops it. Only holds are relaxed; denials and suspensions still apply
                    d.would, d.verdict = "hold", "allow"
                    d.findings.append("observe mode: would have been held, let through and recorded")
                carried = {dim: (scores if d.tier == "L2" else l0.scores)[dim] for dim in st.carry}
                state.update(st, ev, l0, d.verdict, cfg, carried)
                st.drift = drift.advance(st.drift, drift.points(l0.facets, d.verdict, d.would), cfg)
                st.drift_peak = max(st.drift_peak, st.drift)
                too_many_holds = st.holds >= self.cfg["unattended"]["max_holds"]
                if d.verdict == "suspend" or st.denies >= self.cfg["suspend_after_denies"] or too_many_holds:
                    if too_many_holds and d.verdict == "hold":
                        d.combos.append(f"{st.holds} actions are waiting for review; not continuing with nobody watching")
                    elif d.verdict != "suspend":
                        d.combos.append(f"{st.denies} actions have been denied in this session")
                    d.verdict = "suspend"
                    st.suspended, st.suspended_reason = True, d.reason()

            d.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            d.event_id = self.store.log_event(
                {
                    "session_id": ev.session_id,
                    "step": st.step,
                    "ts": ev.ts,
                    "tool": ev.tool,
                    "kind": ev.kind,
                    "target": ev.target[:4000],
                    "verdict": d.verdict,
                    "score": d.score,
                    "tier": d.tier,
                    "latency_ms": d.latency_ms,
                    "detail": {
                        "scores": d.scores,
                        "findings": d.findings,
                        "combos": d.combos,
                        "rationale": d.rationale,
                        "judge_tokens": d.judge_tokens,
                        "judge_failed": d.judge_failed,
                        "would": d.would,
                        "drift": st.drift,
                        "facets": sorted(l0.facets),
                        "carried": carried,
                        "cwd": ev.cwd,
                        "tool_use_id": ev.tool_use_id,
                        "described": ev.described,
                        "said": said,
                        "said_at": "pre" if said else "",
                    },
                }
            )
            self.store.save_state(st, ev.ts)
            return d
