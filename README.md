# PCパーツ・周辺機器 セール監視

日本のPCパーツ販売店のセール候補を収集し、他店の現在価格と価格履歴に照らして、本当に安いと判定できる商品だけを根拠付きで通知する仕組みです。取得できない情報を推測で補わず、価格・送料・販売者・在庫などの確認結果と不足理由をJSONに残します。

現在は**並行検証**です。2026-11-11 の再設計の正は [設計仕様](docs/architecture.md) です。正式切替の条件は未達で、新設計の取得率・通知件数・安定性は未実証です。

## 対象10店と取得経路

以下は [config/routes.json](config/routes.json) の初期状態です。経路は販売者×チャネルを表し、状態は機能ごとに持ちます。「発見」は `sale_discovery`、「商品確認」は `product_verify`、「比較探索」は `comparison_lookup`、「全3機能」はその3つです。active は実運用の成功を保証する表示ではありません。

| 店 | 経路 | 初期状態 | 理由・内訳 |
| --- | --- | --- | --- |
| ドスパラ | `dospara:direct`（直販） | 発見・商品確認は `active`、比較探索は `not_implemented` | 比較探索は未実装です。稼働機能に停止理由の設定はありません。 |
| ark | `ark:direct`（直販） | 全3機能 `active` | 停止理由の設定はありません。 |
| パソコン工房 | `koubou:direct`（直販） | 全3機能 `active` | 停止理由の設定はありません。 |
| ソフマップ | `sofmap:direct`（直販） | 全3機能 `active` | 停止理由の設定はありません。 |
| ツクモ | `tsukumo:direct`（直販） | 全3機能 `paused_maintenance` | ネットショップ全体がメンテナンス表示で停止中です。 |
| Joshin | `joshin:direct`（直販） | 全3機能 `paused_refused` | 自動取得の明示拒否の記録があります。 |
| ビックカメラ | `bic:direct`（直販） | 全3機能 `paused_unreachable` | 無応答で、原因は未確定です。 |
| ヨドバシ | `yodobashi:direct`（直販） | 全3機能 `paused_unreachable` | 無応答で、原因は未確定です。 |
| Yahoo!ショッピング | `yahoo:api`（API） | 発見・比較探索は `paused_configuration` | `YAHOO_CLIENT_ID` の設定待ちです。商品確認機能の定義はありません。 |
| Amazon | `amazon:direct`（商品ページ）／`amazon:chimolog`（ちもろぐ） | 商品ページの商品確認は `paused_refused`、ちもろぐの発見は `paused_configuration` | 商品ページがアクセス確認画面になります。転記価格だけでは購入条件を確認できないため、商品確認経路が使えるまで発見も停止します。 |

状態の正は実行時の `state/routes.json` と公開 `latest.json` の `routes` です。上表は外部サイトを再確認した結果ではありません。

| 状態 | 意味・疎通確認の規則 |
| --- | --- |
| `active` | 通常収集の対象です。 |
| `paused_maintenance` | サイト停止中です。1日1回・1リクエストで、通常ページとして解析できることを連続2回確認して復帰します。 |
| `paused_refused` | 明示拒否です。自動疎通確認はせず、手動 `probe` のみとします。復帰には人による設定変更が必要です。 |
| `paused_unreachable` | 無応答・原因未確定です。週1回・1リクエストで確認し、明示拒否なら `paused_refused` にします。 |
| `paused_configuration` | 設定待ちです。疎通確認はしません。 |
| `not_implemented` | その機能は未実装です。 |

active でない機能には通常収集の通信もタスク作成もしません。疎通確認は別記録とし、Retry-After を守ります。トップページの成功だけで別機能を復旧扱いにせず、実行場所を変えて拒否を解除しません。人による設定変更では `config_revision` を上げます。

楽天は取得・比較・履歴更新から除外し、過去の楽天価格も現在比較やB判定に使いません。取得完了率の分母は表示上10店のままです。

## 比較範囲と通知の表示

scope v1 は **ドスパラ・ark・パソコン工房・ソフマップの直販**です。`config/routes.json` の `scope.in_scope` で価格を見る前に固定し、実行途中の障害を理由に縮めません。変更時は `scope.version` を上げて公開JSONに記録します。

A/Bの数値基準は不変ですが、比較範囲を「10店すべて」から「事前に固定した販売経路」へ変えた仕様変更です。同一runで確認した有効な比較価格は、安いものも含めてすべて評価します。

判定と通知には「A・比較範囲 v1」のように範囲を付け、「確認した販売者・チャネル」と「未確認の販売経路と理由」を必ず示します。範囲外・停止中・予算不足・取得失敗・条件不明を区別し、「10店で最安」とは表現しません。判定ごとの `comparison_scope.label`、`confirmed`、`unconfirmed` を使います。

## A/B判定

税込支払額（本体＋送料−確認済み割引）と、確定ポイント控除後の実質額を別々に判定します。未知の送料・ポイントを0円に補完しません。

- A：独立した販売者2者以上との比較で、最安比較先より10%以上かつ500円以上安いことが必要です。
- B：今年の観測済み最低価格を500円以上更新するか、直近90日以内で最古の観測から30日以上・3観測日以上ある履歴の日別最低価格の中央値より10%以上かつ500円以上安く、同期間最低価格以下であることが必要です。どちらも現在の比較先が最低1者必要です。30日・3観測日の条件は中央値によるBだけに適用します。
- 現在もっと安い比較先が確認できた候補は採用しません。
- **比較範囲内の既知比較先**が今回未取得、または送料・ポイント等が未確認なら、残った比較先だけで成立したA/Bを `known_comparator_not_verified_this_run` として保留します。過去価格を現在価格に使わず、URLと最終確認日時を示します。今回の明示的な売切れ・期限切れ等で販売終了を確認できれば不足を解消できますが、経過日数だけでは解除しません。範囲外の既知比較先は保留理由にしません。
- 型番・JAN・状態・セット内容・既知の保証条件が合わない比較を除外します。片方だけ構成が確認できた場合も、2枚組と単品などを誤比較しないよう保留します。
- 数値差不足、比較先不足、履歴不足を区別します。履歴範囲外を含めた「年間最安」とは呼びません。

販売者は `seller_id` で重複を除きます。モール販売者と直販の同一性が未確認なら `seller_identity_review_needed` とし、確認後に `config/sources.json` の `seller_aliases` へ対応を登録します。工房6店舗の共通チラシは販売者 `koubou` 1者です。

比較で見つけた商品（`discovery_kind="comparison"`）も、今回のセール候補と同一商品で全検証を通れば通知候補になります。発見経路は出典として残し、イベントIDは従来どおり `offer.key` から作ります。

## 実行の流れと通信の規則

新設計はGitHub Actionsで、1ジョブ・1回の `run` 内に次を完結させます。比較依頼は永続キューにせず毎回計画し直し、検索進捗・商品識別子からURLへの索引・確認履歴は保存します。

| 段階 | 内容 | 通信 |
| --- | --- | --- |
| 0 準備 | 設定・状態スキーマ・scope版の確認と、期限の来た疎通確認です。 | 疎通確認のみ |
| A 発見 | 発見機能がactiveの店でセール一覧・セール商品を取得します。 | あり |
| B 計画 | 候補を選び、範囲内の既知比較先の確認一式を予算から予約します。 | なし |
| C 確認 | 計画した商品ページを確認し、上限付きで比較先を探索します。 | あり |
| D 判定・公開 | A/B判定、イベント作成、履歴反映、公開JSON生成を行います。 | なし |

調整役とホスト別子プロセスで実行し、A・Cは店舗別に並行処理する設計です。子と段階に期限を設け、期限切れはブラウザーを含む子孫を停止して次へ進み、その店は未確認として残します。初期設定は全体90分、発見40分、確認30分、探索割合0.2です。現在比較には同じrunの観測だけを使います。

ホスト別の最小間隔は設定値とrobots.txtのCrawl-delayの大きい方です。段階の時間予算から上限 `floor(秒数 / 間隔)` を求め、一覧・商品・検索・送料規定・robots.txt・再試行・チラシ・ブラウザー遷移を同じ予算に数えます。再開で予算は復活しません。同一リクエストの成功応答は共有しますが、HTTPメソッド・本文・取得方式が異なるものを統合しません。

robots.txtはrun・ホストごとに1回取得し、Disallowの対象にはアクセスしません。401/403以外の4xxは制限なし、5xx・タイムアウト・401/403はそのrunのホスト取得を停止します。403やアクセス確認画面などの明示拒否後は同じrunでブラウザーへ進みません。ボット検知の回避は実装しません。

段階Dが失敗しても収集済み状態を保存する設計です。書き手は常に1つとします。新設計による一連の実行・保存・公開は未実証です。

## 実行方法

Python 3.12以上を使います。仮想環境を作り、そのPythonで実行します。

```sh
python -m venv .venv
# 仮想環境を有効化してから実行します。
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests -v
```

以下は**新CLIの実装反映後に使う手順**です。この作業ツリーでは `pipeline.py` とCLIの `run`・`stage`・`migrate`・`probe` はまだ未実装で、下記コマンドの実行成功は未実証です。経路・予算・移行・計画・履歴評価・scope付き集計のモジュールはあります。段階別実行の仕様は設計書3章を参照してください。

```sh
python -m sale_monitor.cli run --state <dir> --public <dir> --run-id <id>
python -m sale_monitor.cli migrate --state <dir> --input-sha <最終データSHA>
python -m sale_monitor.cli migrate --state <dir> --revert
python -m sale_monitor.cli probe --route <route_id>
```

`<dir>` は状態保存先と公開先をそれぞれ指定します。移行前には旧方式の自動改修・状態更新を停止し、実行中・待機中の書き手がないことを確認します。最終データSHAと復元地点を固定し、移行後に履歴・観測・未反映journal・イベントID・配信状態の保持を確認します。停止経路の旧タスクと廃止した比較依頼は削除せず `state/archive/` へ退避します。撤回時は戻すコードと状態の組も明記します。移行完了は正式切替と別に記録します。

旧 `collect` / `aggregate` は互換のため残っていますが、新設計の定期実行では使いません。このツリーの `.github/workflows/monitor.yml` はまだ旧店舗別ジョブ構成で、新設計へ反映済みとは扱いません。

### 実装上の注記

- ドスパラは見出しのメーカー区切りから完全な商品表記を保持し、明示型番を優先します。末尾・色・容量・限定モデル表記を削らず、曖昧な区切りや関連商品から型番を推測しません。
- arkは対象商品価格欄と構造化データの期限が矛盾すれば、双方の証拠を残して `expiry_conflict_review_needed` として保留します。関連商品の期限や年のない表示から推測しません。セール一覧の旧入口 `/t/c/760/` は `list_url_replacements` で `/search/?onsale=1` へ置換します。比較検索や商品URLには適用しません。
- 工房の購入欄で会員価格を確認できない場合は通常価格と区別して保留し、比較先としての不足も隠しません。
- ソフマップは検索条件・読み込み先と商品ページを別々に照合します。空HTMLや不明なフィルターを正常な空結果と扱いません。
- ツクモ検索の404は、検索語・タイトル・専用の該当なし表示が一致する場合だけ正常な空結果とする実装が残っています。停止中の経路で取得を試す理由にはなりません。

## 状態ファイルと公開JSON

`state/` は `--state`、`public/` は `--public` の指定先です。コードはmain、データはmonitor-dataブランチに置く構成です。以下には新設計の保存契約も含み、全ファイルが実運用で生成済みという意味ではありません。

| ファイル | 内容 |
| --- | --- |
| `state/routes.json` | 経路×機能の状態、根拠、疎通確認記録、設定版です。 |
| `state/archive/<store>.json` | 退避した旧キューです。元タスクID・作成日時・試行回数、退避理由・移行版・入力SHAを保持します。 |
| `state/plans/<run_id>.json` | 今回の候補、確認予約、探索、見送り理由です。 |
| `state/comparison_tracking.json` | 商品識別子×店の確認試行・成功日時と連続失敗数です。 |
| `state/schema.json` | 状態スキーマ版と適用済み移行です。 |
| `state/stores/<store>.json` | 取得結果、探索進捗、未処理、履歴反映前の観測journalです。比較依頼の永続的な債務には使いません。 |
| `state/history/<store>/<date>.json` | 観測IDで重複排除した価格・条件の履歴です。 |
| `state/events/registry.json` | 固定イベントID、直前状態、公開と配信の状態です。 |
| `state/flyers/assets/<hash>.json` | チラシの抽出結果と読み取り候補です。 |
| `state/flyers/editions/<hash>.json` | チラシ版、対象店舗、開催期間、確認状態です。 |
| `state/flyers/reviews/<edition>.json` | 画像ハッシュに対応する確認済み内容です。 |
| `state/metrics/<run>.json` | 実行ごとの公開索引と測定値です。 |
| `state/validation/run_provenance.json` | 実行イベント・日時・試行番号の確認記録です。 |
| `state/validation/manual_review.json` | 正式監査の個別証拠と集計です。 |
| `public/latest.json` | 最新索引、10店の状況、完了率、公開ファイル参照です。scope付き集計はスキーマ版2になります。 |
| `public/notifications.json` | 固定event_idと現在も有効な根拠を持つ通知候補です。 |
| `public/review_queue.json` | 保留候補、不足理由、チラシ確認先です。 |
| `public/flyer_review.json` | 元画像URL、版、抽出文字列・候補、掲載数量の適用範囲です。 |
| `public/evidence.json` | 商品単位の支払額・ポイント別A/B判定と比較証拠です。各判定レコードに `comparison_scope` が付きます。 |
| `public/collection_errors.json` | URL別の取得失敗、探索漏れ、通信証拠です。 |
| `public/comparison_plan.json` | 今回の計画の要約です。候補数、状態別件数、予約リクエスト数を読みます。計画がない場合は `no_plan_for_run` です。 |
| `public/comparison_routing.json` | 互換用の旧比較依頼・確認記録です。新計画の達成証明には使いません。 |
| `public/validation.json` | 並行検証・監査・ベースラインと切替可否です。 |

`reporting.aggregate(..., scope=...)` が `latest.json` へ追加するキーは以下です。

| キー | 意味 |
| --- | --- |
| `comparison_scope` | 固定範囲の版、範囲内経路、範囲外経路と理由です。 |
| `routes` | 経路の状態・根拠などです。機能別状態を確認します。 |
| `route_counts` | 店舗単位の `active_direct_stores` と `paused_stores` です。 |
| `run_outcome` | `successful`、理由、今回観測数、今回結果のある店、確認済み空結果数です。 |
| `active_store_count` | 停止扱いの店を除いた店舗数です。 |
| `active_complete_stores` | そのうち取得完了した店舗数です。 |

判定ごとの `comparison_scope` は `version`・`label`・`confirmed`・`unconfirmed` を持ちます。通知はイベントの `current_evidence` にある現在の判定を使います。公開は配信確認ではなく、厳密な一度だけの配信保証はありません。明示的な配信確認がある場合だけ既存の `ack --event-id ...` で記録します。

公開先として使う索引URLは [latest.json](https://raw.githubusercontent.com/chijimi33/pc-sale-monitor/monitor-data/public/latest.json) です。この文書更新では公開先の稼働状態を確認していません。

## 共通チラシ

枚方・大阪日本橋・東大阪・なんばアウトレット別館・堺・岸和田へ同じ版を適用し、画像・PDFのハッシュごとに一度抽出します。店舗別に画像取得・OCRをせず、共通掲載数量 `listed_quantity` と店舗独自の証拠 `branch_overrides` を分離します。通販と店舗は別の販売条件です。

OCRはTesseractの日本語・英語モデルを使い、Actionsでインストールします。PDFに文字層があれば直接抽出します。疑義は `review_queue` に残し、確認内容はデータブランチの `state/flyers/reviews/<edition>.json` へ登録します。同じ版の記録がない場合は `config/flyer_reviews/<edition>.json` を参照します。記入例は [flyer-review.example.json](docs/flyer-review.example.json) です。

商品ごとに画像ハッシュを記録し、一部画像だけの確認は `partially_reviewed` と表示します。ハッシュ変更後は古い確認を適用しません。分割払いの月額・手数料を商品価格候補にしません。容量・Officeセット等が違う商品は `variant` を指定し、商品IDに価格を含めないことで同条件の価格変更を履歴として残します。

BTO構成例は `price_basis: configuration_example` とし、掲載額を `printed_configuration_price_yen` に保存して確定価格はnullにします。画像確認済みでも完全型番や店舗在庫が不明なら理由を残します。掲載数量を店舗在庫へ変換せず、未確認は「店舗在庫要確認」です。店舗別価格・独自条件が違う場合は、その店舗を共通条件から除外して別途確認します。

## 並行検証と正式切替の条件

設計書8章に従い、新設計で7日間のベースラインを取り、公開成功の最大間隔、発見範囲の完了、選抜候補の一式確認達成率、範囲内経路の稼働率、通知時点の観測鮮度を測定します。サービス水準を決め、それを満たすまで並行検証を続けます。正式切替には、さらに次が必要です。

1. 正式監査20件以上、誤通知0、未解決0を確認します。有効化した新機能を経た通知も監査対象に含めます。
2. 通知側が古いJSONや期限切れを「該当なし」と誤認しないことを実確認します。
3. 通知文に比較範囲と未確認経路が出ることを実確認します。

正常な取得・解析と成果物の整合が必要です。確認済みの正常なゼロ件は成功ですが、古い観測の再保存や疎通確認だけでは成功に数えません。現在の `run_outcome.successful` は今回の観測または確認済み空結果を数える実装で、これだけで成果物全体の整合や正式切替条件を証明できません。

**旧ゲートの「10店すべて完了」「全店95%」「24時間超の未処理0」「40枠」は廃止しました。** 監査20件・誤通知0・未解決0は維持します。scope付き `validation.json` にも旧診断の `reasons` 等が残る実装ですが、旧ゲートを復活させる根拠にしません。新方式は `gate_model="scope_v1"`、`baseline`、`cutover_blockers` を確認します。現実装は `service_level_undetermined` を阻害理由として `cutover_ready=false` を返し、サービス水準決定後の成立は未実証です。

Qwenの定期保守は無効を維持する方針です。旧設計の改修を続けるCodexの1日2回の自動レビューは停止方針とし、再開用の控えは [監督専用プロンプト](docs/codex-qa-supervisor-prompt.md) です。この文書更新はタスク停止の実操作や保存確認を行ったという意味ではありません。

## Yahooと送料の後段対応

Yahooは任意・後段です。Actionsの `YAHOO_CLIENT_ID` を設定しただけでは比較範囲に入りません。JAN専用検索と応答の一致、販売者対応表、確定できる送料区分、一般利用者に無条件で付くポイント、ページ送り上限の検証が必要です。LYP固有還元は除外し、Yahooチャネルの価格を直販価格の確認と扱いません。秘密情報をコード・公開JSON・会話へ記載しません。

送料規定領域のハッシュとレビュー済み版の一致に基づく適用、および利用者が申告する配送プロファイルは後段・未実装です。商品区分とすべての例外条件を確認できる場合だけ適用し、不明な地域条件や未設定プロファイルを推測しません。既存の店舗別送料解析を新しい共通方式の実装済み証拠とは扱いません。

## 参照

仕様の正は [設計仕様](docs/architecture.md)、通知切替用の控えは [ChatGPTタスクプロンプト](docs/chatgpt-task-prompt.md) です。[Qwen検証の運用](docs/qwen-qa.md) には旧運用の記述も残るため、再設計と矛盾する切替条件には適用しません。[導入状況](docs/deployment-status.md) は2026年9月の歴史的記録です。現在の状況は公開JSONで確認します。

- [Yahoo!商品検索v3](https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html)：送料区分、一般ストアポイントとプレミアムポイントの分離、検索結果ウィンドウです。
- [Playwright Python](https://playwright.dev/python/docs/intro)：表示後のHTML取得です。
- [GitHub Actionsのschedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)：実行時刻と制約です。
- [GitHub Actionsの同時実行制御](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)：実行の重複を制御します。
- [ドスパラ既存ツール](https://github.com/chijimi33/Dospara-coupon-price-tool)、[ちもろぐ既存ツール](https://github.com/chijimi33/Chimolog-price-tool)。
- [ツクモ公式送料案内](https://shop.tsukumo.co.jp/shopping-help/service/souryo.html)：既存の店舗別解析の参照先です。経路停止中の取得はしません。
