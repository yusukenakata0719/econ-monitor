from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from econ_monitor.config import Config, Indicator
from econ_monitor.rules import Evaluation
from econ_monitor.state import Changes


@dataclass
class Snapshot:
    """指標ごとの最新値（レポートの一覧表用）。"""

    indicator: Indicator
    status: str  # ok / error / skipped
    value: Optional[float] = None
    obs_date: Optional[date] = None
    previous: Optional[float] = None
    frequency: Optional[str] = None
    error: str = ""

    @property
    def change(self) -> Optional[float]:
        if self.value is None or self.previous is None:
            return None
        return self.value - self.previous


@dataclass
class RunResult:
    config: Config
    generated_at: datetime
    mode: str
    snapshots: List[Snapshot]
    evaluations: List[Evaluation]
    changes: Changes
    skipped_sources: Dict[str, str] = field(default_factory=dict)
    run_url: Optional[str] = None


def format_obs_date(d: Optional[date], frequency: Optional[str]) -> str:
    if d is None:
        return "-"
    if frequency in ("M",):
        return f"{d.year}-{d.month:02d}"
    if frequency == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if frequency == "H":
        return f"{d.year}H{(d.month - 1) // 6 + 1}"
    if frequency == "A":
        return str(d.year)
    return d.isoformat()
