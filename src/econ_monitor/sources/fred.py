"""FRED（セントルイス連銀）API。https://fred.stlouisfed.org/docs/api/fred/"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import List, Optional

from econ_monitor.config import Indicator
from econ_monitor.http import DEFAULT_TIMEOUT
from econ_monitor.sources.base import FetchResult, Source, SourceError, to_series

log = logging.getLogger(__name__)

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredSource(Source):
    name = "fred"

    def __init__(self, session, interval_sec: float = 0.3, api_key: Optional[str] = None):
        super().__init__(session, interval_sec)
        self.api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY", "")

    def unavailable_reason(self) -> Optional[str]:
        if not self.api_key:
            return "環境変数 FRED_API_KEY が未設定"
        return None

    def fetch(self, indicators: List[Indicator], start: date) -> FetchResult:
        results: FetchResult = {}
        for i, ind in enumerate(indicators):
            if i:
                time.sleep(self.interval_sec)
            try:
                results[ind.id] = self._fetch_one(ind, start)
            except Exception as e:  # noqa: BLE001 - 1 系列の失敗で全体を止めない
                log.warning("FRED %s の取得に失敗: %s", ind.series, e)
                results[ind.id] = e
        return results

    def _fetch_one(self, ind: Indicator, start: date):
        params = {
            "series_id": ind.series,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start.isoformat(),
            **ind.params,
        }
        resp = self.session.get(OBSERVATIONS_URL, params=params, timeout=DEFAULT_TIMEOUT)
        try:
            body = resp.json()
        except ValueError:
            raise SourceError(f"HTTP {resp.status_code}: JSON 以外の応答") from None
        if resp.status_code != 200 or "observations" not in body:
            # エラーメッセージに API キーが含まれることはないが、URL は出さない
            raise SourceError(f"HTTP {resp.status_code}: {body.get('error_message', body)}")
        pairs = ((o["date"], None if o["value"] in (".", "") else float(o["value"])) for o in body["observations"])
        return to_series(pairs, ind.id)
