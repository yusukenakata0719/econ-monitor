"""Markdown レポート（Actions のジョブサマリー・artifact 用）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from econ_monitor.models import RunResult, format_obs_date
from econ_monitor.rules import Evaluation
from econ_monitor.slack import SEVERITY_ORDER

SEVERITY_LABEL = {"critical": "🔴 重大", "warning": "🟠 警告", "info": "🔵 情報"}


def build_markdown(result: RunResult) -> str:
    cfg = result.config
    ch = result.changes
    new_keys = {e.key for e in ch.new}
    lines: List[str] = [
        "# マクロ経済モニター レポート",
        "",
        f"- 生成日時: {result.generated_at.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- モード: {result.mode}",
        f"- 新規 {len(ch.new)}件 / 解消 {len(ch.resolved)}件 / 継続 {len(ch.ongoing)}件",
    ]
    if result.skipped_sources:
        lines.append("- スキップしたソース: " + ", ".join(f"{k}（{v}）" for k, v in result.skipped_sources.items()))
    if result.run_url:
        lines.append(f"- 実行ログ: {result.run_url}")

    lines += ["", "## アクティブな異常", ""]
    active = [e for e in ch.active if not e.is_data_quality]
    if active:
        lines += ["| 重要度 | 国 | 指標 | ルール | 内容 | 観測期間 | 状態 |", "|---|---|---|---|---|---|---|"]
        for e in _sorted(active):
            lines.append(
                f"| {SEVERITY_LABEL.get(e.severity, e.severity)} | {cfg.country_name(e.indicator.country)} "
                f"| {_esc(e.indicator.name)} | {_esc(e.description or e.rule_id)} | {_esc(e.message)} "
                f"| {format_obs_date(e.obs_date, e.frequency)} | {'🆕 新規' if e.key in new_keys else '継続'} |"
            )
    else:
        lines.append("なし")

    if ch.resolved:
        lines += ["", "## 解消した異常", ""]
        for r in ch.resolved:
            e = r.evaluation
            lines.append(f"- [{cfg.country_name(e.indicator.country)}] {e.indicator.name} / {e.description or e.rule_id}: {e.message}（{r.since[:10]} から）")

    quality = [e for e in ch.active if e.is_data_quality]
    if quality:
        lines += ["", "## データ品質", ""]
        for e in _sorted(quality):
            lines.append(f"- [{cfg.country_name(e.indicator.country)}] {e.indicator.name}（{e.indicator.source}:{e.indicator.series}）: {e.description} — {_esc(e.message)}")

    lines += ["", "## 全指標の最新値", "", "| 国 | 指標 | ソース | 最新値 | 前回差 | 観測期間 | 状態 |", "|---|---|---|---:|---:|---|---|"]
    active_by_ind = {}
    for e in ch.active:
        active_by_ind.setdefault(e.indicator.id, []).append(e)
    country_order = {code: i for i, code in enumerate(cfg.countries)}
    for s in sorted(result.snapshots, key=lambda s: (country_order.get(s.indicator.country, 99), s.indicator.source, s.indicator.id)):
        ind = s.indicator
        if s.status == "ok":
            flags = active_by_ind.get(ind.id, [])
            status = " ".join(SEVERITY_LABEL.get(e.severity, "")[:1] for e in flags) or "✅"
            value = f"{s.value:,.3f}".rstrip("0").rstrip(".") + (f" {ind.unit}" if ind.unit else "")
            change = f"{s.change:+,.3f}".rstrip("0").rstrip(".") if s.change is not None else "-"
        else:
            status = "⚠️ 取得失敗" if s.status == "error" else "⏭ スキップ"
            value, change = "-", "-"
        lines.append(
            f"| {cfg.country_name(ind.country)} | {_esc(ind.name)} | {ind.source}:{ind.series} "
            f"| {value} | {change} | {format_obs_date(s.obs_date, s.frequency)} | {status} |"
        )
    return "\n".join(lines) + "\n"


def write_report(result: RunResult, path: str | Path | None) -> str:
    md = build_markdown(result)
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    return md


def _sorted(evs: List[Evaluation]) -> List[Evaluation]:
    return sorted(evs, key=lambda e: (SEVERITY_ORDER.get(e.severity, 9), e.indicator.country, e.indicator.id))


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
