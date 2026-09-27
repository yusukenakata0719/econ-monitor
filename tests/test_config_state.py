from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from conftest import make_indicator
from econ_monitor.config import ConfigError, load_config, parse_config
from econ_monitor.rules import Evaluation
from econ_monitor.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_config_is_valid():
    cfg = load_config(ROOT / "config" / "indicators.yaml")
    ids = [i.id for i in cfg.indicators]
    assert len(ids) == len(set(ids))
    assert {"fred", "boj", "worldbank"} <= {i.source for i in cfg.indicators}
    for ind in cfg.indicators:
        assert ind.country in cfg.countries, ind.id
        assert ind.rules, f"{ind.id} にルールがありません"


def _raw(**ind):
    base = {"id": "a", "name": "A", "country": "US", "source": "fred", "series": "X",
            "rules": [{"id": "r", "type": "threshold", "above": 1}]}
    base.update(ind)
    return {"indicators": [base]}


def test_country_expansion_and_rule_defaults():
    raw = _raw(source="worldbank", country=None, countries=["US", "JP"], rules=[{"id": "z", "type": "zscore"}])
    raw["rule_defaults"] = {"zscore": {"window": 40, "min_periods": 10}}
    cfg = parse_config(raw)
    assert [i.id for i in cfg.indicators] == ["a_us", "a_jp"]
    assert [i.country for i in cfg.indicators] == ["US", "JP"]
    assert cfg.indicators[0].rules[0].window == 40


@pytest.mark.parametrize(
    "override,msg",
    [
        ({"source": "imf"}, "source"),
        ({"source": "boj"}, "db"),
        ({"source": "boj", "db": "FM01", "frequency": "X"}, "frequency"),
        ({"rules": [{"id": "r", "type": "threshold"}]}, "above か below"),
        ({"rules": [{"id": "r", "type": "zscore", "transform": "log"}]}, "transform"),
        ({"rules": [{"id": "stale", "type": "threshold", "above": 1}]}, "予約語"),
        ({"rules": [{"id": "r", "type": "threshold", "above": 1, "typo": 1}]}, "不明なキー"),
        ({"serie": "X"}, "不明なキー"),
    ],
)
def test_config_validation_errors(override, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(_raw(**override))


def test_duplicate_ids_rejected():
    raw = _raw()
    raw["indicators"].append(dict(raw["indicators"][0]))
    with pytest.raises(ConfigError, match="重複"):
        parse_config(raw)


def _ev(rule_id="r", triggered=True, ind_id="x", notify_resolved=True):
    return Evaluation(indicator=make_indicator(id=ind_id), rule_id=rule_id, severity="warning",
                      triggered=triggered, message="m", notify_resolved=notify_resolved)


def test_state_lifecycle(tmp_path):
    path = tmp_path / "state" / "alerts.json"
    t1, t2, t3 = (datetime(2026, 1, d) for d in (1, 2, 3))

    store = StateStore.load(path)
    ch = store.reconcile([_ev(triggered=True), _ev("q", triggered=False)], t1)
    assert [e.key for e in ch.new] == ["x:r"] and not ch.resolved and ch.has_notifiable
    store.save(path)

    store = StateStore.load(path)
    ch = store.reconcile([_ev(triggered=True)], t2)
    assert not ch.new and [e.key for e in ch.ongoing] == ["x:r"] and not ch.has_notifiable
    assert store.alerts["x:r"]["since"] == t1.isoformat()

    ch = store.reconcile([_ev(triggered=False)], t3)
    assert [r.evaluation.key for r in ch.resolved] == ["x:r"] and ch.resolved[0].since == t1.isoformat()
    assert "x:r" not in store.alerts


def test_state_keeps_unevaluated_rules_and_prunes_removed_indicators():
    store = StateStore({"x:r": {"active": True, "since": "2026-01-01"}, "gone:r": {"active": True, "since": "2026-01-01"}})
    ch = store.reconcile([], datetime(2026, 1, 2))  # 取得失敗などで評価されなかった
    assert not ch.resolved and "x:r" in store.alerts
    store.prune({"x"})
    assert list(store.alerts) == ["x:r"]


def test_resolved_without_notification_is_not_notifiable():
    store = StateStore({"x:r": {"active": True, "since": "2026-01-01"}})
    ch = store.reconcile([_ev(triggered=False, notify_resolved=False)], datetime(2026, 1, 2))
    assert len(ch.resolved) == 1 and not ch.notify_resolved and not ch.has_notifiable
