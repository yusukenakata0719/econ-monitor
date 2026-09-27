"""通知の内容（どのセクションに何を載せるか）を組み立てる。描画は mailer が行う。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from econ_monitor.models import RunResult, format_obs_date
from econ_monitor.rules import Evaluation

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
GROUP_MIN_SIZE = 3  # 同じソース・同じ内容の取得エラーがこの件数以上なら 1 行にまとめる


@dataclass
class Item:
    kind: str  # critical / warning / info / resolved
    label: str  # 国名、またはまとめた場合はソース名
    name: str
    url: Optional[str]
    text: str


@dataclass
class Section:
    kind: str  # new / resolved / ongoing / quality
    title: str
    items: List[Item]


@dataclass
class Digest:
    title: str
    counts: str
    generated: str
    sections: List[Section]
    empty_note: Optional[str] = None
    skipped: Dict[str, str] = field(default_factory=dict)
    run_url: Optional[str] = None
    headline: str = ""  # 件名などに使う、最も重要な項目の要約


def should_notify(result: RunResult) -> bool:
    if result.mode == "digest":
        return True
    return result.changes.has_notifiable


def build_digest(result: RunResult) -> Digest:
    ch = result.changes
    resolved = ch.notify_resolved
    anomalies_new = sort_evaluations([e for e in ch.new if not e.is_data_quality])
    quality = sort_evaluations([e for e in (ch.active if result.mode == "digest" else ch.new) if e.is_data_quality])

    sections = [
        Section("new", "新たに検知した異常", [_item(result, e) for e in anomalies_new]),
        Section(
            "resolved",
            "解消",
            [
                _item(result, r.evaluation, kind="resolved", suffix=f"（{r.since[:10]} から継続）")
                for r in sorted(resolved, key=lambda r: (SEVERITY_ORDER.get(r.evaluation.severity, 9), r.evaluation.indicator.id))
            ],
        ),
    ]
    if result.mode == "digest":
        ongoing = sort_evaluations([e for e in ch.ongoing if not e.is_data_quality])
        sections.append(Section("ongoing", "継続中の異常", [_item(result, e) for e in ongoing]))
    sections.append(Section("quality", "データ品質（取得エラー・更新停止）", _quality_items(result, quality)))
    sections = [s for s in sections if s.items]

    headline = ""
    for s in sections:
        first = s.items[0]
        headline = f"{first.label} {first.name}" + (" ほか" if sum(len(x.items) for x in sections) > 1 else "")
        break

    return Digest(
        title="マクロ経済モニター" + ("（週次ダイジェスト）" if result.mode == "digest" else ""),
        counts=f"新規 {len(ch.new)}件 · 解消 {len(resolved)}件 · 継続 {len(ch.ongoing)}件",
        generated=result.generated_at.strftime("%Y-%m-%d %H:%M %Z"),
        sections=sections,
        empty_note="現在アクティブな異常はありません。" if result.mode == "digest" and not ch.active and not resolved else None,
        skipped=dict(result.skipped_sources),
        run_url=result.run_url,
        headline=headline,
    )


def sort_evaluations(evs: List[Evaluation]) -> List[Evaluation]:
    return sorted(evs, key=lambda e: (SEVERITY_ORDER.get(e.severity, 9), e.indicator.country, e.indicator.id))


def _item(result: RunResult, e: Evaluation, kind: Optional[str] = None, suffix: str = "") -> Item:
    ind = e.indicator
    desc = f"{e.description}: " if e.description else ""
    obs = f"（{format_obs_date(e.obs_date, e.frequency)}）" if e.obs_date and not e.is_data_quality else ""
    return Item(
        kind=kind or e.severity,
        label=result.config.country_name(ind.country),
        name=ind.name,
        url=ind.source_url,
        text=f"{desc}{e.message}{obs}{suffix}",
    )


def _quality_items(result: RunResult, evs: List[Evaluation]) -> List[Item]:
    """API キーの誤りや障害で同じエラーが大量に出た場合に、ソース単位の 1 行にまとめる。"""
    groups: Dict[tuple, List[Evaluation]] = {}
    for e in evs:
        groups.setdefault((e.indicator.source, e.rule_id, e.message), []).append(e)
    items: List[Item] = []
    for (source, _, message), members in groups.items():
        if len(members) < GROUP_MIN_SIZE or members[0].rule_id != "fetch_error":
            items.extend(_item(result, e) for e in members)
            continue
        codes = ", ".join(e.indicator.series for e in members[:5]) + (" ほか" if len(members) > 5 else "")
        items.append(
            Item(
                kind=members[0].severity,
                label=source,
                name=f"{len(members)} 系列",
                url=None,
                text=f"{members[0].description}: {message}（{codes}）",
            )
        )
    return items
