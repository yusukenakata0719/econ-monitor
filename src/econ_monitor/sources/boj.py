"""日本銀行 時系列統計データ検索サイト API。

仕様: https://www.stat-search.boj.or.jp/info/api_manual.pdf
- 1 リクエストで指定できる系列は同一 DB・同一頻度のみ、最大 250 系列・6 万データ点
- 取り切れない場合は NEXTPOSITION を STARTPOSITION に指定して続きを取得
- 短時間の連続アクセスは禁止されているため、リクエスト間隔を空ける
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date
from typing import Dict, List, Tuple

import pandas as pd

from econ_monitor.config import Indicator
from econ_monitor.http import DEFAULT_TIMEOUT
from econ_monitor.sources.base import FetchResult, Source, SourceError, to_series

log = logging.getLogger(__name__)

DATA_CODE_URL = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
MAX_CODES_PER_REQUEST = 250


def start_date_param(start: date, frequency: str) -> str:
    """頻度に応じた startDate の書式（日次・週次・月次は YYYYMM、四半期は YYYYQQ、半期は YYYYHH、年次は YYYY）。"""
    if frequency in ("D", "W", "M"):
        return f"{start.year}{start.month:02d}"
    if frequency in ("Q", "H"):
        return f"{start.year}01"
    return str(start.year)


def parse_boj_date(value, frequency: str) -> pd.Timestamp:
    """SURVEY_DATES の値を期初の日付に変換する（例: 四半期 202402 -> 2024-04-01）。"""
    s = str(value)
    f = frequency.upper()
    if len(s) == 8:
        return pd.Timestamp(year=int(s[:4]), month=int(s[4:6]), day=int(s[6:]))
    year = int(s[:4])
    if len(s) == 4:
        return pd.Timestamp(year=year, month=1, day=1)
    sub = int(s[4:6])
    if "QUARTER" in f:
        return pd.Timestamp(year=year, month=3 * (sub - 1) + 1, day=1)
    if "SEMI" in f or "HALF" in f:
        return pd.Timestamp(year=year, month=6 * (sub - 1) + 1, day=1)
    return pd.Timestamp(year=year, month=sub, day=1)


class BojSource(Source):
    name = "boj"

    def fetch(self, indicators: List[Indicator], start: date) -> FetchResult:
        groups: Dict[Tuple[str, str], List[Indicator]] = defaultdict(list)
        for ind in indicators:
            groups[(ind.db, ind.frequency)].append(ind)

        results: FetchResult = {}
        first = True
        for (db, freq), members in groups.items():
            for i in range(0, len(members), MAX_CODES_PER_REQUEST):
                chunk = members[i : i + MAX_CODES_PER_REQUEST]
                if not first:
                    time.sleep(self.interval_sec)
                first = False
                try:
                    results.update(self._fetch_group(db, freq, chunk, start))
                except SourceError as e:
                    if len(chunk) == 1:
                        results[chunk[0].id] = e
                        continue
                    # 存在しない系列コードが 1 つでもあるとまとめて失敗するため、1 系列ずつ取り直して切り分ける
                    log.warning("日銀 %s/%s の一括取得に失敗（%s）。個別に再取得します", db, freq, e)
                    for ind in chunk:
                        time.sleep(self.interval_sec)
                        try:
                            results.update(self._fetch_group(db, freq, [ind], start))
                        except Exception as e2:  # noqa: BLE001
                            results[ind.id] = e2
                except Exception as e:  # noqa: BLE001 - ネットワーク障害などはグループ全体を失敗扱い
                    log.warning("日銀 %s/%s の取得に失敗: %s", db, freq, e)
                    for ind in chunk:
                        results[ind.id] = e
        return results

    def _fetch_group(self, db: str, freq: str, members: List[Indicator], start: date) -> FetchResult:
        params = {
            "format": "json",
            "lang": "en",
            "db": db,
            "code": ",".join(ind.series for ind in members),
            "startDate": start_date_param(start, freq),
        }
        dates: Dict[str, list] = defaultdict(list)
        values: Dict[str, list] = defaultdict(list)
        freq_names: Dict[str, str] = {}
        position = None
        while True:
            if position is not None:
                time.sleep(self.interval_sec)
                params["startPosition"] = position
            body = self._get(params)
            for rs in body.get("RESULTSET") or []:
                code = rs["SERIES_CODE"]
                freq_names[code] = rs.get("FREQUENCY") or ""
                dates[code].extend(rs["VALUES"]["SURVEY_DATES"])
                values[code].extend(rs["VALUES"]["VALUES"])
            position = body.get("NEXTPOSITION")
            if not position:
                break

        results: FetchResult = {}
        for ind in members:
            if ind.series not in dates:
                results[ind.id] = SourceError(f"応答に系列 {ind.series} が含まれていません")
                continue
            fname = freq_names[ind.series]
            try:
                pairs = ((parse_boj_date(d, fname), v) for d, v in zip(dates[ind.series], values[ind.series]))
                results[ind.id] = to_series(pairs, ind.id)
            except Exception as e:  # noqa: BLE001
                results[ind.id] = e
        return results

    def _get(self, params) -> dict:
        resp = self.session.get(DATA_CODE_URL, params=params, timeout=DEFAULT_TIMEOUT)
        try:
            body = resp.json()
        except ValueError:
            raise SourceError(f"HTTP {resp.status_code}: JSON 以外の応答") from None
        status = body.get("STATUS", resp.status_code)
        if resp.status_code != 200 or status != 200:
            raise SourceError(f"STATUS {status} {body.get('MESSAGEID', '')}: {body.get('MESSAGE', '')}")
        return body
