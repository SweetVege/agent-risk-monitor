"""The parts of the monitor that may be absent: the model judge and the drift score.

The engine works without either. With no judge, gray-band actions are decided by the rule scores
alone; with no drift score, the review threshold never moves. Everything else imports the two
from here, so nothing else needs to know whether they are installed.
"""
from __future__ import annotations


class NoJudge:
    """Stands in when no judge is installed. Anything with `available` and `evaluate` can take its place."""

    available = False
    unavailable = "no judge is installed"
    failures = 0
    last_error = ""

    def __init__(self, cfg: dict | None = None):
        self.cfg = {}

    def check(self) -> None:
        pass

    def evaluate(self, tasks: list, summary: str, ev, findings: list):
        return None


class _NoDrift:
    @staticmethod
    def points(facets, verdict: str, would: str = "") -> int:
        return 0

    @staticmethod
    def advance(value: float, gained: int, cfg: dict) -> float:
        return 0.0

    @staticmethod
    def level(value: float, cfg: dict) -> str:
        return "low"

    @staticmethod
    def gray_threshold(value: float, cfg: dict) -> int:
        return cfg["thresholds"]["gray"]


try:
    from . import drift
    HAS_DRIFT = True
except ImportError:
    drift, HAS_DRIFT = _NoDrift, False

try:
    from .judge import Judge
    HAS_JUDGE = True
except ImportError:
    Judge, HAS_JUDGE = NoJudge, False
