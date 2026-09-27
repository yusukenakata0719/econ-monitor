"""Slack Incoming Webhook への通知（Block Kit）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

from econ_monitor.http import DEFAULT_TIMEOUT
from econ_monitor.models import RunResult, format_obs_date
from econ_monitor.rules import Evaluation

log = logging.getLogger(__name__)

SEVERITY_ICON = {"critical": ":red_circle:", "warning": ":large_orange_circle:", "info": ":large_blue_circle:"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SECTION_TEXT_LIMIT = 2900  # Slack の section テキスト上限は 3000 文字
MAX_BLOCKS = 50
GROUP_MIN_SIZE = 3  # 同じソース・同じ内容のデータ品質アラートがこの件数以上なら 1 行にまとめる


def should_notify(result: RunResult) -> bool:
    if result.mode == "digest":
        return True
    return result.changes.has_notifiable


def build_payload(result: RunResult) -> Dict[str, Any]:
    ch = result.changes
    resolved = ch.notify_resolved
    anomalies_new = _sorted([e for e in ch.new if not e.is_data_quality])
    quality_new = _sorted([e for e in ch.new if e.is_data_quality])

    counts = f"新規 {len(ch.new)}件 · 解消 {len(resolved)}件 · 継続 {len(ch.ongoing)}件"
    title = "マクロ経済モニター" + ("（週次ダイジェスト）" if result.mode == "digest" else "")
    tz_time = result.generated_at.strftime("%Y-%m-%d %H:%M %Z")

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f":bar_chart: {title}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{tz_time} | {counts}"}]},
    ]

    def add_section(heading: str, lines: List[str]) -> None:
        if not lines:
            return
        blocks.append({"type": "divider"})
        for chunk in _chunk_lines([f"*{heading}*"] + lines):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    add_section(":rotating_light: 新たに検知した異常", [_line(result, e) for e in anomalies_new])
    add_section(
        ":white_check_mark: 解消",
        [_line(result, r.evaluation, icon=":white_check_mark:", suffix=f"（{r.since[:10]} から継続）") for r in _sorted_resolved(resolved)],
    )
    if result.mode == "digest":
        ongoing = _sorted([e for e in ch.ongoing if not e.is_data_quality])
        add_section(":hourglass_flowing_sand: 継続中の異常", [_line(result, e) for e in ongoing])
        quality_new = _sorted([e for e in ch.active if e.is_data_quality])
    add_section(":warning: データ品質（取得エラー・更新停止）", _quality_lines(result, quality_new))

    if result.mode == "digest" and not ch.active and not resolved:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "現在アクティブな異常はありません。"}})

    footer = []
    if result.skipped_sources:
        footer.append("スキップ: " + ", ".join(f"{k}（{v}）" for k, v in result.skipped_sources.items()))
    if result.run_url:
        footer.append(f"<{result.run_url}|GitHub Actions の実行結果・レポート>")
    if footer:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " | ".join(footer)}]})

    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[: MAX_BLOCKS - 1] + [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "（件数が多いため省略しました。全件はレポートを参照）"}]}
        ]
    return {"text": f"{title}: {counts}", "blocks": blocks}


def send(webhook_url: str, payload: Dict[str, Any], session: requests.Session) -> None:
    resp = session.post(webhook_url, json=payload, timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Slack への送信に失敗しました: HTTP {resp.status_code} {resp.text[:200]}")


def _line(result: RunResult, e: Evaluation, icon: str | None = None, suffix: str = "") -> str:
    ind = e.indicator
    icon = icon or SEVERITY_ICON.get(e.severity, "")
    country = result.config.country_name(ind.country)
    name = f"<{ind.source_url}|{ind.name}>" if ind.source_url else ind.name
    desc = f"{e.description}: " if e.description else ""
    obs = f"（{format_obs_date(e.obs_date, e.frequency)}）" if e.obs_date and not e.is_data_quality else ""
    return f"{icon} *[{country}] {name}* — {desc}{e.message}{obs}{suffix}"


def _quality_lines(result: RunResult, evs: List[Evaluation]) -> List[str]:
    """API キーの誤りや障害で同じエラーが大量に出た場合に、ソース単位の 1 行にまとめる。"""
    groups: Dict[tuple, List[Evaluation]] = {}
    for e in evs:
        groups.setdefault((e.indicator.source, e.rule_id, e.message), []).append(e)
    lines = []
    for (source, _, message), members in groups.items():
        if len(members) < GROUP_MIN_SIZE or members[0].rule_id != "fetch_error":
            lines.extend(_line(result, e) for e in members)
            continue
        codes = ", ".join(e.indicator.series for e in members[:5]) + (" ほか" if len(members) > 5 else "")
        icon = SEVERITY_ICON.get(members[0].severity, "")
        lines.append(f"{icon} *[{source}] {len(members)} 系列* — {members[0].description}: {message}（{codes}）")
    return lines


def _sorted(evs: List[Evaluation]) -> List[Evaluation]:
    return sorted(evs, key=lambda e: (SEVERITY_ORDER.get(e.severity, 9), e.indicator.country, e.indicator.id))


def _sorted_resolved(rs):
    return sorted(rs, key=lambda r: (SEVERITY_ORDER.get(r.evaluation.severity, 9), r.evaluation.indicator.id))


def _chunk_lines(lines: List[str]) -> List[str]:
    chunks, buf = [], ""
    for line in lines:
        line = line[:SECTION_TEXT_LIMIT]
        if buf and len(buf) + 1 + len(line) > SECTION_TEXT_LIMIT:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks
