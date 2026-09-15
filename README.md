# PCパーツ・周辺機器 セール監視

楽天を除く10店の特価候補を収集し、履歴・比較判定・変更イベントをJSONへ保存します。GitHub Actionsで4時間ごとに店舗別ジョブを実行し、既存のChatGPTタスクには結果の説明と疑義確認を渡す構成です。

**導入モードは並行検証です。7日間の測定と検証が終わるまで、ChatGPTの通知を新しいJSONへ切り替えません。** コードのテスト合格と、10店の実運用での取得成功は別の状態として報告します。

## 対象と収集範囲

| 対象 | 実装 | 不明時の扱い |
|---|---|---|
| ドスパラ | 既存コードの商品API・クーポン・残数・商品ページ照合を再利用。キャンペーン一覧と商品を個別にキュー化 | 残数やクーポン照合失敗は候補保留 |
| ark | 登録セール一覧のページ送り、JSON-LDと商品仕様表 | 送料等が欠ける場合は判定保留 |
| ツクモ・Sofmap・Joshin・ビック | 店舗別URLパターン、セール一覧、商品JSON-LDと専用セレクタ | 403・タイムアウト・項目不足を個別に記録 |
| 工房通販・ヨドバシ | HTML解析、必要時のPlaywright表示取得 | ブラウザでも未取得なら保留 |
| Yahoo!ショッピング | 公式商品検索v3。一般向け価格・基本ストアポイント・販売者・在庫・送料区分 | Client ID未設定は `configuration_needed` |
| Amazon | ちもろぐで候補発見、Amazon商品ページの購入欄を別途確認 | 販売元・出荷元・状態・送料・価格が揃わなければ保留 |
| 工房共通チラシ | 共通公開ページを1ジョブで監視。画像/PDFのハッシュごとに抽出結果を保存 | OCRの疑義、店舗在庫は確認待ち |

通常商品を全件巡回しません。登録したセール一覧とそこから明示的にリンクされたセールページを対象にし、ページ送りは末尾まで追います。比較は既に発見した候補のJANまたは完全な表示型番で検索し、次回収集キューに保存します。初回は比較店不足が多くなります。取得範囲は `config/sources.json` で管理します。

楽天関連ホストへの新規HTTP取得・リダイレクト・ブラウザの通信を拒否します。旧楽天履歴が残っていても現在比較とB判定から除外します。巡回完了率の分母は常に10です。

## 実行

Python 3.12を使用します。

```sh
python -m venv .venv
# 作成した仮想環境のPythonを使って、以下を実行
python -m pip install -e .
python -m playwright install chromium
python -m unittest discover -s tests -v
python -m sale_monitor.cli collect --store dospara --run-id trial-001
python -m sale_monitor.cli collect --store ark --run-id trial-001
python -m sale_monitor.cli aggregate --run-id trial-001
```

同一巡回の店舗には同じ `--run-id` を指定します。`--seconds` は実行環境の時間制限に合わせた中断時刻で、Web呼出数の上限ではありません。キュー・商品観測・履歴への受け渡しを商品ごとに原子的に保存し、次回は未完了位置から再開します。現在比較には今回の `observed_run_id` を持つ価格だけを使います。取得障害は在庫切れに変換しません。同じ巡回で失敗したページは、関連する比較候補ごとに再取得せず、未処理として次回へ繰り越します。キャッシュは正本として使用しません。

サイトから `Retry-After` が返った場合、その待機期限を同一ホストの全URLとブラウザー取得で共有します。長い待機はデータブランチの `retry_after` にUTCのUnix秒で保存し、再開時も期限前にアクセスしません。最新索引の `retry_after_epoch_seconds` で期限を確認できます。他の店舗のジョブは独立して継続します。

Yahoo!はGitHubリポジトリの **Settings → Secrets and variables → Actions** に `YAHOO_CLIENT_ID` を設定すると有効になります。APIキーをコード、公開JSON、ChatGPTの会話へ記載する必要はありません。現時点では未取得として扱います。

## 永続化と通知用ファイル

`.github/workflows/monitor.yml` はUTC `17 */4 * * *` に実行します。日本時間では原則1:17、5:17、9:17、13:17、17:17、21:17です。スケジュール実行の遅延を前提とし、確認日時とジョブ未実行を必ず検査します。

`main` はコード、`monitor-data` はデータです。収集は10個の独立ジョブ、チラシは工房ジョブ1個、集計・データブランチ更新は1個のジョブが担当します。通常のfast-forward pushのみを使用し、強制上書きは行いません。過去データの取得失敗時に空の履歴で初期化しません。

| ファイル | 内容 |
|---|---|
| `state/stores/<store>.json` | 再開位置、未処理、取得結果、履歴へ渡す観測記録 |
| `state/history/<store>/<date>.json` | 観測IDで重複排除した価格・条件の履歴 |
| `state/events/registry.json` | 固定イベントID、直前状態、公開済みと配信済みの区別 |
| `state/flyers/assets/<hash>.json` | 一度だけ抽出したチラシ内容と読み取り候補 |
| `state/flyers/editions/<hash>.json` | チラシ版、対象店舗、開催期間・確認状態 |
| `state/requests/<store>.json` | 特価候補だけを対象とする次回の比較検索 |
| `state/metrics/<run>.json` | 巡回範囲・必須項目取得率・未処理滞留の測定 |
| `public/latest.json` | 軽量な最新索引、10店の取得状況 |
| `public/notifications.json` | 現在も根拠が有効な通知候補と固定ID |
| `public/review_queue.json` | 判定できない候補と不足理由、チラシ確認先 |
| `public/flyer_review.json` | 共通チラシの画像URL、版、OCR文字列・商品候補、掲載数量の適用範囲 |
| `public/evidence.json` | 商品単位のA/B計算と比較証拠 |
| `public/validation.json` | 並行検証の期間・取得率・切替可否 |

公開中の読取URLは `https://raw.githubusercontent.com/chijimi33/pc-sale-monitor/monitor-data/public/latest.json` です。ChatGPTへ全店再巡回を要求せず、検証後に `docs/chatgpt-task-prompt.md` の切替用プロンプトで通知します。

イベントの公開は配信確認ではありません。初期構成は厳密な一度だけの配信を保証しません。明示的な配信確認を受けた場合だけ `ack --event-id ...` で記録できます。古いイベントを再提示するときも `current_evidence` の現在条件を使用します。

## A/B判定

税込支払額（本体＋送料−確認済み割引）と、確定ポイント控除後の実質額を別々に判定します。未知の送料・ポイントは0円に補完しません。

- A：独立した販売者2者以上との比較で、最安比較先より10%以上かつ500円以上安い。
- B：今年の観測済み最低価格を500円以上更新、または30〜90日の観測で3日以上の日別最低価格の中央値より10%以上かつ500円以上安く、同期間最低価格以下。いずれも現在の比較先が最低1者必要。
- 現在もっと安い比較先が確認できた候補は採用しない。
- 型番・JAN・状態・セット内容・既知の保証条件が合わない比較は除外する。片方だけ構成が確認できた場合も、2枚組と単品などを誤比較しないよう保留する。
- 数値差不足、比較店不足、履歴不足を別々に残す。履歴範囲外を含めた「年間最安」とは呼ばない。

販売者の重複は `seller_id` で除外します。モール販売者と直販の同一性が未確認の場合は `seller_identity_review_needed` とし、確認後に `config/sources.json` の `seller_aliases` へ対応を登録します。6店舗の共通チラシは販売者 `koubou` 1者です。

## 共通チラシ

枚方・大阪日本橋・東大阪・なんばアウトレット別館・堺・岸和田へ同じ版を適用します。店舗別に画像を取得・OCRしません。共通掲載数量は `listed_quantity`、店舗独自の証拠は `branch_overrides` に分離します。通販と店舗は別販売条件です。

OCRはTesseractの日本語・英語モデルを使用し、Actionsでインストールします。PDFの文字層があれば直接抽出します。読取疑義は `review_queue` に残し、確認した内容だけを `config/flyer_reviews/<edition>.json` へ登録します。例は `docs/flyer-review.example.json` です。版だけでなく確認した画像のハッシュを商品ごとに記録し、一部の画像だけの確認は `partially_reviewed` と表示します。ハッシュが変わると古い確認内容は適用されません。分割払いの月額や手数料は商品価格候補にしません。

店舗在庫が未確認の場合は「店舗在庫要確認」です。掲載数量だけでは在庫ありと判定しません。店舗別価格・独自条件の差異がある場合、共通価格をその店舗へ誤適用しないよう、該当店舗を除外して別途確認します。

## 7日間の並行検証

`python -m sale_monitor.cli validate` で現状を確認します。切替は自動実行しません。まず収集範囲、不足項目、滞留、誤判定を点検し、以下の初期ゲートを満たした後に既存ChatGPTタスクを切り替えます。

1. 公開後7日以上、直近7日に40個以上の異なる4時間枠で測定がある。同じ枠の再実行は最新1回だけを取得率の集計に使う。
2. 10店の巡回が完了し、必須項目の取得率95%以上、24時間を超える未処理がない。
3. 最低20件の人またはChatGPTによる証拠照合で誤通知0件を確認し、`state/validation/manual_review.json` に `reviewed_at`、`reviewed_count`、`false_positive_count` を記録する。
4. JSONの取得と既存タスクでの読取を確認する。

40枠・95%・20件は初期運用の検証ゲートです。A/Bの数値基準を変更するものではありません。Yahoo!未設定やサイト側取得障害が残る間は切替不可として原因を表示します。Amazonの無料取得率もここで測定し、必要性が判明してからKeepa APIを比較します。有料契約は作成していません。

## 参照

初期の取得状況と残課題は [導入状況](docs/deployment-status.md) を参照してください。最新の状況は常に公開JSONを優先します。

- [Yahoo!商品検索v3](https://developer.yahoo.co.jp/webapi/shopping/v3/itemsearch.html)：送料区分、一般ストアポイントとプレミアムポイントの分離、検索結果ウィンドウ。
- [Playwright Python](https://playwright.dev/python/docs/intro)：表示後のHTML取得。
- [GitHub Actionsのschedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)：実行時刻と制約。
- [ドスパラ既存ツール](https://github.com/chijimi33/Dospara-coupon-price-tool)、[ちもろぐ既存ツール](https://github.com/chijimi33/Chimolog-price-tool)。
