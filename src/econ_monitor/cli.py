"""コマンドラインエントリポイント: `econ-monitor` / `python -m econ_monitor`"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from econ_monitor import mailer
from econ_monitor.config import ConfigError, load_config
from econ_monitor.digest import build_digest, should_notify
from econ_monitor.http import make_session
from econ_monitor.models import RunResult
from econ_monitor.pipeline import run
from econ_monitor.report import write_report
from econ_monitor.sources import build_sources

log = logging.getLogger("econ_monitor")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="主要国マクロ経済指標の異常検知とメール通知")
    p.add_argument("--config", default="config/indicators.yaml", help="指標定義ファイル")
    p.add_argument("--state", default="state/alerts.json", help="アラート状態ファイル（実行間で引き継ぐ）")
    p.add_argument(
        "--mode",
        choices=("changes", "digest"),
        default="changes",
        help="changes: 新規発生・解消があるときだけ通知 / digest: アクティブな異常をすべて通知",
    )
    p.add_argument("--report", default="reports/report.md", help="Markdown レポートの出力先")
    p.add_argument("--data-dir", default=None, help="取得した系列を CSV で保存するディレクトリ")
    p.add_argument("--dry-run", action="store_true", help="メールを送信せず、状態も保存しない（本文を標準出力に、HTML をレポートと同じ場所に出力）")
    p.add_argument("--no-notify", action="store_true", help="メールを送信せず状態だけ更新する（初回の状態作成などに）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as e:
        log.error("設定ファイルの読み込みに失敗しました: %s", e)
        return 2

    now = datetime.now(ZoneInfo(config.settings.timezone))
    session = make_session()
    result, store, results = run(
        config,
        build_sources(session, config.settings),
        state_path=args.state,
        mode=args.mode,
        now=now,
        run_url=_github_run_url(),
        data_dir=args.data_dir,
    )
    write_report(result, args.report)

    ch = result.changes
    log.info("新規 %d / 解消 %d / 継続 %d", len(ch.new), len(ch.resolved), len(ch.ongoing))
    for e in ch.new:
        log.info("[新規] %s/%s: %s", e.indicator.id, e.rule_id, e.message)

    if should_notify(result):
        if not _notify(args, result):
            # 状態を保存しないことで、次回実行時に同じ変化を再通知する
            return 1
    else:
        log.info("通知対象の変化はありません")

    if not args.dry_run:
        store.save(args.state)

    fetched = [v for v in results.values() if not isinstance(v, BaseException)]
    if results and not fetched:
        log.error("すべての系列の取得に失敗しました")
        return 1
    return 0


def _notify(args: argparse.Namespace, result: RunResult) -> bool:
    """メールで通知する。送信できなかった（設定不足・送信失敗）場合は False。"""
    digest = build_digest(result)
    if args.dry_run:
        msg = mailer.build_message(result, digest, "sender@example.com", ["recipient@example.com"])
        print(f"Subject: {msg['Subject']}\n")
        print(mailer.build_text(result, digest))
        if args.report:
            preview = Path(args.report).with_name("email_preview.html")
            preview.write_text(mailer.build_html(result, digest), encoding="utf-8")
            log.info("メールのプレビューを %s に保存しました", preview)
        return True
    if args.no_notify:
        log.info("--no-notify のため通知をスキップ")
        return True
    missing = mailer.SmtpSettings.missing()
    if missing:
        log.error("メール送信の設定が不足しているため通知できません: %s（状態は保存しません）", ", ".join(missing))
        return False
    try:
        settings = mailer.SmtpSettings.from_env()
        mailer.send(mailer.build_message(result, digest, settings.sender, settings.recipients), settings)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return False
    log.info("メールを %d 件の宛先に送信しました", len(settings.recipients))
    return True


def _github_run_url() -> Optional[str]:
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


if __name__ == "__main__":
    sys.exit(main())
