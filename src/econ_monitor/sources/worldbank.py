"""世界銀行 Indicators API v2。https://datahelpdesk.worldbank.org/knowledgebase/articles/889392

年次データ。同じ指標コードの国はまとめて 1 リクエストで取得する（国コードは ISO2/ISO3 どちらも可）。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date
from typing import Dict, List

from econ_monitor.config import Indicator
from econ_monitor.http import DEFAULT_TIMEOUT
from econ_monitor.sources.base import FetchResult, Source, SourceError, to_series

log = logging.getLogger(__name__)

BASE_URL = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"


class WorldBankSource(Source):
    name = "worldbank"

    def fetch(self, indicators: List[Indicator], start: date) -> FetchResult:
        groups: Dict[str, List[Indicator]] = defaultdict(list)
        for ind in indicators:
            groups[ind.series].append(ind)

        results: FetchResult = {}
        for i, (code, members) in enumerate(groups.items()):
            if i:
                time.sleep(self.interval_sec)
            try:
                rows = self._fetch_rows(code, [m.country for m in members], start)
            except Exception as e:  # noqa: BLE001
                log.warning("世界銀行 %s の取得に失敗: %s", code, e)
                for ind in members:
                    results[ind.id] = e
                continue

            by_country: Dict[str, list] = defaultdict(list)
            for row in rows:
                pair = (f"{row['date']}-01-01", row.get("value"))
                # 指定した国コードが ISO2 でも ISO3 でも対応付けられるよう両方で登録
                by_country[str(row.get("country", {}).get("id", "")).upper()].append(pair)
                by_country[str(row.get("countryiso3code", "")).upper()].append(pair)
            for ind in members:
                try:
                    results[ind.id] = to_series(by_country.get(ind.country.upper(), []), ind.id)
                except Exception as e:  # noqa: BLE001
                    results[ind.id] = e
        return results

    def _fetch_rows(self, code: str, countries: List[str], start: date) -> list:
        url = BASE_URL.format(countries=";".join(countries), indicator=code)
        params = {"format": "json", "per_page": 1000, "date": f"{start.year}:{date.today().year}", "page": 1}
        rows: list = []
        while True:
            resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            try:
                body = resp.json()
            except ValueError:
                raise SourceError(f"HTTP {resp.status_code}: JSON 以外の応答") from None
            if resp.status_code != 200 or not isinstance(body, list) or not body:
                raise SourceError(f"HTTP {resp.status_code}: {body}")
            meta = body[0]
            if "message" in meta:  # 例: [{"message": [{"id": "120", "key": "Invalid value", ...}]}]
                raise SourceError(str(meta["message"]))
            rows.extend((body[1] if len(body) > 1 else None) or [])  # データなしの場合 body[1] は null
            if int(meta.get("page", 1)) >= int(meta.get("pages", 1)):
                return rows
            params["page"] += 1
            time.sleep(self.interval_sec)
