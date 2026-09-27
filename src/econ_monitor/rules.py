"""異常検知ルールの評価。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Optional

import pandas as pd

from econ_monitor.config import Indicator, Rule
from econ_monitor.transforms import apply_transform, infer_frequency, transform_is_percent, transform_label

STALE_RULE_ID = "stale"
FETCH_ERROR_RULE_ID = "fetch_error"
DATA_QUALITY_RULES = (STALE_RULE_ID, FETCH_ERROR_RULE_ID)

# 最新観測期間の末日からこの日数を超えて更新がなければ「データ更新停止」とみなす（頻度別の既定値）。
# 公表ラグ（例: 4-6月期 GDP は 7月末公表、世界銀行の年次データは 1-2 年遅れ）を考慮して長めにとる。
DEFAULT_MAX_AGE_DAYS = {"D": 10, "W": 21, "M": 75, "Q": 150, "H": 300, "A": 730}

_PERIOD_LENGTH = {
    "D": pd.DateOffset(days=1),
    "W": pd.DateOffset(weeks=1),
    "M": pd.DateOffset(months=1),
    "Q": pd.DateOffset(months=3),
    "H": pd.DateOffset(months=6),
    "A": pd.DateOffset(years=1),
}


@dataclass
class Evaluation:
    indicator: Indicator
    rule_id: str
    severity: str
    triggered: bool
    message: str
    value: Optional[float] = None
    obs_date: Optional[date] = None
    frequency: Optional[str] = None
    notify_resolved: bool = True
    description: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.indicator.id}:{self.rule_id}"

    @property
    def is_data_quality(self) -> bool:
        return self.rule_id in DATA_QUALITY_RULES


def evaluate_rule(ind: Indicator, rule: Rule, series: pd.Series) -> Evaluation:
    freq = infer_frequency(series.index)
    x = apply_transform(series, rule.transform, rule.periods).dropna()
    x = x[~x.isin([math.inf, -math.inf])]
    base = dict(
        indicator=ind,
        rule_id=rule.id,
        severity=rule.severity,
        frequency=freq,
        notify_resolved=rule.should_notify_resolved,
        description=rule.description,
    )
    if x.empty:
        return Evaluation(triggered=False, message="変換後のデータがありません", **base)

    last = float(x.iloc[-1])
    obs = x.index[-1].date()
    label = transform_label(rule.transform, rule.periods)
    unit = "%" if transform_is_percent(rule.transform) else ind.unit
    shown = f"{label} {_fmt(last)}{unit}"

    if rule.type == "threshold":
        hit_above = rule.above is not None and last >= rule.above
        hit_below = rule.below is not None and last <= rule.below
        conds = []
        if rule.above is not None:
            conds.append(f"{_fmt(rule.above)}以上")
        if rule.below is not None:
            conds.append(f"{_fmt(rule.below)}以下")
        message = f"{shown}（閾値: {' / '.join(conds)}）"
        return Evaluation(
            triggered=hit_above or hit_below,
            message=message,
            value=last,
            obs_date=obs,
            detail={"above": rule.above, "below": rule.below},
            **base,
        )

    # zscore
    hist = x.iloc[:-1].iloc[-rule.window:]
    if len(hist) < rule.min_periods:
        return Evaluation(
            triggered=False,
            message=f"履歴不足（{len(hist)}/{rule.min_periods}期）",
            value=last,
            obs_date=obs,
            **base,
        )
    if rule.robust:
        center = float(hist.median())
        scale = 1.4826 * float((hist - center).abs().median())
    else:
        center = float(hist.mean())
        scale = float(hist.std(ddof=1))
    if not scale or math.isnan(scale):
        return Evaluation(triggered=False, message="ばらつきが 0 のため評価不可", value=last, obs_date=obs, **base)

    z = (last - center) / scale
    if rule.direction == "up":
        triggered = z >= rule.threshold
    elif rule.direction == "down":
        triggered = z <= -rule.threshold
    else:
        triggered = abs(z) >= rule.threshold
    message = f"{shown}、過去{len(hist)}期に対し z={z:+.2f}（閾値 ±{_fmt(rule.threshold)}）"
    return Evaluation(
        triggered=triggered,
        message=message,
        value=last,
        obs_date=obs,
        detail={"z": round(z, 3), "mean": center, "std": scale, "window": len(hist)},
        **base,
    )


def evaluate_staleness(ind: Indicator, series: pd.Series, today: date) -> Evaluation:
    freq = infer_frequency(series.index)
    last_date = series.index[-1].date()
    # 観測日は期初（例: 月次は 1 日）なので、期末から数える
    period_end = (series.index[-1] + _PERIOD_LENGTH[freq] - pd.DateOffset(days=1)).date()
    max_age = ind.max_age_days or DEFAULT_MAX_AGE_DAYS[freq]
    age = (today - period_end).days
    return Evaluation(
        indicator=ind,
        rule_id=STALE_RULE_ID,
        severity="info",
        triggered=age > max_age,
        message=f"最新観測日 {last_date.isoformat()}（期末から {age}日経過、許容 {max_age}日）",
        value=None,
        obs_date=last_date,
        frequency=freq,
        description="データ更新停止の疑い",
    )


def fetch_error_evaluation(ind: Indicator, error: Optional[BaseException]) -> Evaluation:
    message = "取得成功" if error is None else f"{type(error).__name__}: {error}"[:300]
    return Evaluation(
        indicator=ind,
        rule_id=FETCH_ERROR_RULE_ID,
        severity="warning",
        triggered=error is not None,
        message=message,
        description="データ取得エラー",
    )


def _fmt(v: float) -> str:
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:.1f}"
    return f"{v:.2f}"
