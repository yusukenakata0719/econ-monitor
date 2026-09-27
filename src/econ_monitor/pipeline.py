"""取得 → 異常検知 → 状態差分 → レポート・通知 の一連の処理。"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from econ_monitor.config import Config, Indicator
from econ_monitor.models import RunResult, Snapshot
from econ_monitor.rules import Evaluation, evaluate_rule, evaluate_staleness, fetch_error_evaluation
from econ_monitor.sources import FetchResult, Source
from econ_monitor.state import StateStore
from econ_monitor.transforms import infer_frequency

log = logging.getLogger(__name__)


def fetch_all(config: Config, sources: Dict[str, Source], today: date):
    """ソースごとにまとめて取得する。戻り値は (指標id -> 系列 or 例外, スキップしたソースと理由)。"""
    by_source: Dict[str, List[Indicator]] = defaultdict(list)
    for ind in config.indicators:
        by_source[ind.source].append(ind)

    results: FetchResult = {}
    skipped: Dict[str, str] = {}
    for name, indicators in by_source.items():
        source = sources[name]
        reason = source.unavailable_reason()
        if reason:
            log.warning("%s をスキップ: %s", name, reason)
            skipped[name] = reason
            continue
        # 指標ごとに遡及年数が異なる場合があるため、ソース内の最大値でまとめて取得する
        years = max(ind.lookback_years or config.settings.lookback_years for ind in indicators)
        start = date(today.year - years, 1, 1)
        log.info("%s から %d 系列を取得（%s 以降）", name, len(indicators), start)
        results.update(source.fetch(indicators, start))
    return results, skipped


def evaluate_all(config: Config, results: FetchResult, today: date):
    evaluations: List[Evaluation] = []
    snapshots: List[Snapshot] = []
    for ind in config.indicators:
        if ind.id not in results:
            snapshots.append(Snapshot(indicator=ind, status="skipped"))
            continue
        data = results[ind.id]
        if isinstance(data, BaseException):
            evaluations.append(fetch_error_evaluation(ind, data))
            snapshots.append(Snapshot(indicator=ind, status="error", error=str(data)))
            continue

        series: pd.Series = data
        if ind.lookback_years:
            series = series[series.index >= pd.Timestamp(today.year - ind.lookback_years, 1, 1)]
        evaluations.append(fetch_error_evaluation(ind, None))
        evaluations.append(evaluate_staleness(ind, series, today))
        for rule in ind.rules:
            try:
                evaluations.append(evaluate_rule(ind, rule, series))
            except Exception:  # noqa: BLE001 - 1 ルールの不具合で全体を止めない（状態は前回のまま維持）
                log.exception("%s/%s の評価に失敗", ind.id, rule.id)
        snapshots.append(
            Snapshot(
                indicator=ind,
                status="ok",
                value=float(series.iloc[-1]),
                obs_date=series.index[-1].date(),
                previous=float(series.iloc[-2]) if len(series) > 1 else None,
                frequency=infer_frequency(series.index),
            )
        )
    return evaluations, snapshots


def run(
    config: Config,
    sources: Dict[str, Source],
    state_path: str | Path,
    mode: str,
    now: datetime,
    run_url: Optional[str] = None,
    data_dir: str | Path | None = None,
) -> tuple:
    """1 回分の処理を実行し、(RunResult, 更新後の StateStore, 取得結果) を返す。

    状態の保存や通知の送信は呼び出し側で行う（送信失敗時に状態を保存しないため）。
    """
    today = now.date()
    results, skipped = fetch_all(config, sources, today)
    if data_dir:
        _dump_data(results, Path(data_dir))

    evaluations, snapshots = evaluate_all(config, results, today)
    store = StateStore.load(state_path)
    store.prune({ind.id for ind in config.indicators})
    changes = store.reconcile(evaluations, now)

    result = RunResult(
        config=config,
        generated_at=now,
        mode=mode,
        snapshots=snapshots,
        evaluations=evaluations,
        changes=changes,
        skipped_sources=skipped,
        run_url=run_url,
    )
    return result, store, results


def _dump_data(results: FetchResult, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for ind_id, data in results.items():
        if isinstance(data, pd.Series):
            data.rename("value").to_csv(data_dir / f"{ind_id}.csv", index_label="date")
