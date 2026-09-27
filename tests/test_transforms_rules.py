from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from conftest import make_indicator, monthly
from econ_monitor.config import Rule
from econ_monitor.rules import evaluate_rule, evaluate_staleness, fetch_error_evaluation
from econ_monitor.transforms import apply_transform, infer_frequency


@pytest.mark.parametrize(
    "freq,expected",
    [("D", "D"), ("B", "D"), ("W-SAT", "W"), ("MS", "M"), ("QS", "Q"), ("YS", "A")],
)
def test_infer_frequency(freq, expected):
    idx = pd.date_range("2000-01-01", periods=30, freq=freq)
    assert infer_frequency(idx) == expected


def test_yoy_monthly():
    s = monthly([100.0] * 12 + [110.0])
    assert apply_transform(s, "yoy").iloc[-1] == pytest.approx(10.0)
    assert apply_transform(s, "yoy").iloc[:12].isna().all()


def test_yoy_daily_uses_date_offset_across_gaps():
    # 1 年前ちょうどの日付が欠けていても直前の値を使う
    idx = pd.to_datetime(["2024-09-26", "2024-09-30", "2025-09-29"])
    s = pd.Series([100.0, 200.0, 220.0], index=idx)
    assert apply_transform(s, "yoy").iloc[-1] == pytest.approx(120.0)  # 2024-09-26 の 100 と比較


def test_ann_pct_quarterly():
    s = pd.Series([100.0, 101.0], index=pd.to_datetime(["2025-01-01", "2025-04-01"]))
    s = pd.concat([pd.Series([99.0, 99.5], index=pd.to_datetime(["2024-07-01", "2024-10-01"])), s])
    assert apply_transform(s, "ann_pct").iloc[-1] == pytest.approx((1.01**4 - 1) * 100)


def test_sahm_matches_definition():
    values = [4.0] * 15 + [4.3, 4.6, 4.9]
    s = monthly(values)
    # 最新の 3 ヶ月平均 4.6 − 過去 12 ヶ月の 3 ヶ月平均の最小値 4.0 = 0.6
    assert apply_transform(s, "sahm").iloc[-1] == pytest.approx(0.6)


def test_threshold_above_and_below():
    ind = make_indicator()
    s = monthly([1.0, 2.0, -0.5])
    rule = Rule(id="band", type="threshold", above=3.0, below=0.0)
    ev = evaluate_rule(ind, rule, s)
    assert ev.triggered and ev.value == -0.5 and ev.obs_date == date(2010, 3, 1)
    assert "0以下" in ev.message

    ev = evaluate_rule(ind, rule, monthly([1.0, 2.0, 1.5]))
    assert not ev.triggered


def test_zscore_detects_spike(rng):
    values = list(100 + rng.normal(0, 1, 200)) + [110.0]
    ind = make_indicator()
    ev = evaluate_rule(ind, Rule(id="z", type="zscore", window=120, threshold=3), monthly(values))
    assert ev.triggered and ev.detail["z"] > 3 and ev.detail["window"] == 120


def test_zscore_direction_and_no_trigger(rng):
    values = list(rng.normal(0, 1, 100)) + [-8.0]
    ind = make_indicator()
    assert not evaluate_rule(ind, Rule(id="z", type="zscore", direction="up", min_periods=24), monthly(values)).triggered
    assert evaluate_rule(ind, Rule(id="z", type="zscore", direction="down", min_periods=24), monthly(values)).triggered
    calm = list(rng.normal(0, 1, 100)) + [0.1]
    assert not evaluate_rule(ind, Rule(id="z", type="zscore", min_periods=24), monthly(calm)).triggered


def test_zscore_robust_ignores_past_outlier(rng):
    base = list(rng.normal(0, 1, 100))
    base[50] = 1000.0  # 過去の外れ値で標準偏差が膨らむ
    values = base + [6.0]
    ind = make_indicator()
    assert not evaluate_rule(ind, Rule(id="z", type="zscore", min_periods=24), monthly(values)).triggered
    assert evaluate_rule(ind, Rule(id="z", type="zscore", min_periods=24, robust=True), monthly(values)).triggered


def test_zscore_insufficient_history():
    ev = evaluate_rule(make_indicator(), Rule(id="z", type="zscore", min_periods=24), monthly([1.0] * 10))
    assert not ev.triggered and "履歴不足" in ev.message


def test_zscore_constant_series_is_not_triggered():
    ev = evaluate_rule(make_indicator(), Rule(id="z", type="zscore", min_periods=24), monthly([1.0] * 50 + [2.0]))
    assert not ev.triggered and "評価不可" in ev.message


def test_staleness_measured_from_period_end():
    ind = make_indicator()
    s = monthly([1.0] * 24, start="2024-01-01")  # 最終観測 2025-12（期末 2025-12-31）
    assert not evaluate_staleness(ind, s, date(2026, 3, 1)).triggered  # 60 日
    assert evaluate_staleness(ind, s, date(2026, 4, 1)).triggered  # 91 日 > 75 日
    annual = pd.Series([1.0] * 5, index=pd.date_range("2020-01-01", periods=5, freq="YS"))  # 最終 2024
    assert not evaluate_staleness(ind, annual, date(2026, 9, 27)).triggered
    custom = make_indicator(max_age_days=30)
    assert evaluate_staleness(custom, s, date(2026, 3, 1)).triggered


def test_fetch_error_evaluation():
    ev = fetch_error_evaluation(make_indicator(), RuntimeError("boom"))
    assert ev.triggered and ev.is_data_quality and "boom" in ev.message
    assert not fetch_error_evaluation(make_indicator(), None).triggered


def test_inf_values_are_dropped():
    s = monthly([0.0, 0.0, 1.0])  # pct で 0 除算 -> inf
    ev = evaluate_rule(make_indicator(), Rule(id="p", type="threshold", transform="pct", above=1e9), s)
    assert not ev.triggered and not np.isinf(ev.value or 0)
