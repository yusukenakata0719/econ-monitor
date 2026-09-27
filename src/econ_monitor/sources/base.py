from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Union

import pandas as pd
import requests

from econ_monitor.config import Indicator

FetchResult = Dict[str, Union[pd.Series, BaseException]]


class SourceError(RuntimeError):
    """API がエラー応答を返した、または応答を解釈できなかった。"""


class Source:
    name = ""

    def __init__(self, session: requests.Session, interval_sec: float = 0.0):
        self.session = session
        self.interval_sec = interval_sec

    def unavailable_reason(self) -> Optional[str]:
        """取得不能（API キー未設定など）なら理由を返す。"""
        return None

    def fetch(self, indicators: List[Indicator], start: date) -> FetchResult:
        """指標 id -> 系列（または発生した例外）の dict を返す。例外は送出しない。"""
        raise NotImplementedError


def to_series(pairs, name: str) -> pd.Series:
    """(日付, 値) の組から、欠損を除いた日付昇順の float Series を作る。"""
    s = pd.Series({pd.Timestamp(d): v for d, v in pairs if v is not None}, dtype=float, name=name)
    s = s[~s.index.duplicated(keep="last")].sort_index().dropna()
    if s.empty:
        raise SourceError("有効な観測値がありません")
    return s
