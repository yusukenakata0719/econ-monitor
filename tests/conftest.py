from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from econ_monitor.config import Indicator, Rule


def monthly(values, start="2010-01-01") -> pd.Series:
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"), dtype=float)


def make_indicator(rules=(), **kw) -> Indicator:
    base = dict(id="x", name="テスト指標", country="US", source="fred", series="TEST", unit="%")
    base.update(kw)
    return Indicator(rules=tuple(rules), **base)


@pytest.fixture
def rng():
    return np.random.default_rng(0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """API クライアントのリクエスト間スリープをテストでは無効化する。"""
    import time

    monkeypatch.setattr(time, "sleep", lambda *_: None)


__all__ = ["monthly", "make_indicator", "Rule"]
