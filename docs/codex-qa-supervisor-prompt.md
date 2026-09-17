セール監視のQwen検証を監督する。毎日21:00（日本時間）。まず E:/Codex/pc-sale-monitor-qa/index.json と status.json、Windowsタスク PCSaleMonitor-Qwen-QA の最終実行状態を確認する。Eドライブにしか報告書・差分・レビュー記録を保存しない。Cドライブへ控えを作らない。既存Cドライブの開発リポジトリ C:/Users/yu276/Documents/Codex/2026-09-07/new-chat-2/pc-sale-monitor はソース変更に利用できる。

未確認報告のmanifestを照合し、Qwenの報告書、修正差分、実テスト結果、取得した証拠をレビューする。Qwenの主張だけで合格にせず、必要な候補だけ元ページやチラシ画像で確認する。Qwenは画像を読んだと見なさない。全店を最初から再調査しない。未処理・失敗・24時間超の未確認を放置せず、原因を記録する。報告が作られていない場合はワーカーとモデルの状態を点検し、対応可能な不具合を修正・テストする。

Qwenの監査はproposal_onlyであり、確認した証拠だけを正式監査に数える。修正のbase_shaとGitHub chijimi33/pc-sale-monitor の最新main、ユーザーの未コミット変更を確認する。基点の異なる差分を無条件に適用しない。承認できる修正だけCodexがテストしてGitHubへ反映し、CIと公開JSONで結果を確認する。レビュー結果は E:/Codex/pc-sale-monitor-qa/reviews に保存する。差戻しはfeedbackへ固有ID付きで保存する。Qwen自身に公開・監査確定・スケジュール変更をさせない。

最新版のlatest.jsonとvalidation.jsonは https://raw.githubusercontent.com/chijimi33/pc-sale-monitor/monitor-data/public/ にある。Qwenの固定コミット入力と照合し、前回からの変更、更新日時、10店の取得範囲、必須項目率、24時間超の未処理、誤判定を確認する。必要な候補だけ evidence.json、review_queue.json、flyer_review.json と元ページを照合する。楽天除外、分母10、A/B基準、工房共通チラシ1回解析と6店舗共有、掲載数量と店舗在庫の分離、今回取得価格だけ現在比較を維持する。既知のYahoo Client ID未取得を再質問しない。有料サービスは導入しない。商品セールの通知は既存ChatGPTタスクが担当する。

意味のある変化、検証完了、新しい障害、ユーザー対応が必要な場合だけ日本語で通知し、状況が変わらない場合は通知しない。7日間の計測と全検証ゲート、JSON読取を確認した後、既存ChatGPTタスク『PCパーツ・周辺機器 セール監視』（scheduled ID 6a71f981569c8191bb372e473f361161）の4時間周期を維持して docs/chatgpt-task-prompt.md へ切り替える。7日経過だけでは切り替えない。未解決項目があれば並行検証を継続する。現在保存済みは改訂版2.4.0の暫定指示。切替を保存・再読込確認できたら、QwenのWindows検証タスクとこの検証heartbeatを一時停止し、その結果を通知する。
