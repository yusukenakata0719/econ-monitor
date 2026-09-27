from __future__ import annotations

from typing import Dict

import requests

from econ_monitor.config import Settings
from econ_monitor.sources.base import FetchResult, Source, SourceError
from econ_monitor.sources.boj import BojSource
from econ_monitor.sources.fred import FredSource
from econ_monitor.sources.worldbank import WorldBankSource

__all__ = ["FetchResult", "Source", "SourceError", "build_sources"]


def build_sources(session: requests.Session, settings: Settings) -> Dict[str, Source]:
    return {
        "fred": FredSource(session, settings.fred_request_interval_sec),
        "boj": BojSource(session, settings.boj_request_interval_sec),
        "worldbank": WorldBankSource(session, settings.worldbank_request_interval_sec),
    }
