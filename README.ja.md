# Mercury

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

AI エージェント向けのローカル模擬取引環境です。**ashare-mcp が A 株研究を、Financial Datasets MCP がグローバルデータ基盤を担い、Python が独立した CNY/USD の仮想口座を管理します。** 証券会社への接続や実資金による注文はありません。コアの実行に必要なのは Python 3.11 以降の標準ライブラリだけです。

> 開発状況：A 株の研究・戦略・リスク管理・約定処理・振り返りのループを実装しました。[フローガイド](docs/agent-workflow.md)と[検証記録](docs/repository-acceptance.md)を参照してください。時刻付きの実際の MCP データ取得は確認済みですが、フォワード約定と実際のモデル API 呼び出しは未検証です。FDM は認証済みで、直近の価格取得は credits/balance 制限により停止しました。

```text
Collector subagent -> ashare-mcp (CN) / FDM (global) -> Raw response / time / SHA-256
                                                        |
                                             Local validation and analysis
                                                        |
Frozen Observation -> Research / Strategy JSON -> TradeIntent -> Hard risk limits
                                                        |
                                  SQLite paper fills -> Review -> Next observation
                                                        |
                                           Independent audit + primary review
```

## A 株エージェントの一連のフロー

```powershell
python -m mercury workflow-demo
```

合成例では2つのオフラインエージェントが同じ CNY 初期資金・観測・手数料・ルールを使い、9月25～27日の休場をまたぐ T+1 売買をそれぞれ完了します。モデル API や実際の価格データは使いません。[AGENTS.md](AGENTS.md) に、取得・観測の固定・研究・意図の生成・リスク検証・約定処理・振り返り・独立監査の必須手順を記載しています。

`cycle` は有限回のループを実行し、オフラインエージェント、OpenAI Responses API、または明示的な HTTPS JSON サービスを利用できます。認証情報は環境変数のみから読み込みます。モデルは固定した観測を受け取り、厳格な戦略 DSL を返します。ローカル実行ツールは提供しません。`backtest` と `compare` は独立口座と共有観測で時系列に再生します。設定と JSON 形式は[フローガイド](docs/agent-workflow.md)を参照してください。

CNY 口座は `--risk-policy` で銘柄比率・注文金額・日次売買額・日次損失の上限を固定できます。有効化した口座では直接の `buy`/`sell` を拒否し、検証済み TradeIntent を一括で提出します。`--execution-policy` はスプレッド・遅延・部分約定に対応し、同一価格データの容量を共有し、注文単位で手数料を累積計算します。これらは模擬パラメーターであり、実際の板を再現したものではありません。合成例は実際の API 接続やフォワード収益を立証しません。

## A 株実験（主要市場）

A 株の実験は **ashare-mcp → 独立した CNY 口座**を使います。Financial Datasets はグローバル市場データの基盤と独立した USD 比較口座用に維持します。主要実験で市場や通貨を混在させません。

```powershell
python -m mercury ashare-tools
python -m mercury --db data/cn-demo.db init --market CN --mode demo --calendar config/cn-calendar-2026.json --start 2026-09-24T10:00:00+08:00
python -m mercury --db data/cn-demo.db register-instrument examples/cn-instrument.json
python -m mercury --db data/cn-demo.db register-session examples/cn-session.json
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1000.json
python -m mercury --db data/cn-demo.db buy 600519.SH 100 --limit 11 --request-id cn-demo-buy-001
python -m mercury --db data/cn-demo.db import-fixture examples/cn-quote-1001.json
python -m mercury --db data/cn-demo.db advance 2026-09-24T10:01:00+08:00
python -m mercury --db data/cn-demo.db match
python -m mercury --db data/cn-demo.db report
python -m mercury --db data/cn-demo.db audit
```

この例の価格と証券情報は**合成データ**であり、記載銘柄の実際の価格ではありません。CN の発注には、銘柄分類と日別の売買停止・リスク警示・上場段階・値幅制限の資料が必要です。資料には銘柄・日付・利用可能時刻を対応付けます。カレンダーは 2026 年、現行ルールの適用期間は **2026-07-06～2026-12-31** です。9 月25～27日は中秋節の休場で、9 月24日に購入した株式は9 月28日に売却可能になります。

T+1 の取得ロット、根拠のある ETF 分類別 T+0、売却可能数量と予約数量、板別の数量・呼値・単一注文上限、適用期間付き値幅制限、手数料・名義書換料・印紙税の内訳に対応しています。CN は連続売買時間帯の指値注文のみを扱い、板寄せや保護価格のない成行注文は対象外です。流動性の証拠がなければストップ高での買い、ストップ安での売りを保守的に拒否します。DAY 注文は期限切れ時に予約を解放し、口座照会やレポートでも失効イベントを追加する場合があります。`audit` は読み取り専用です。

Codex のローカル stdio サーバー `mercury-ashare` を登録済みです。表示には新しいチャットや再読み込みが必要な場合があります。コアは標準ライブラリのみを使い、別途確認した MCP 環境とローカルパスは [A 株セットアップ](docs/ashare-setup.md)（簡体字中国語）に記載しています。起動前にソース一覧とランチャーのハッシュを確認します。

実際の検証では上流の30ツールとMercury拡張1ツールを検出し、9月24日の日足と供給元の時刻を含む実際のスナップショットを取得しました。時刻は9月24日16:11:44（UTC+8）で、25–27日の休場中は古い価格です。通貨が欠ける場合は登録済みCNY銘柄契約が必要です。`ashare-quote`は`mercury_get_realtime_quote`を使用し、元レスポンスから価格・時刻・銘柄を再検証し、拡張のハッシュを記録します。従来のquote/minute/infoの失敗証拠も保存しています。日足は分析・評価専用で、実際のフォワード約定は未検証です。[フィールド出典](docs/ashare-timestamp-source.md)と[独立監査](docs/ashare-audit-review.md)を参照してください。

## クイックスタート

リポジトリのルートで実行します。`pip install` は不要です。

```powershell
python -m mercury demo
python -m unittest discover -s tests -v
python -m mercury --help
```

`demo` は一時的な合成価格データを使い、買い・取消・売り・保有継続・レポート・監査を実演します。終了時には自身が作成した一時口座を削除します。実際の市場データ接続を確認するものではありません。

## ローカル口座

```powershell
python -m mercury --db data/demo.db init --mode demo --start 2026-01-05T15:00:00Z
python -m mercury --db data/demo.db import-fixture examples/quote-100.json
python -m mercury --db data/demo.db buy DEMO 10 --request-id decision-001 --reason "Demo buy"
python -m mercury --db data/demo.db import-fixture examples/quote-101.json
python -m mercury --db data/demo.db advance 2026-01-05T15:01:00Z
python -m mercury --db data/demo.db match
python -m mercury --db data/demo.db hold --reason "Maintain position"
python -m mercury --db data/demo.db portfolio
python -m mercury --db data/demo.db report
python -m mercury --db data/demo.db audit
```

- `buy` / `sell` は注文を登録し、`match` が明示的に約定処理を行います。約定価格は記録済みの価格データ、スリッページ、指値条件から決まります。取引コマンドに約定価格を指定する引数はありません。
- `--limit 99` は買いの上限価格または売りの下限価格です。その価格での約定を強制するものではありません。スリッページ適用後に条件を満たさない注文は未約定のまま残ります。
- `cancel <order_id>` は未約定注文を取り消し、確保していた資金や株数を解放します。約定済み注文は取り消せません。
- `hold` は保有継続の判断と評価額を記録します。既存注文は取り消しません。現金・注文・ポジションは再起動後も保持されます。
- `--request-id` は必須です。同じ判断を再試行する場合は同じ ID を使います。異なる引数で同じ ID を使うとエラーになります。
- データベースごとに 1 つの口座・実験を管理します。初期値は 100,000 USD、手数料 5 bps、スリッページ 2 bps（1 bp = 0.01%）です。`init` で指定でき、作成済み口座の設定は後から変更しません。
- 整数株・買い持ちで、レバレッジはありません。新規口座は部分約定を設定できます。買い注文は現金、売り注文は残り株数を確保します。SQLite が書き込みトランザクションを直列化します。価格変動で利用可能資金を超えた成行注文は拒否されます。
- demo/replay の注文は発注時点より後の価格データを待ちます。live は現在時点で利用可能な最新の MCP 価格データを使用します。有効期間は既定で 300 秒です。古い価格では約定せず、保有銘柄を新鮮な価格で評価できない場合、純資産は `null` になります。
- 金額は Decimal を用いて小数点以下 8 桁で記録します。買い手数料は取得原価に含め、売却時は平均原価を配分します。純リターンには売買双方の手数料が含まれます。
- バックグラウンドループや定期タスクは自動起動しません。エージェントが CLI/API から取得・約定処理・レポートをそれぞれ呼び出します。

## 実際の市場データ → 模擬取引

ローカル環境に `FINANCIAL_DATASETS_API_KEY` を設定してください。Windows では **「環境変数を編集」→ ユーザー環境変数 → 新規** を開き、この名前とキーの値を入力します。`PATH` は変更しません。設定を引き継ぐため、ターミナルや Codex を再起動してください。詳細は[接続設定ガイド](docs/market-setup.md)（簡体字中国語）を参照してください。

コードはキーファイルを読み取らず、コマンドライン引数でキーを受け取らず、`.env` に書き込みません。ソースコード・取得データ・チャット・Git にキーを保存しないでください。

```powershell
python -m mercury mcp-tools
python -m mercury --db data/forward-agent-a.db init --mode live
python -m mercury --db data/forward-agent-a.db collect-quote AAPL --currency USD
python -m mercury --db data/forward-agent-a.db buy AAPL 1 --request-id forward-001 --reason "Your strategy rationale"
python -m mercury --db data/forward-agent-a.db match
python -m mercury --db data/forward-agent-a.db snapshot
python -m mercury --db data/forward-agent-a.db audit
```

`live` は現在時刻と MCP データを使うモードを意味します。**資金も約定もすべてシミュレーションです。** データ取得と取引処理は分離され、MCP クライアントに発注機能はありません。`--currency USD` は対象銘柄の価格通貨を明示的に確認する指定です。プロバイダーのスナップショットには通貨が含まれない場合があるため、暗黙に推測しません。異なる通貨が返された場合は拒否します。

クライアントは[公式ホスト型 MCP](https://docs.financialdatasets.ai/mcp-server) の `/api` を使用します。初期化とツール検出を行った後、`get_stock_price` / `get_stock_prices` を呼び出します。JSON と SSE の応答に対応しています。未対応の応答形式、時刻の欠落、不正な出所情報があるデータは約定に使用しません。実際の権限・利用枠・応答形式は接続して検証する必要があり、モックテストだけでは確認できません。

## 履歴データとローカル分析

```powershell
python -m mercury collect-history AAPL --start 2025-01-01 --end 2025-01-31 --out data/captures/aapl-202501.json
python -m mercury analyze-history AAPL data/captures/aapl-202501.json --asof 2025-02-01T00:00:00Z
python -m mercury --db data/replay-a.db init --mode replay --start 2025-01-01T00:00:00Z --max-age 86400
python -m mercury --db data/replay-a.db load-history AAPL data/captures/aapl-202501.json
```

履歴のインポートは一括トランザクションで行います。1 行でも不正な場合はバッチ全体をロールバックします。インポート後は実際のデータ日付に合わせて `advance` し、観測と判断の後、より後の価格データまで進めて `match` を実行します。日足は翌日の UTC 午前 0 時から利用可能とし、当日の寄り付き時点で終値を参照することはできません。ローカル分析では `asof` までに利用可能な行だけを使い、価格リターン、ドローダウン、20 期間移動平均、標本リターンの変動を計算します。

`collect-history` が保存するのは 1 回のツール応答です。**市場全体のダウンローダーではありません。** 応答に `next_page_url: null` が明示されている場合に限り、ページ取得の完了と判定します。ページ情報がなければ不明として扱います。次のページがある場合は別途取得するか、要求する期間を短くしてください。1 回の応答で 5～10 年分のデータセットが揃ったとは判断できません。[評価方針](docs/evaluation.md)（簡体字中国語）も参照してください。

## 監査と制約

`audit` は台帳を変更せず、イベントのハッシュ、価格と出所の対応、注文状態、手数料・スリッページ・指値を確認します。現金・ポジション・取得原価・記録済み評価額も独立に再計算します。終了コードが 0 以外の場合は検証失敗またはエラーです。これはローカルの整合性検証であり、別のサブエージェントによるレビューは [AGENTS.md](AGENTS.md)（簡体字中国語）に従って実施します。Codex Auto Review を代替するものではありません。

SHA-256 はローカルの内容の整合性を確認するもので、プロバイダーの署名ではありません。同一プロセスの Python 非公開メソッドやローカルデータベースは、プロセスやファイルへアクセスできる悪意あるエージェントを隔離しません。厳密な評価には独立した実行環境、読み取り専用の観測インターフェース、エージェントが書き換えられない外部の証拠が必要です。

未実装の機能：米国取引所カレンダー・閉場時の制約と板寄せ機構、実測した板の厚み・注文待ち行列の再現、マーケットインパクト、株式分割・配当、上場廃止と生存者バイアスの補正、匿名化ベンチマーク実行環境、大規模な履歴データセット、長期フォワード評価。現段階は検証可能な模擬取引基盤であり、投資成績やモデル能力を立証する完成済みベンチマークではありません。

## プロジェクト構成

| パス | 用途 |
| --- | --- |
| `mercury/` | 取引エンジン、市場データ接続、分析、監査、JSON CLI |
| `tests/` | ローカル回帰テスト |
| `examples/` | 合成データであることを明記した価格サンプル |
| `docs/` | 接続設定、評価方針、独立監査報告（簡体字中国語） |
| `scripts/` | 環境変数を使う Windows 接続確認スクリプト |
| `data/` | 非公開の実験データベースと取得応答。Git の追跡対象外 |
| `.github/workflows/checks.yml` | Python 3.11 / 3.13 でのローカルテストとデモ |

口座や取得応答を新規作成する際、既存ファイルの上書きは拒否します。コミットやプッシュは自動実行しません。

## 開発

変更を提案する前にクイックスタートの検証コマンドを実行し、[AGENTS.md](AGENTS.md) を確認してください。機能ブランチとプルリクエストを使い、既存のデータや変更を保持します。約定処理・手数料・データ取り込み・評価に実質的な変更がある場合は、独立したサブエージェントによるレビューが必要です。3 言語の README を同時に更新してください。ローカルテストの成功は、リモート CI や実際の MCP 接続の成功を意味しません。

## ライセンス

[Apache License 2.0](LICENSE)。
