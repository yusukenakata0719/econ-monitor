# econ-monitor — 主要国マクロ経済モニター

FRED・日本銀行・世界銀行の API から主要国のマクロ経済指標を毎日取得し、異常を検知したらメールで通知するツールです。GitHub Actions で自動実行します。

```
GitHub Actions (cron: 毎日 08:00 JST / 月曜 08:30 JST にダイジェスト)
  │
  ├─ 取得 ──── FRED（米国・ユーロ圏・英国・中国・市場指標）
  │            日本銀行 時系列統計データ検索サイト API（金利・為替・物価・短観・M2・国際収支）
  │            世界銀行 Indicators API（G7＋中国・インドの年次指標）
  │
  ├─ 変換 ──── 前年比 / 差分 / 前期比年率 / サーム指標 など
  ├─ 検知 ──── 閾値ルール・Z スコア（外れ値）・データ更新停止・取得エラー
  ├─ 差分 ──── 前回の状態（actions/cache で保持）と比較して「新規発生」「解消」だけを抽出
  │
  └─ 出力 ──── メール通知（HTML）／ジョブサマリー＋Markdown レポート＋取得データ CSV（artifact）
```

## 監視している指標（`config/indicators.yaml`）

| 地域 | 主な指標と検知ルール |
|---|---|
| 米国 | 失業率（**サームルール**・急上昇）、雇用者数（前月比マイナス）、CPI・コア PCE（インフレ率の閾値）、実質 GDP（マイナス成長）、鉱工業生産、新規失業保険申請、**長短金利差（逆イールド）**、10 年債利回りの急変、FF 金利の変更、HY スプレッド、ドル指数、消費者信頼感 |
| 日本 | コール翌日物（政策金利の変更）、ドル円（急変動・160 円超）、実質実効為替レート、企業物価・企業向けサービス価格、**日銀短観 DI**、M2、経常収支 |
| ユーロ圏・英国 | HICP、ECB 預金金利、独・英 10 年債利回り、SONIA、英失業率（サームルール）、ユーロ・ポンド相場 |
| 中国・グローバル | 人民元相場、VIX（30 超・45 超）、WTI 原油 |
| G7＋中国・インド（年次） | 実質 GDP 成長率、インフレ率、失業率、経常収支 GDP 比 |

## セットアップ

### 1. リポジトリを GitHub に作成して push

```bash
git init && git add . && git commit -m "Initial commit"
gh repo create econ-monitor --private --source . --push
```

### 2. API キーとメール送信用のアカウントを用意

- **FRED API キー**: https://fred.stlouisfed.org/docs/api/api_key.html で無料登録して発行（日本銀行・世界銀行はキー不要）
- **送信用メールアカウント**: SMTP で送信できるアカウントの「アプリ用パスワード」を発行します（通常のログインパスワードは使えません）

| サービス | SMTP_HOST | SMTP_PORT | SMTP_USERNAME | アプリ用パスワードの発行 |
|---|---|---|---|---|
| Gmail | `smtp.gmail.com` | `587` | Gmail アドレス | 2 段階認証を有効にして https://myaccount.google.com/apppasswords |
| iCloud メール | `smtp.mail.me.com` | `587` | iCloud メールアドレス（`@icloud.com` など） | https://account.apple.com →「サインインとセキュリティ」→「App 用パスワード」 |

### 3. GitHub Secrets に登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** から登録します（値をチャットや端末の履歴に残さないため、ブラウザでの登録がおすすめです）。

| Secret | 内容 | 必須 |
|---|---|---|
| `FRED_API_KEY` | FRED の API キー | ✅ |
| `SMTP_HOST` | 上の表の SMTP サーバー | ✅ |
| `SMTP_USERNAME` | ログインに使うメールアドレス | ✅ |
| `SMTP_PASSWORD` | アプリ用パスワード | ✅ |
| `MAIL_TO` | 宛先（カンマ区切りで複数可） | ✅ |
| `SMTP_PORT` | 省略時 `587`（STARTTLS）。`465` なら SSL | |
| `MAIL_FROM` | 差出人。省略時は `SMTP_USERNAME`（iCloud・Gmail は自分のアドレスのままにする） | |

### 4. 動作確認と初回実行

1. **Actions → Macro Monitor → Run workflow** で `dry_run` にチェックして実行し、ジョブサマリーのレポートと artifact の `email_preview.html`（メールの見た目）を確認
2. 初回の本番実行では、現在すでに発生している異常がすべて「新規」として通知されます。
   一括通知が不要なら `no_notify` にチェックして 1 回実行し、状態だけ作っておきます
3. 以降は毎日 08:00 JST に自動実行され、変化があったときだけ通知されます

## 通知の仕組み

- 異常ごと（指標 × ルール）に状態を持ち、**発生したとき 1 回だけ**通知します。続いている間は毎日通知しません
- 条件を満たさなくなったら「解消」を通知します（Z スコアや政策金利変更のような単発イベントは解消通知なし。`notify_resolved` で変更可）
- 毎週月曜はダイジェストとして、継続中の異常も含めて一覧を送ります
- データ取得エラーや、系列が長期間更新されていない（公表停止の疑い）場合も「データ品質」として通知します
- メールの送信に失敗した場合は状態を保存しないため、次回の実行で再通知されます

メールの例（HTML 版では指標名がデータの出典ページへのリンクになり、末尾に全指標の最新値の一覧表が付きます）:

```
件名: [マクロ経済モニター] 新規 2件 / 解消 1件 / 継続 3件｜米国 失業率 ほか

マクロ経済モニター
2026-09-27 08:00 JST | 新規 2件 · 解消 1件 · 継続 3件

■ 新たに検知した異常
🔴 [米国] 失業率 — サームルール発動（景気後退シグナル）: サーム指標 0.53%（閾値: 0.50以上）（2026-08）
🟠 [日本] 国内企業物価指数 — 企業物価が前年比 5% 超: 前年比 7.59%（閾値: 5.00以上）（2026-08）

■ 解消
✅ [米国] 長短金利差（10年-2年） — 逆イールド: 水準 0.36%pt（閾値: 0以下）（2026-09-25）（2026-07-01 から継続）
```

## 検知ルールの書き方

`config/indicators.yaml` に指標を追加するだけで監視対象を増やせます（コード変更不要）。

```yaml
- id: us_unrate            # 状態管理のキー（変更すると別指標扱い）
  name: 失業率
  country: US
  source: fred             # fred / boj / worldbank
  series: UNRATE           # 各 API の系列コード
  unit: "%"
  rules:
    - id: sahm
      type: threshold      # 最新値が above 以上 / below 以下で発火
      transform: sahm
      above: 0.5
      severity: critical   # info / warning / critical
      description: サームルール発動
    - id: jump
      type: zscore         # 直近 window 期の分布から threshold σ 以上乖離で発火
      transform: diff
      window: 120
      threshold: 3.0
      direction: up        # up / down / both
      robust: false        # true で中央値・MAD（過去の外れ値に強い）
```

| transform | 意味 |
|---|---|
| `level` | 水準そのまま |
| `diff` / `pct` | `periods` 期前との差 / 変化率（%） |
| `yoy` / `yoy_diff` | 1 年前との変化率（%）/ 差（日付基準なので日次・月次どちらでも可） |
| `ann_pct` | 前期比年率（%）。四半期 GDP など |
| `sahm` | 3 期移動平均 −（過去 12 期の 3 期移動平均の最小値） |

ソース別の追加項目:

- **日本銀行**: `db`（例: `FM08`）と `frequency`（`D/W/M/Q/H/A`）が必須。系列コードは[時系列統計データ検索サイト](https://www.stat-search.boj.or.jp/)で確認できます。
  API は同一 DB・同一頻度の系列を最大 250 件まとめて取得し、利用規約に従いリクエスト間に 1.5 秒の間隔を空けています
- **FRED**: `params` で observations API のパラメータを追加できます（例: 日次を月次平均に `{frequency: m, aggregation_method: avg}`）
- **世界銀行**: `countries: [US, JP, ...]` で複数国に展開できます（id は `<id>_us` などになります）

## ローカルでの実行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export FRED_API_KEY=...            # 未設定なら FRED の指標はスキップ
econ-monitor --dry-run             # メールを送らず本文を表示し、reports/email_preview.html を出力（状態も保存しない）
econ-monitor --mode digest --dry-run
pytest                             # テスト（API はモック）
```

主なオプション: `--config` / `--state` / `--report` / `--data-dir`（取得データを CSV 保存）/ `--mode changes|digest` / `--dry-run` / `--no-notify`

## ディレクトリ構成

```
config/indicators.yaml          監視対象の指標と検知ルール
src/econ_monitor/
  sources/{fred,boj,worldbank}.py  API クライアント（リトライ・バッチ取得・ページング）
  transforms.py                 前年比・サーム指標などの変換
  rules.py                      閾値・Z スコア・更新停止の判定
  state.py                      アラート状態の保存と差分（新規／継続／解消）
  digest.py                     通知に載せる内容（新規・解消・継続・データ品質）の組み立て
  mailer.py / report.py         メール通知（SMTP）と Markdown レポート
  pipeline.py / cli.py          全体の処理とコマンドライン
.github/workflows/
  macro-monitor.yml             定期実行（毎日＋週次ダイジェスト）
  ci.yml                        テスト
```

## 運用上の注意

- アラート状態は `actions/cache` に保存しています。キャッシュは 7 日間アクセスがないと削除されますが、毎日実行していれば維持されます。消えた場合は、その時点でアクティブな異常が再通知されるだけです
- GitHub の仕様で、**公開リポジトリ**では 60 日間コミットがないとスケジュール実行が自動停止します（private は対象外）
- スケジュール実行は GitHub 側の混雑で数十分遅れることがあります
- 日本の CPI（総務省）や中国の月次物価は FRED/日銀では更新が止まっているため含めていません。e-Stat API 等のソースを `sources/` に追加すれば拡張できます
