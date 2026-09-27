from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from econ_monitor import cli, slack
from econ_monitor.config import parse_config
from econ_monitor.pipeline import run
from econ_monitor.report import build_markdown
from econ_monitor.sources.base import Source

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

RAW = {
    "countries": {"US": "米国", "JP": "日本"},
    "indicators": [
        {"id": "us_spread", "name": "長短金利差", "country": "US", "source": "fred", "series": "T10Y2Y", "unit": "%pt",
         "rules": [{"id": "inversion", "type": "threshold", "below": 0, "description": "逆イールド"}]},
        {"id": "jp_ppi", "name": "企業物価", "country": "JP", "source": "boj", "series": "P", "db": "PR01", "frequency": "M",
         "rules": [{"id": "hot", "type": "threshold", "transform": "yoy", "above": 5}]},
        {"id": "jp_broken", "name": "壊れた系列", "country": "JP", "source": "boj", "series": "Z", "db": "PR01", "frequency": "M",
         "rules": [{"id": "r", "type": "threshold", "above": 1}]},
    ],
}


class FakeSource(Source):
    def __init__(self, data, reason=None):
        self.data, self.reason = data, reason

    def unavailable_reason(self):
        return self.reason

    def fetch(self, indicators, start):
        return {i.id: self.data[i.id] for i in indicators}


def _series(values, end="2026-09-01"):
    return pd.Series(values, index=pd.date_range(end=end, periods=len(values), freq="MS"), dtype=float)


def _sources(spread_last):
    return {
        "fred": FakeSource({"us_spread": _series([0.5] * 30 + [spread_last])}),
        "boj": FakeSource({"jp_ppi": _series([100.0] * 12 + [103.0], end="2026-08-01"), "jp_broken": RuntimeError("HTTP 500")}),
        "worldbank": FakeSource({}),
    }


def test_run_detects_new_then_ongoing_then_resolved(tmp_path):
    cfg = parse_config(RAW)
    state = tmp_path / "alerts.json"

    result, store, _ = run(cfg, _sources(-0.2), state, "changes", NOW)
    assert {e.key for e in result.changes.new} == {"us_spread:inversion", "jp_broken:fetch_error"}
    assert slack.should_notify(result)
    store.save(state)

    result, store, _ = run(cfg, _sources(-0.3), state, "changes", NOW)
    assert not result.changes.new and not slack.should_notify(result)
    assert result.mode == "changes" and len(result.changes.ongoing) == 2
    store.save(state)

    result, _, _ = run(cfg, _sources(0.4), state, "changes", NOW)
    assert [r.evaluation.key for r in result.changes.resolved] == ["us_spread:inversion"]
    assert slack.should_notify(result)


def test_payload_and_report_contents(tmp_path):
    cfg = parse_config(RAW)
    result, _, _ = run(cfg, _sources(-0.2), tmp_path / "s.json", "changes", NOW, run_url="https://github.com/o/r/actions/runs/1")
    payload = slack.build_payload(result)
    text = json.dumps(payload, ensure_ascii=False)
    assert "[米国] <https://fred.stlouisfed.org/series/T10Y2Y|長短金利差>" in text
    assert "逆イールド" in text and "2026-09" in text
    assert "データ品質" in text and "HTTP 500" in text
    assert "https://github.com/o/r/actions/runs/1" in text
    assert payload["text"].startswith("マクロ経済モニター")
    for b in payload["blocks"]:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= 3000

    md = build_markdown(result)
    assert "| 🟠 警告 | 米国 | 長短金利差 | 逆イールド |" in md
    assert "⚠️ 取得失敗" in md


def test_digest_lists_ongoing_and_skipped_sources(tmp_path):
    cfg = parse_config(RAW)
    sources = _sources(-0.2)
    sources["fred"] = FakeSource({}, reason="環境変数 FRED_API_KEY が未設定")
    result, store, _ = run(cfg, sources, tmp_path / "s.json", "digest", NOW)
    assert result.skipped_sources == {"fred": "環境変数 FRED_API_KEY が未設定"}
    text = json.dumps(slack.build_payload(result), ensure_ascii=False)
    assert "週次ダイジェスト" in text and "FRED_API_KEY" in text


def test_payload_is_truncated_to_block_limit(tmp_path):
    raw = {"indicators": [
        {"id": f"i{n}", "name": "x" * 200, "country": "US", "source": "fred", "series": f"S{n}",
         "rules": [{"id": "r", "type": "threshold", "above": 0}]}
        for n in range(400)
    ]}
    cfg = parse_config(raw)
    src = {"fred": FakeSource({f"i{n}": _series([1.0, 2.0]) for n in range(400)})}
    result, _, _ = run(cfg, src, tmp_path / "s.json", "changes", NOW)
    payload = slack.build_payload(result)
    assert len(payload["blocks"]) <= slack.MAX_BLOCKS


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(RAW, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cli, "build_sources", lambda session, settings: _sources(-0.2))
    for k in ("GITHUB_STEP_SUMMARY", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path, cfg_path


def test_cli_dry_run_does_not_save_state(cli_env, capsys):
    tmp_path, cfg_path = cli_env
    code = cli.main(["--config", str(cfg_path), "--state", str(tmp_path / "st.json"), "--report", str(tmp_path / "r.md"), "--dry-run"])
    assert code == 0
    assert not (tmp_path / "st.json").exists()
    assert (tmp_path / "r.md").exists()
    assert json.loads(capsys.readouterr().out)["blocks"]


def test_cli_does_not_save_state_when_slack_fails(cli_env, monkeypatch):
    tmp_path, cfg_path = cli_env
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")

    def boom(*a, **k):
        raise RuntimeError("Slack への送信に失敗しました: HTTP 500")

    monkeypatch.setattr(slack, "send", boom)
    args = ["--config", str(cfg_path), "--state", str(tmp_path / "st.json"), "--report", str(tmp_path / "r.md")]
    assert cli.main(args) == 1
    assert not (tmp_path / "st.json").exists()

    sent = []
    monkeypatch.setattr(slack, "send", lambda url, payload, session: sent.append(payload))
    assert cli.main(args) == 0 and len(sent) == 1
    assert (tmp_path / "st.json").exists()
    assert cli.main(args) == 0 and len(sent) == 1  # 変化がなければ再通知しない


def test_cli_invalid_config_returns_2(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("indicators: []\n", encoding="utf-8")
    assert cli.main(["--config", str(bad)]) == 2


def test_identical_fetch_errors_are_grouped(tmp_path):
    raw = {"indicators": [
        {"id": f"i{n}", "name": f"指標{n}", "country": "US", "source": "fred", "series": f"S{n}",
         "rules": [{"id": "r", "type": "threshold", "above": 0}]}
        for n in range(8)
    ]}
    cfg = parse_config(raw)
    src = {"fred": FakeSource({f"i{n}": RuntimeError("api_key is invalid") for n in range(8)})}
    result, _, _ = run(cfg, src, tmp_path / "s.json", "changes", NOW)
    text = json.dumps(slack.build_payload(result), ensure_ascii=False)
    assert text.count("api_key is invalid") == 1
    assert "[fred] 8 系列" in text and "S0, S1, S2, S3, S4 ほか" in text
