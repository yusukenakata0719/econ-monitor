"""アラート状態の永続化と差分判定。

同じ異常を毎回通知しないよう、ルールごとに「発火中かどうか」を JSON に保存し、
前回からの変化（新規発生 / 解消）だけを通知対象にする。
GitHub Actions では actions/cache でこのファイルを実行間で引き継ぐ。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from econ_monitor.rules import Evaluation

STATE_VERSION = 1


@dataclass
class Resolved:
    evaluation: Evaluation
    since: str  # 発生日時（ISO 形式）


@dataclass
class Changes:
    new: List[Evaluation] = field(default_factory=list)
    resolved: List[Resolved] = field(default_factory=list)
    ongoing: List[Evaluation] = field(default_factory=list)

    @property
    def active(self) -> List[Evaluation]:
        return self.new + self.ongoing

    @property
    def notify_resolved(self) -> List[Resolved]:
        return [r for r in self.resolved if r.evaluation.notify_resolved]

    @property
    def has_notifiable(self) -> bool:
        return bool(self.new or self.notify_resolved)


class StateStore:
    def __init__(self, alerts: Dict[str, Dict[str, Any]] | None = None):
        self.alerts: Dict[str, Dict[str, Any]] = alerts or {}

    @classmethod
    def load(cls, path: str | Path) -> "StateStore":
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        return cls(raw.get("alerts", {}))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": STATE_VERSION, "alerts": self.alerts}, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, p)

    def reconcile(self, evaluations: List[Evaluation], now: datetime) -> Changes:
        """今回の評価結果を状態に反映し、前回からの変化を返す。

        今回評価されなかったルール（取得失敗した指標のルールなど）は状態を維持する。
        """
        changes = Changes()
        for ev in evaluations:
            prev = self.alerts.get(ev.key)
            was_active = bool(prev and prev.get("active"))
            if ev.triggered:
                since = prev["since"] if was_active else now.isoformat(timespec="seconds")
                self.alerts[ev.key] = {
                    "active": True,
                    "since": since,
                    "severity": ev.severity,
                    "message": ev.message,
                    "obs_date": ev.obs_date.isoformat() if ev.obs_date else None,
                    "value": ev.value,
                    "updated": now.isoformat(timespec="seconds"),
                }
                (changes.ongoing if was_active else changes.new).append(ev)
            elif was_active:
                changes.resolved.append(Resolved(evaluation=ev, since=prev["since"]))
                self.alerts.pop(ev.key, None)
        return changes

    def prune(self, valid_indicator_ids: set) -> None:
        """設定から削除された指標の状態を捨てる。"""
        for key in list(self.alerts):
            if key.split(":", 1)[0] not in valid_indicator_ids:
                del self.alerts[key]
