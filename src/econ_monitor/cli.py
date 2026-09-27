"""コマンドラインエントリポイント: `econ-monitor` / `python -m econ_monitor`"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from econ_monitor import slack
from econ_monitor.config import ConfigError, load_config
from econ_monitor.http import make_session
from econ_monitor.pipeline import run
from econ_monitor.report import write_report
from econ_monitor.sources import build_sources

log = logging.getLogger("econ_monitor")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="主要国マクロ経済指標の異常検知と Slack 通知")
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
    p.add_argument("--dry-run", action="store_true", help="Slack に送信せず、状態も保存しない（ペイロードを標準出力に表示）")
    p.add_argument("--no-notify", action="store_true", help="Slack に送信せず状態だけ更新する（初回の状態作成などに）")
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

    if slack.should_notify(result):
        payload = slack.build_payload(result)
        webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.no_notify:
            log.info("--no-notify のため Slack 通知をスキップ")
        elif not webhook:
            log.warning("SLACK_WEBHOOK_URL が未設定のため通知をスキップ（状態は保存しません）")
            return 1
        else:
            try:
                slack.send(webhook, payload, session)
                log.info("Slack に通知しました")
            except Exception as e:  # noqa: BLE001
                # 状態を保存しないことで、次回実行時に同じ変化を再通知する
                log.error("%s", e)
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


def _github_run_url() -> Optional[str]:
    server, repo, run_id = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


if __name__ == "__main__":
    sys.exit(main())
