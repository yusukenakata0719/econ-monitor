"""時系列の変換（前年比・差分・サーム指標など）。

すべての関数は日付昇順・重複なしの DatetimeIndex を持つ float の Series を受け取る。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRANSFORMS = ("level", "diff", "pct", "yoy", "yoy_diff", "ann_pct", "sahm")

PERIODS_PER_YEAR = {"D": 260, "W": 52, "M": 12, "Q": 4, "H": 2, "A": 1}

# 変換後の値の表示用ラベルと、単位が % になるかどうか
_LABELS = {
    "level": ("水準", False),
    "diff": ("{p}期差", False),
    "pct": ("{p}期変化率", True),
    "yoy": ("前年比", True),
    "yoy_diff": ("前年差", False),
    "ann_pct": ("前期比年率", True),
    "sahm": ("サーム指標", False),
}


def infer_frequency(index: pd.DatetimeIndex) -> str:
    """観測日の間隔の中央値から頻度（D/W/M/Q/H/A）を推定する。"""
    if len(index) < 3:
        return "M"
    median_days = float(np.median(np.diff(index.values).astype("timedelta64[D]").astype(float)))
    if median_days <= 4:
        return "D"
    if median_days <= 10:
        return "W"
    if median_days <= 45:
        return "M"
    if median_days <= 120:
        return "Q"
    if median_days <= 250:
        return "H"
    return "A"


def apply_transform(s: pd.Series, name: str, periods: int = 1) -> pd.Series:
    s = s.dropna().astype(float)
    if name == "level":
        return s
    if name == "diff":
        return s.diff(periods)
    if name == "pct":
        return (s / s.shift(periods) - 1.0) * 100.0
    if name == "yoy":
        return (s / _year_ago(s) - 1.0) * 100.0
    if name == "yoy_diff":
        return s - _year_ago(s)
    if name == "ann_pct":
        ppy = PERIODS_PER_YEAR[infer_frequency(s.index)]
        return ((s / s.shift(periods)) ** (ppy / periods) - 1.0) * 100.0
    if name == "sahm":
        # サームルール: 3期移動平均が、過去12期の3期移動平均の最小値から何ポイント上昇したか
        m3 = s.rolling(3).mean()
        return m3 - m3.shift(1).rolling(12).min()
    raise ValueError(f"未知の transform: {name}")


def transform_label(name: str, periods: int = 1) -> str:
    return _LABELS[name][0].format(p=periods)


def transform_is_percent(name: str) -> bool:
    return _LABELS[name][1]


def _year_ago(s: pd.Series) -> pd.Series:
    """各観測日の 1 年前時点の値（ちょうどの日付がなければ直前の値）を返す。

    日次データの休日や頻度の違いを吸収するため、期数ではなく日付で遡る。
    """
    target = s.index - pd.DateOffset(years=1)
    prev = s.asof(target)
    prev.index = s.index
    return prev
