from __future__ import annotations

import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from econ_monitor import cli, mailer
from econ_monitor.config import parse_config
from econ_monitor.digest import build_digest, should_notify
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

SMTP_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "me@example.com",
    "SMTP_PASSWORD": "app-pass",
    "MAIL_TO": "a@example.com, b@example.com",
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


def _email(result):
    msg = mailer.build_message(result, build_digest(result), "monitor@example.com", ["me@example.com", "team@example.com"])
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    text_part = msg.get_body(preferencelist=("plain",)).get_content()
    return msg, html_part, text_part


def test_run_detects_new_then_ongoing_then_resolved(tmp_path):
    cfg = parse_config(RAW)
    state = tmp_path / "alerts.json"

    result, store, _ = run(cfg, _sources(-0.2), state, "changes", NOW)
    assert {e.key for e in result.changes.new} == {"us_spread:inversion", "jp_broken:fetch_error"}
    assert should_notify(result)
    store.save(state)

    result, store, _ = run(cfg, _sources(-0.3), state, "changes", NOW)
    assert not result.changes.new and not should_notify(result)
    assert result.mode == "changes" and len(result.changes.ongoing) == 2
    store.save(state)

    result, _, _ = run(cfg, _sources(0.4), state, "changes", NOW)
    assert [r.evaluation.key for r in result.changes.resolved] == ["us_spread:inversion"]
    assert should_notify(result)


def test_email_and_report_contents(tmp_path):
    cfg = parse_config(RAW)
    result, _, _ = run(cfg, _sources(-0.2), tmp_path / "s.json", "changes", NOW, run_url="https://github.com/o/r/actions/runs/1")
    msg, html_part, text_part = _email(result)

    assert msg["Subject"] == "[マクロ経済モニター] 新規 2件 / 解消 0件 / 継続 0件｜米国 長短金利差 ほか"
    assert msg["To"] == "me@example.com, team@example.com"
    assert '<a href="https://fred.stlouisfed.org/series/T10Y2Y" style="color:#1a5fb4">長短金利差</a>' in html_part
    assert "逆イールド" in html_part and "2026-09" in html_part
    assert "データ品質" in html_part and "HTTP 500" in html_part
    assert "https://github.com/o/r/actions/runs/1" in html_part
    assert "全指標の最新値" in html_part and "企業物価" in html_part  # 異常のない指標も一覧に載る
    assert "🟠 [米国] 長短金利差 — 逆イールド: 水準 -0.20%pt（閾値: 0以下）（2026-09）" in text_part
    assert "⚠️ 取得失敗 [日本] 壊れた系列" in text_part

    md = build_markdown(result)
    assert "| 🟠 警告 | 米国 | 長短金利差 | 逆イールド |" in md
    assert "⚠️ 取得失敗" in md


def test_email_escapes_html(tmp_path):
    raw = {"indicators": [{"id": "x", "name": "<script>alert(1)</script>", "country": "US", "source": "fred", "series": "S",
                           "rules": [{"id": "r", "type": "threshold", "above": 0}]}]}
    result, _, _ = run(parse_config(raw), {"fred": FakeSource({"x": _series([1.0, 2.0])})}, tmp_path / "s.json", "changes", NOW)
    _, html_part, _ = _email(result)
    assert "<script>" not in html_part and "&lt;script&gt;" in html_part


def test_digest_lists_ongoing_and_data_quality(tmp_path):
    cfg = parse_config(RAW)
    state = tmp_path / "s.json"
    _, store, _ = run(cfg, _sources(-0.2), state, "changes", NOW)
    store.save(state)

    result, _, _ = run(cfg, _sources(-0.3), state, "digest", NOW)
    msg, html_part, _ = _email(result)
    assert msg["Subject"].startswith("[マクロ経済モニター・週次] 新規 0件 / 解消 0件 / 継続 2件")
    assert "継続中の異常" in html_part and "逆イールド" in html_part
    assert "データ品質" in html_part  # ダイジェストでは継続中の取得エラーも載せる


def test_digest_without_anomalies_says_so(tmp_path):
    raw = {"indicators": [{"id": "x", "name": "X", "country": "US", "source": "fred", "series": "S",
                           "rules": [{"id": "r", "type": "threshold", "above": 100}]}]}
    result, _, _ = run(parse_config(raw), {"fred": FakeSource({"x": _series([1.0, 2.0])})}, tmp_path / "s.json", "digest", NOW)
    assert should_notify(result)
    msg, html_part, _ = _email(result)
    assert "現在アクティブな異常はありません。" in html_part
    assert msg["Subject"] == "[マクロ経済モニター・週次] 新規 0件 / 解消 0件 / 継続 0件"


def test_skipped_sources_are_shown(tmp_path):
    sources = _sources(-0.2)
    sources["fred"] = FakeSource({}, reason="環境変数 FRED_API_KEY が未設定")
    result, _, _ = run(parse_config(RAW), sources, tmp_path / "s.json", "changes", NOW)
    assert result.skipped_sources == {"fred": "環境変数 FRED_API_KEY が未設定"}
    _, html_part, text_part = _email(result)
    assert "FRED_API_KEY" in html_part and "FRED_API_KEY" in text_part


def test_identical_fetch_errors_are_grouped(tmp_path):
    raw = {"indicators": [
        {"id": f"i{n}", "name": f"指標{n}", "country": "US", "source": "fred", "series": f"S{n}",
         "rules": [{"id": "r", "type": "threshold", "above": 0}]}
        for n in range(8)
    ]}
    src = {"fred": FakeSource({f"i{n}": RuntimeError("api_key is invalid") for n in range(8)})}
    result, _, _ = run(parse_config(raw), src, tmp_path / "s.json", "changes", NOW)
    _, html_part, _ = _email(result)
    assert html_part.count("api_key is invalid") == 1
    assert "[fred] 8 系列" in html_part and "S0, S1, S2, S3, S4 ほか" in html_part


# ───────────── SMTP 設定と送信 ─────────────
def test_smtp_settings_from_env():
    s = mailer.SmtpSettings.from_env(SMTP_ENV)
    assert (s.host, s.port, s.security, s.sender) == ("smtp.example.com", 587, "starttls", "me@example.com")
    assert s.recipients == ["a@example.com", "b@example.com"]
    s = mailer.SmtpSettings.from_env({**SMTP_ENV, "SMTP_PORT": "465", "MAIL_FROM": "Monitor <bot@example.com>"})
    assert (s.port, s.security, s.sender) == (465, "ssl", "Monitor <bot@example.com>")
    assert mailer.SmtpSettings.missing({"SMTP_HOST": "x"}) == ["SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO"]
    with pytest.raises(ValueError, match="SMTP_SECURITY"):
        mailer.SmtpSettings.from_env({**SMTP_ENV, "SMTP_SECURITY": "tls13"})


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append(("send", from_addr, tuple(to_addrs)))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _built_message(tmp_path, settings):
    result, _, _ = run(parse_config(RAW), _sources(-0.2), tmp_path / "s.json", "changes", NOW)
    return mailer.build_message(result, build_digest(result), settings.sender, settings.recipients)


def test_send_uses_starttls_and_login(tmp_path, fake_smtp):
    settings = mailer.SmtpSettings.from_env(SMTP_ENV)
    mailer.send(_built_message(tmp_path, settings), settings)
    assert fake_smtp.instances[0].calls == [
        "starttls",
        ("login", "me@example.com", "app-pass"),
        ("send", "me@example.com", ("a@example.com", "b@example.com")),
    ]


def test_send_over_ssl_skips_starttls(tmp_path, fake_smtp):
    settings = mailer.SmtpSettings.from_env({**SMTP_ENV, "SMTP_PORT": "465"})
    mailer.send(_built_message(tmp_path, settings), settings)
    assert "starttls" not in fake_smtp.instances[0].calls


def test_send_wraps_smtp_errors(tmp_path, monkeypatch):
    class Refused(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Refused)
    settings = mailer.SmtpSettings.from_env(SMTP_ENV)
    with pytest.raises(RuntimeError, match="SMTPAuthenticationError"):
        mailer.send(_built_message(tmp_path, settings), settings)


# ───────────── CLI ─────────────
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(RAW, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(cli, "build_sources", lambda session, settings: _sources(-0.2))
    for k in ("GITHUB_STEP_SUMMARY", *mailer.SmtpSettings.REQUIRED, "SMTP_PORT", "SMTP_SECURITY", "MAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    args = ["--config", str(cfg_path), "--state", str(tmp_path / "st.json"), "--report", str(tmp_path / "r.md")]
    return tmp_path, args


def test_cli_dry_run_prints_email_and_saves_preview(cli_env, capsys):
    tmp_path, args = cli_env
    assert cli.main(args + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Subject: [マクロ経済モニター] 新規 2件")
    assert "逆イールド" in out
    assert "長短金利差" in (tmp_path / "email_preview.html").read_text(encoding="utf-8")
    assert (tmp_path / "r.md").exists()
    assert not (tmp_path / "st.json").exists()


def test_cli_missing_smtp_settings_does_not_save_state(cli_env):
    tmp_path, args = cli_env
    assert cli.main(args) == 1
    assert not (tmp_path / "st.json").exists()


def test_cli_does_not_save_state_when_send_fails(cli_env, monkeypatch):
    tmp_path, args = cli_env
    for k, v in SMTP_ENV.items():
        monkeypatch.setenv(k, v)

    def boom(msg, settings):
        raise RuntimeError("メールの送信に失敗しました: SMTPAuthenticationError")

    monkeypatch.setattr(mailer, "send", boom)
    assert cli.main(args) == 1
    assert not (tmp_path / "st.json").exists()

    sent = []
    monkeypatch.setattr(mailer, "send", lambda msg, settings: sent.append(msg))
    assert cli.main(args) == 0 and len(sent) == 1
    assert (tmp_path / "st.json").exists()
    assert cli.main(args) == 0 and len(sent) == 1  # 変化がなければ再通知しない


def test_cli_no_notify_saves_state_without_sending(cli_env, monkeypatch):
    tmp_path, args = cli_env
    monkeypatch.setattr(mailer, "send", lambda *a: pytest.fail("送信してはいけない"))
    assert cli.main(args + ["--no-notify"]) == 0
    assert (tmp_path / "st.json").exists()


def test_cli_invalid_config_returns_2(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("indicators: []\n", encoding="utf-8")
    assert cli.main(["--config", str(bad)]) == 2
