"""監視対象の指標と異常検知ルールの定義（config/indicators.yaml）を読み込む。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from econ_monitor.transforms import TRANSFORMS

SOURCES = ("fred", "boj", "worldbank")
RULE_TYPES = ("threshold", "zscore")
SEVERITIES = ("info", "warning", "critical")
DIRECTIONS = ("both", "up", "down")
FREQUENCIES = ("D", "W", "M", "Q", "H", "A")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    type: str
    transform: str = "level"
    periods: int = 1
    severity: str = "warning"
    description: str = ""
    # threshold: 最新値が above 以上 / below 以下で発火（両方指定時はどちらか）
    above: Optional[float] = None
    below: Optional[float] = None
    # zscore: 直近 window 期（最新値は含まない）に対する標準化スコア
    window: int = 60
    threshold: float = 3.0
    min_periods: int = 24
    robust: bool = False
    direction: str = "both"
    # 解消時に通知するか。未指定なら threshold は通知、zscore（単発のショック）は通知しない
    notify_resolved: Optional[bool] = None

    @property
    def should_notify_resolved(self) -> bool:
        if self.notify_resolved is not None:
            return self.notify_resolved
        return self.type == "threshold"


@dataclass(frozen=True)
class Indicator:
    id: str
    name: str
    country: str
    source: str
    series: str
    unit: str = ""
    db: Optional[str] = None  # 日銀: DB 名（例: FM01）
    frequency: Optional[str] = None  # 日銀: D/W/M/Q/H/A（API は同一頻度ごとにまとめて取得する）
    params: Dict[str, Any] = field(default_factory=dict)  # FRED: 追加のクエリパラメータ
    lookback_years: Optional[int] = None
    max_age_days: Optional[int] = None
    rules: Tuple[Rule, ...] = ()

    @property
    def source_url(self) -> Optional[str]:
        if self.source == "fred":
            return f"https://fred.stlouisfed.org/series/{self.series}"
        if self.source == "worldbank":
            return f"https://data.worldbank.org/indicator/{self.series}?locations={self.country}"
        if self.source == "boj":
            return "https://www.stat-search.boj.or.jp/"
        return None


@dataclass(frozen=True)
class Settings:
    lookback_years: int = 20
    timezone: str = "Asia/Tokyo"
    # 日銀 API は短時間の連続アクセスが禁止されているため、リクエスト間隔を空ける
    boj_request_interval_sec: float = 1.5
    fred_request_interval_sec: float = 0.3
    worldbank_request_interval_sec: float = 0.3


@dataclass(frozen=True)
class Config:
    settings: Settings
    countries: Dict[str, str]
    indicators: Tuple[Indicator, ...]

    def country_name(self, code: str) -> str:
        return self.countries.get(code, code)


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw)


def parse_config(raw: Dict[str, Any]) -> Config:
    settings = Settings(**(raw.get("settings") or {}))
    countries = {str(k): str(v) for k, v in (raw.get("countries") or {}).items()}
    rule_defaults: Dict[str, Dict[str, Any]] = raw.get("rule_defaults") or {}

    indicators: List[Indicator] = []
    for i, entry in enumerate(raw.get("indicators") or []):
        where = f"indicators[{i}] ({entry.get('id', '?')})"
        for expanded in _expand_countries(entry):
            indicators.append(_parse_indicator(expanded, rule_defaults, where))

    if not indicators:
        raise ConfigError("indicators が 1 件も定義されていません")
    seen: set = set()
    for ind in indicators:
        if ind.id in seen:
            raise ConfigError(f"指標 id が重複しています: {ind.id}")
        seen.add(ind.id)
    return Config(settings=settings, countries=countries, indicators=tuple(indicators))


def _expand_countries(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`countries: [US, JP]` を持つ定義を国ごとの指標に展開する（id は `<id>_<国コード小文字>`）。"""
    countries = entry.get("countries")
    if not countries:
        return [entry]
    out = []
    for code in countries:
        e = {k: v for k, v in entry.items() if k != "countries"}
        e["country"] = code
        e["id"] = f"{entry['id']}_{str(code).lower()}"
        out.append(e)
    return out


def _parse_indicator(entry: Dict[str, Any], rule_defaults: Dict[str, Dict[str, Any]], where: str) -> Indicator:
    for key in ("id", "name", "country", "source", "series"):
        if not entry.get(key):
            raise ConfigError(f"{where}: '{key}' は必須です")
    source = entry["source"]
    if source not in SOURCES:
        raise ConfigError(f"{where}: source は {SOURCES} のいずれかです（指定値: {source}）")
    if source == "boj":
        if not entry.get("db"):
            raise ConfigError(f"{where}: 日銀の指標には 'db' が必須です")
        if entry.get("frequency") not in FREQUENCIES:
            raise ConfigError(f"{where}: 日銀の指標には 'frequency'（{FREQUENCIES}）が必須です")

    rules = []
    rule_ids: set = set()
    for j, r in enumerate(entry.get("rules") or []):
        rule = _parse_rule(r, rule_defaults, f"{where}.rules[{j}]")
        if rule.id in rule_ids or rule.id in ("stale", "fetch_error"):
            raise ConfigError(f"{where}: ルール id '{rule.id}' が重複または予約語です")
        rule_ids.add(rule.id)
        rules.append(rule)

    known = {f for f in Indicator.__dataclass_fields__} - {"rules"}
    unknown = set(entry) - known - {"rules"}
    if unknown:
        raise ConfigError(f"{where}: 不明なキー {sorted(unknown)}")
    kwargs = {k: v for k, v in entry.items() if k in known}
    kwargs["series"] = str(kwargs["series"])
    kwargs["country"] = str(kwargs["country"])
    return Indicator(**kwargs, rules=tuple(rules))


def _parse_rule(r: Dict[str, Any], rule_defaults: Dict[str, Dict[str, Any]], where: str) -> Rule:
    rtype = r.get("type")
    if rtype not in RULE_TYPES:
        raise ConfigError(f"{where}: type は {RULE_TYPES} のいずれかです（指定値: {rtype}）")
    merged = {**(rule_defaults.get(rtype) or {}), **r}
    if not merged.get("id"):
        raise ConfigError(f"{where}: ルールには 'id' が必須です")
    unknown = set(merged) - set(Rule.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"{where}: 不明なキー {sorted(unknown)}")
    rule = Rule(**merged)
    if rule.transform not in TRANSFORMS:
        raise ConfigError(f"{where}: transform は {TRANSFORMS} のいずれかです（指定値: {rule.transform}）")
    if rule.severity not in SEVERITIES:
        raise ConfigError(f"{where}: severity は {SEVERITIES} のいずれかです")
    if rule.direction not in DIRECTIONS:
        raise ConfigError(f"{where}: direction は {DIRECTIONS} のいずれかです")
    if rule.periods < 1:
        raise ConfigError(f"{where}: periods は 1 以上です")
    if rule.type == "threshold" and rule.above is None and rule.below is None:
        raise ConfigError(f"{where}: threshold ルールには above か below が必要です")
    if rule.type == "zscore" and (rule.window < 3 or rule.min_periods > rule.window):
        raise ConfigError(f"{where}: zscore ルールは window>=3 かつ min_periods<=window が必要です")
    return rule
