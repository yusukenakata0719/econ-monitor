from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
import requests
import responses
from responses import matchers

from conftest import make_indicator
from econ_monitor.sources.boj import DATA_CODE_URL, BojSource, parse_boj_date, start_date_param
from econ_monitor.sources.fred import OBSERVATIONS_URL, FredSource
from econ_monitor.sources.worldbank import WorldBankSource

START = date(2020, 1, 1)


# ───────────── FRED ─────────────
@responses.activate
def test_fred_parses_observations_and_skips_missing():
    responses.get(
        OBSERVATIONS_URL,
        match=[matchers.query_param_matcher(
            {"series_id": "UNRATE", "api_key": "k", "file_type": "json", "observation_start": "2020-01-01"})],
        json={"observations": [
            {"date": "2020-01-01", "value": "3.5"},
            {"date": "2020-02-01", "value": "."},
            {"date": "2020-03-01", "value": "4.4"},
        ]},
    )
    src = FredSource(requests.Session(), api_key="k")
    out = src.fetch([make_indicator(id="u", series="UNRATE")], START)
    assert list(out["u"].values) == [3.5, 4.4]
    assert out["u"].index[-1] == pd.Timestamp("2020-03-01")


@responses.activate
def test_fred_error_is_returned_per_series():
    responses.get(OBSERVATIONS_URL, status=400, json={"error_code": 400, "error_message": "Bad Request. The series does not exist."})
    responses.get(OBSERVATIONS_URL, json={"observations": [{"date": "2020-01-01", "value": "1"}]})
    src = FredSource(requests.Session(), api_key="k")
    out = src.fetch([make_indicator(id="bad", series="NOPE"), make_indicator(id="ok", series="OK")], START)
    assert isinstance(out["bad"], Exception) and "does not exist" in str(out["bad"])
    assert isinstance(out["ok"], pd.Series)


def test_fred_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert FredSource(requests.Session()).unavailable_reason()


# ───────────── 日本銀行 ─────────────
@pytest.mark.parametrize(
    "value,freq,expected",
    [
        (20260918, "DAILY", "2026-09-18"),
        (202608, "MONTHLY", "2026-08-01"),
        (202602, "QUARTERLY", "2026-04-01"),
        (202602, "SEMIANNUAL", "2026-07-01"),
        (2025, "ANNUAL", "2025-01-01"),
    ],
)
def test_parse_boj_date(value, freq, expected):
    assert parse_boj_date(value, freq) == pd.Timestamp(expected)


def test_start_date_param():
    assert start_date_param(date(2020, 5, 1), "D") == "202005"
    assert start_date_param(date(2020, 5, 1), "Q") == "202001"
    assert start_date_param(date(2020, 5, 1), "A") == "2020"


def _boj_body(series, next_position=None):
    return {
        "STATUS": 200,
        "MESSAGEID": "M181000I",
        "NEXTPOSITION": next_position,
        "RESULTSET": [
            {"SERIES_CODE": code, "FREQUENCY": freq, "VALUES": {"SURVEY_DATES": dates, "VALUES": values}}
            for code, freq, dates, values in series
        ],
    }


def _boj(id, series, db="FM01", frequency="M"):
    return make_indicator(id=id, series=series, source="boj", db=db, frequency=frequency, country="JP")


@responses.activate
def test_boj_groups_by_db_and_frequency_and_follows_pagination():
    responses.get(
        DATA_CODE_URL,
        match=[matchers.query_param_matcher({"format": "json", "lang": "en", "db": "PR01", "code": "A,B", "startDate": "202001"})],
        json=_boj_body([("A", "MONTHLY", [202001, 202002], [1.0, None])], next_position=2),
    )
    responses.get(
        DATA_CODE_URL,
        match=[matchers.query_param_matcher(
            {"format": "json", "lang": "en", "db": "PR01", "code": "A,B", "startDate": "202001", "startPosition": "2"})],
        json=_boj_body([("B", "MONTHLY", [202001], [5.0])]),
    )
    responses.get(
        DATA_CODE_URL,
        match=[matchers.query_param_matcher({"format": "json", "lang": "en", "db": "CO", "code": "T", "startDate": "202001"})],
        json=_boj_body([("T", "QUARTERLY", [202001, 202002], [10, 12])]),
    )
    inds = [_boj("a", "A", "PR01"), _boj("b", "B", "PR01"), _boj("t", "T", "CO", "Q")]
    out = BojSource(requests.Session()).fetch(inds, START)
    assert list(out["a"].values) == [1.0]
    assert list(out["b"].values) == [5.0]
    assert list(out["t"].index) == [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-04-01")]
    assert len(responses.calls) == 3


@responses.activate
def test_boj_isolates_invalid_code_by_refetching_individually():
    err = {"STATUS": 400, "MESSAGEID": "M181013E", "MESSAGE": "Nonexistent series code：1"}
    base = {"format": "json", "lang": "en", "db": "FM01", "startDate": "202001"}
    responses.get(DATA_CODE_URL, match=[matchers.query_param_matcher({**base, "code": "GOOD,BAD"})], status=400, json=err)
    responses.get(DATA_CODE_URL, match=[matchers.query_param_matcher({**base, "code": "GOOD"})],
                  json=_boj_body([("GOOD", "MONTHLY", [202001], [1.0])]))
    responses.get(DATA_CODE_URL, match=[matchers.query_param_matcher({**base, "code": "BAD"})], status=400, json=err)
    out = BojSource(requests.Session()).fetch([_boj("g", "GOOD"), _boj("b", "BAD")], START)
    assert isinstance(out["g"], pd.Series)
    assert isinstance(out["b"], Exception) and "Nonexistent" in str(out["b"])


# ───────────── 世界銀行 ─────────────
def _wb_row(iso2, iso3, year, value):
    return {"country": {"id": iso2}, "countryiso3code": iso3, "date": str(year), "value": value}


@responses.activate
def test_worldbank_batches_countries_and_paginates():
    url = "https://api.worldbank.org/v2/country/US;JPN/indicator/NY.GDP.MKTP.KD.ZG"
    responses.get(url, match=[matchers.query_param_matcher({"format": "json", "per_page": "1000", "date": f"2020:{date.today().year}", "page": "1"})],
                  json=[{"page": 1, "pages": 2}, [_wb_row("US", "USA", 2021, 5.8), _wb_row("JP", "JPN", 2021, None)]])
    responses.get(url, match=[matchers.query_param_matcher({"format": "json", "per_page": "1000", "date": f"2020:{date.today().year}", "page": "2"})],
                  json=[{"page": 2, "pages": 2}, [_wb_row("US", "USA", 2020, -2.2), _wb_row("JP", "JPN", 2020, -4.1)]])
    inds = [make_indicator(id="us", country="US", source="worldbank", series="NY.GDP.MKTP.KD.ZG"),
            make_indicator(id="jp", country="JPN", source="worldbank", series="NY.GDP.MKTP.KD.ZG")]
    out = WorldBankSource(requests.Session()).fetch(inds, START)
    assert list(out["us"].values) == [-2.2, 5.8]
    assert list(out["jp"].values) == [-4.1]  # ISO3 指定でも対応付く・null は除外


@responses.activate
def test_worldbank_error_message_and_empty_data():
    responses.get("https://api.worldbank.org/v2/country/US/indicator/BAD.CODE",
                  json=[{"message": [{"id": "120", "key": "Invalid value", "value": "The provided parameter value is not valid"}]}])
    responses.get("https://api.worldbank.org/v2/country/US/indicator/EMPTY", json=[{"page": 1, "pages": 1}, None])
    inds = [make_indicator(id="bad", source="worldbank", series="BAD.CODE"),
            make_indicator(id="empty", source="worldbank", series="EMPTY")]
    out = WorldBankSource(requests.Session()).fetch(inds, START)
    assert "Invalid value" in str(out["bad"])
    assert "有効な観測値" in str(out["empty"])
