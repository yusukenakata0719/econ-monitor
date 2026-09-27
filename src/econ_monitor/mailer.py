"""SMTP によるメール通知（HTML ＋ テキストの multipart）。

Gmail・iCloud メールなど、SMTP 送信に対応したアカウントならどれでも使える。
設定は環境変数（GitHub Actions では Secrets）から読む:

  SMTP_HOST      例: smtp.gmail.com / smtp.mail.me.com
  SMTP_PORT      既定 587（STARTTLS）。465 なら SSL
  SMTP_USERNAME  ログインに使うメールアドレス
  SMTP_PASSWORD  アプリ用パスワード
  MAIL_TO        宛先（カンマ区切りで複数可）
  MAIL_FROM      差出人（省略時は SMTP_USERNAME）
  SMTP_SECURITY  starttls / ssl / none（省略時はポートから自動）
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Mapping, Optional

from econ_monitor.digest import Digest
from econ_monitor.models import RunResult
from econ_monitor.report import snapshot_rows

SMTP_TIMEOUT = 30

ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "resolved": "✅"}
SECTION_ICON = {"new": "🚨", "resolved": "✅", "ongoing": "⏳", "quality": "⚠️"}
SECTION_COLOR = {"new": "#c0392b", "resolved": "#1e8449", "ongoing": "#b9770e", "quality": "#7f8c8d"}


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: List[str]
    security: str

    REQUIRED = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO")

    @classmethod
    def missing(cls, env: Optional[Mapping[str, str]] = None) -> List[str]:
        env = os.environ if env is None else env
        return [k for k in cls.REQUIRED if not env.get(k, "").strip()]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SmtpSettings":
        env = os.environ if env is None else env
        missing = cls.missing(env)
        if missing:
            raise ValueError(f"メール送信の設定が不足しています: {', '.join(missing)}")
        port = int(env.get("SMTP_PORT", "").strip() or 587)
        security = env.get("SMTP_SECURITY", "").strip().lower() or ("ssl" if port == 465 else "starttls")
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(f"SMTP_SECURITY は starttls / ssl / none のいずれかです: {security}")
        username = env["SMTP_USERNAME"].strip()
        return cls(
            host=env["SMTP_HOST"].strip(),
            port=port,
            username=username,
            password=env["SMTP_PASSWORD"],
            sender=env.get("MAIL_FROM", "").strip() or username,
            recipients=[a.strip() for a in env["MAIL_TO"].split(",") if a.strip()],
            security=security,
        )


def build_subject(digest: Digest) -> str:
    prefix = "[マクロ経済モニター・週次]" if "ダイジェスト" in digest.title else "[マクロ経済モニター]"
    counts = digest.counts.replace(" · ", " / ")
    return f"{prefix} {counts}" + (f"｜{digest.headline}" if digest.headline else "")


def build_message(result: RunResult, digest: Digest, sender: str, recipients: List[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = build_subject(digest)
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(build_text(result, digest))
    msg.add_alternative(build_html(result, digest), subtype="html")
    return msg


def build_text(result: RunResult, digest: Digest) -> str:
    lines = [digest.title, f"{digest.generated} | {digest.counts}", ""]
    for section in digest.sections:
        lines.append(f"■ {section.title}")
        for i in section.items:
            lines.append(f"{ICON.get(i.kind, '')} [{i.label}] {i.name} — {i.text}")
        lines.append("")
    if digest.empty_note:
        lines += [digest.empty_note, ""]
    lines.append("■ 全指標の最新値")
    for r in snapshot_rows(result):
        lines.append(f"{r['status']} [{r['country']}] {r['name']}: {r['value']}（前回差 {r['change']}、{r['period']}）")
    lines.append("")
    if digest.skipped:
        lines.append("スキップ: " + ", ".join(f"{k}（{v}）" for k, v in digest.skipped.items()))
    if digest.run_url:
        lines.append(f"実行結果: {digest.run_url}")
    return "\n".join(lines) + "\n"


def build_html(result: RunResult, digest: Digest) -> str:
    e = html.escape
    parts = [
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Hiragino Sans\',\'Yu Gothic\',sans-serif;'
        'max-width:760px;margin:0 auto;color:#222;line-height:1.6">',
        f'<h2 style="margin:0 0 4px">📊 {e(digest.title)}</h2>',
        f'<p style="margin:0 0 16px;color:#666;font-size:13px">{e(digest.generated)} ｜ {e(digest.counts)}</p>',
    ]
    for section in digest.sections:
        color = SECTION_COLOR.get(section.kind, "#333")
        parts.append(
            f'<h3 style="margin:20px 0 8px;padding-left:8px;border-left:4px solid {color};font-size:16px">'
            f"{SECTION_ICON.get(section.kind, '')} {e(section.title)}</h3>"
        )
        parts.append('<ul style="margin:0;padding-left:4px;list-style:none">')
        for i in section.items:
            name = f'<a href="{e(i.url)}" style="color:#1a5fb4">{e(i.name)}</a>' if i.url else e(i.name)
            parts.append(
                f'<li style="margin:0 0 8px">{ICON.get(i.kind, "")} <b>[{e(i.label)}] {name}</b><br>'
                f'<span style="font-size:14px">{e(i.text)}</span></li>'
            )
        parts.append("</ul>")
    if digest.empty_note:
        parts.append(f"<p>{e(digest.empty_note)}</p>")

    th = 'style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;background:#f6f8fa;white-space:nowrap"'
    td = 'style="padding:5px 8px;border-bottom:1px solid #eee"'
    tdr = 'style="padding:5px 8px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap"'
    parts += [
        '<h3 style="margin:28px 0 8px;font-size:16px">全指標の最新値</h3>',
        '<table style="border-collapse:collapse;width:100%;font-size:13px">',
        f"<tr><th {th}>国</th><th {th}>指標</th><th {th}>最新値</th><th {th}>前回差</th><th {th}>期間</th><th {th}>状態</th></tr>",
    ]
    for r in snapshot_rows(result):
        parts.append(
            f"<tr><td {td}>{e(r['country'])}</td><td {td}>{e(r['name'])}</td><td {tdr}>{e(r['value'])}</td>"
            f"<td {tdr}>{e(r['change'])}</td><td {td}>{e(r['period'])}</td><td {td}>{e(r['status'])}</td></tr>"
        )
    parts.append("</table>")

    footer = []
    if digest.skipped:
        footer.append("スキップ: " + e(", ".join(f"{k}（{v}）" for k, v in digest.skipped.items())))
    if digest.run_url:
        footer.append(f'<a href="{e(digest.run_url)}" style="color:#1a5fb4">GitHub Actions の実行結果・レポート</a>')
    footer.append("このメールは econ-monitor（GitHub Actions）から自動送信されています。")
    parts.append(f'<p style="margin-top:24px;color:#888;font-size:12px">{"<br>".join(footer)}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def send(msg: EmailMessage, settings: SmtpSettings) -> None:
    context = ssl.create_default_context()
    try:
        if settings.security == "ssl":
            server = smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT)
        with server:
            if settings.security == "starttls":
                server.starttls(context=context)
            server.login(settings.username, settings.password)
            server.send_message(msg, from_addr=settings.sender, to_addrs=settings.recipients)
    except (smtplib.SMTPException, OSError) as e:
        # 例外メッセージにパスワードは含まれないが、念のため種類と概要だけを返す
        raise RuntimeError(f"メールの送信に失敗しました: {type(e).__name__}: {e}") from None
