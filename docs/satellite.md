# 自宅 PC からの収集（satellite）— 設計仕様 v1

2026-11-13 に合意。`docs/architecture.md` の補遺で、矛盾する場合はこの文書の記述を、その章について正とする
（3章「書き手は1つ」、5章 scope、1章 route の拡張）。

## 0. 背景と決定

- ツクモ（`tsukumo:direct`）は 2026-11-13 に復旧したが、GitHub Actions からの接続は応答なしで切断される
  （自宅の家庭回線からは取得できる）。明示的な拒否（403、確認画面）は観測していない。切断が意図的な遮断かは確認できていない。
- 所有者の決定:
  1. 所有者の PC からツクモだけ収集する。名乗り（User-Agent）・robots.txt の順守は Actions と同じ。
  2. ツクモは必須の比較範囲に入れない。PC が止まっていても通知を保留にしない（取れていれば比較に使う）。
  3. ボット検知の回避（自動操作の隠蔽、指紋偽装、CAPTCHA 突破、プロキシの切替・中継）は実装しない。
     明示的な拒否を受けたら、その時点で止める。
- 退けた構成: 公開リポジトリに家庭用 PC の self-hosted runner を置く（一般に危険）／パイプライン全体を PC に移す
  （4店の収集と公開が PC の電源に依存する）／PC が独自周期で集めて「新しければ」比較に使う（同一 run 内の比較という
  不変条件を崩す）／Actions の通信を PC で中継する（決定3に反する）。

## 1. 構成

- route に `executor` を持たせる（既定 `"runner"`＝調整役と同じ場所、`"satellite"`＝所有者の PC）。route 単位で、
  機能ごとには分けない。`tsukumo:direct` は `executor: "satellite"`、`sale_discovery`・`product_verify` が `active`、
  `comparison_lookup` は `paused_configuration`（v1 では PC から検索しない）。
- 収集者は固定登録する。home ごとにランダムな `agent_id`（小文字16進32文字）を永続し、route の
  `satellite_agent` にその ID を人が登録する。初回応答による登録・自動登録はしない。未指定・null・不正な値は未登録として
  `skipped:agent_unregistered` にし、ticket を出さない。不正な登録値だけで設定全体や必須4店を失敗させない。
  登録 ID は既存の `input/routes.json`／`input/config.json` の route に載せ、ticket のトップレベルには追加しない。
- satellite は「同じ run の遠隔の子」。調整役（Actions）と同じ run ID で `cli stage` 相当を動かす。
  調整役は `executor: "satellite"` の店の子プロセスを起動せず、その店へ通常収集の通信をしない。定期疎通確認もしない。
- `monitor-data` の書き手は Actions の1ジョブだけ。PC は `monitor-satellite` にだけ書く。
  遠隔の子は調整役から止められないので、PC が自分で期限停止し、調整役は期限後の結果を受理しない。
- 信頼境界: PC と、リポジトリへの書き込み権限を持つ者を信頼する。取り込み時の検証は事故・不整合・版ずれを防ぐもので、
  悪意ある PC による価格の捏造を防ぐものではない。PC が `monitor-satellite` にしか書かないのはプログラム上の制約であり、
  資格情報の権限がそのブランチに限定されるわけではない。

## 2. 受け渡し

ブランチは2本。どちらも通常コミットの追記のみ（force-push しない）。

- `monitor-relay`（書き手: Actions）。1コミット = 1 ticket、または ack に対する echo（4章）。
  - `ticket.json`: `protocol_version`、`run_id`、`phase`（`discovery`／`verification`）、`store`、`issued_at`、
    `collect_until`、`accept_until`、`seconds`、`code_fingerprint`、`fingerprint_version`、`state_schema_version`、
    `config_revision`、`input_sha256`（添付ファイルごと）、`state_sha`（出典）、`cleared_refusals`、
    確認段階では `accepted_discovery: {satellite_commit, state_sha256}`。
  - `input/stores/<store>.json`（入力状態）、`input/routes.json`（route の実効状態）、`input/config.json`（実効設定）、
    確認段階では `input/plan.json`（その店の `confirm` と `run_id`）。
  - ticket の識別子は relay のコミット SHA。
- `monitor-satellite`（書き手: PC）。1コミット = ack または result。
  複数のエージェントが `monitor-satellite` に書き込むことがある。
  - `manifest.json`: `protocol_version`、`kind`（`ack`／`result`）、`nonce`（ack）、`ticket_commit`、`run_id`、`phase`、`store`、`status`、
    `started_at`、`finished_at`、`code_sha`、`code_fingerprint`、`dependencies`、`input_sha256`、`config_sha256`（実際に使った設定）、
    `output_sha256`、`active_refusals`（未解除の拒否の集合。ack・result とも常に載せる）、`counters`。
    `agent_id` は ack では必須、旧 PC の result では省略を許す。新しい PC はすべての manifest（ack 前後の辞退・異常終了からの復旧を含む）に載せる。
  - result では `stores/<store>.json` を同じコミットに置く。
- 投稿が受理されなかった場合、送信側はまず自分の投稿がブランチの履歴にあるか確認し、あれば投稿済みと扱う。
  履歴になく、他者の投稿が先に入った場合だけ、新しい先端を親にコミットを作り直して再投稿する。
  競合時は最大5回まで投稿を試みる。継続する他者の投稿や通信障害がある場合、書き手が5つ以下でも成功は保証しない。
- 入力・出力のファイルは再シリアライズせず、バイト列のまま扱う。ハッシュはそのバイト列に対して取る。
- 確認段階の入力は、調整役が受理した発見結果（relay に添えたもの）。PC の手元の一時状態は使わない。
  発見結果を受理できなかった run では確認の ticket を出さない。
- PC は同じ ticket を二度実行しない（処理済み ticket を永続記録）。異常終了した ticket の収集はやり直さない。
  push に失敗した結果は、収集をやり直さずに再送する（未送信の結果だけをローカルに保持する）。
- 調整役は、satellite ブランチの先端だけでなく、前回処理したコミット以降の履歴を走査する。処理済み位置と監査記録
  （`state/satellite/<run_id>.json`: ticket・ack・result の各コミット SHA、受理／不受理／期限後の別と理由、段階結果）を、
  同じ `monitor-data` のコミットで永続化する。期限後に現れた result は記録するだけで、正規状態へ合流しない
  （本体は satellite の履歴に残る）。
- git 操作にはすべてタイムアウトを付け、失敗時は間隔を延ばして再試行する。受け渡しの失敗・タイムアウト・不正な結果は
  その店の段階結果（`satellite_absent`／`satellite_declined:<理由>`／`satellite_deadline`／`satellite_rejected:<理由>`／
  `relay_failed`）として記録し、ほかの店の判定と公開は続ける。
- ticket・plan・設定は JSON データとしてのみ扱う。PC はそれらをコマンドやコードとして解釈しない。

## 3. コードと設定の一致

- PC は導入済みのコードを使う。ticket を契機にコードを更新しない。
- コード・依存の指紋: 実際に import されるパッケージ（`sale_monitor` 配下の `.py`）の相対パスと内容 SHA-256
  （改行は LF に正規化）の一覧をパス順に並べたもの、`pyproject.toml` の `dependencies`、指紋方式の版番号から計算する。
  PC は導入済みの依存の版が宣言と一致することも確認する。不一致は通信せず `declined:code_mismatch`／
  `declined:dependency_mismatch`。
- 実効設定は ticket が渡す（`input/config.json`: 段階実行が読む店設定と共通設定、当該 route、`hosts.default` と
  その店のホスト設定、`config_revision`）。PC の段階実行は渡された設定だけで動き、ローカルの `config/` は読まない。
  PC は渡された設定を検証する（既知のキーだけ、URL はすべて許可ホスト、ブラウザー描画なし、
  取得間隔は PC に固定した下限5秒と ticket の値の大きい方）。
- ack 前の確認順は、制御状態の辞退理由 → コード指紋・依存の不一致 → 入力ハッシュ → 入力検証（登録 ID を含む）→ Retry-After。
  検証した `input/config.json` の route の登録 ID が無い、または自分の `agent_id` と異なる場合は、ack を出さず、店へ通信せず、
  自分の ID を付けた `declined:agent_mismatch` を一度だけ返す。この辞退は連続失敗の集計から除く。
- PC の更新は人が明示的に実行する（`python -m sale_monitor.satellite update`: 固定した本家 remote の `origin/main` へ
  fast-forward し、依存を入れ直す）。収集と排他。依存の導入に失敗したら受付停止の印を残し、次に成功するまで ticket を受けない。

## 4. 期限

```text
R = run 開始 + run.max_minutes
G = DEADLINE_GRACE、D = 判定・結果検証の予約時間
S = min(段階開始 + 設定段階時間, R - D - G)
collect_until = S - 90秒     # PC が最後の HTTP 要求を出してよい上限
accept_until  = S + G        # 調整役が結果本体を取得し終えているべき時刻（調整役の時計）
```

- ローカルの子の終了猶予もこの枠に入れる。残りが足りなければ段階を始めない。確認段階の秒数はローカル・遠隔とも
  run の残り時間に収める。
- ack の受付期限は `min(発行 + 180秒, accept_until)`。調整役がローカルの子の実行と並行して監視する。
  受付期限までに登録済み収集者の ack がなければ `satellite_absent` として待たない。
  echo を発行していない段階の `completed`／`stopped:*` は `satellite_rejected:unacknowledged` とし、結果検証に進めない。
- 受理の判定は調整役の時計だけで行う。PC の申告時刻は使わない。
- PC の時計（PC の壁時計の絶対値には頼らない）:
  - 期限用の時計（期限時計）は、スリープ・休止の時間を含む単調時計に限る。Windows は QPC（Python の `time.perf_counter()`）、
    Linux は `CLOCK_BOOTTIME`。それ以外の環境では satellite は起動を拒否する。
  - 手順: (1) PC は ticket を受けたら使い回さない `nonce` を作り、ack の push を始める直前の期限時計 `m1`（と壁時計 `w1`）を記録して、
    `nonce` 入りの ack を push する。(2) 調整役は ack を観測したら `monitor-relay` に `echo.json` =
    `{ticket_commit, nonce, actions_time}` を追記する（登録済み収集者からの、その ticket の最初の ack にだけ1回）。
    echo の送信前に nonce を固定し、同じ nonce の再配達は何もせず、違う nonce へは echo を出さず記録だけする。
    echo 送信失敗は `relay_failed` とし、後から別 nonce に echo を出さない。(3) PC は自分の `nonce` を含む echo を受け取った時点の
    期限時計 `m2`（壁時計 `w2`）を記録する。
  - echo は ack の送信開始より後に作られるので、以後は `調整役の現在時刻 ≤ actions_time + (期限時計の現在値 − m1)` が成り立つ。
    この上限 `U` が `collect_until` に達したら、PC は以後の HTTP 要求を始めない。親エージェントは `U` が `collect_until + 30秒` に
    達したら収集の子を強制終了し、それまでに保存された状態だけを結果にする。
  - 往復時間を N 秒とする。通信遅延による停止期限の前倒しは 0〜N 秒であり、遅い側にはならない
    （前倒しになるのは ack の送信開始から echo の生成までの時間）。
  - echo が ack の push 開始から120秒以内（期限時計）に届かない、`nonce` が違う、別の ticket の echo である場合は、
    店へ通信せず `declined:clock_unverified`。echo を待つ間も店へは通信しない。
  - ack 送信の直前から収集の終わりまで、壁時計の経過と期限時計の経過を比べ、開始時からの累積で5秒以上食い違ったら
    以後の通信をやめる（echo 受領の直後と最初の要求の前にも確認する）。
  - 観測時刻は PC の壁時計で記録されるので、往復から壁時計と調整役の時計の差の区間 `[w1 − actions_time, w2 − actions_time]` を求め、
    区間全体が ±30秒の外にある場合は、店へ通信せず `declined:clock_skew`（受理されない結果を作らないための事前確認）。
  - 運用上の前提: PC の期限時計と調整役の時計は同じ速さで進み、調整役の時計は run の途中で大きく飛ばないとみなす。
    `nonce` が保証するのは「echo が ack の送信開始より後に作られた」という因果関係だけである。前提が崩れた場合でも、
    期限後の結果が現在比較に使われないことは、調整役の時計だけで行う受理判定が守る。

## 5. 取得の節度と停止（PC 側で即時・永続）

- satellite の通信層は1か所。robots.txt の取得・通常要求・内部の再試行・リダイレクトのすべてがそこを通る。
- 許可ホスト（ツクモは `shop.tsukumo.co.jp`、https、既定ポート）以外へ通信しない。キュー・plan・ページ内の派生リンク・
  リダイレクト先のすべてに、通信前に適用する。HTTP のみ（ブラウザー描画なし）。
- 取得間隔は5秒以上（控えめな初期値であり、遮断されない保証ではない）。直前の要求時刻を永続し、段階の切替・再起動を
  またいでも下回らない。
- 明示拒否 = HTTP 401/403（robots.txt を含む）、または応答本文が拒否・確認画面の既知の特徴に当たる場合。
  本文の既知の特徴は次の2つで、どちらも文書の長さや HTTP 状態によらず検査する（`sale_monitor/satellite/refusal.py`）。
  - 通知文・要求文: タイトル・見出し・段落・表のセルなど**1つのまとまり全体**が、拒否の通知または訪問者への確認要求の
    既知の文型（例: `Access Denied`、「アクセスが拒否されました」、「ロボットではないことを確認してください」）に一致する場合。
    文字参照を復元し、大小文字・空白・前後の括弧と句読点を正規化して照合する。インラインタグで分かれた文は連結する。
    許す前置き（`403`・`Error` など）と後続（「…from your network.」など）は文型の側に明記する。
  - 確認用の部品: 取得した HTML に `g-recaptcha`／`h-captcha`／`cf-turnstile` の要素、その応答欄、captcha の iframe がある場合。
  `captcha`・「不正なアクセス」・「アクセスを拒否」のような**単語や文の断片は、見出しでも本文でも、それだけでは拒否としない**
  （商品説明に普通に現れる。本番の最初の run は、ルーターの仕様一覧にある機能名「Login Captcha」で誤って停止した）。
  コメント・スクリプト・スタイルの中の語、通常のライブラリー読み込みも拒否としない。
  別の場所に商品情報があっても、成立した拒否判定は取り消さない。robots.txt の本文にも同じ検査を適用する
  （指令やコメントの行は文型に一致しないので拒否にならず、robots.txt の代わりに返された拒否文・拒否画面は拒否になる）。
  文型と部品の一覧は1か所で管理する。ツクモの確認画面の実物は未確認であり、未知の確認画面を必ず検出できるとは主張しない。
  自由な商品説明のすべてについて誤検知がないことも保証しない（説明文が拒否画面と同じ構造・文言を再現する場合などは、
  停止して人が確認する）。検出をすり抜けた HTTP 200 の確認画面が続いても、応答を得た取得は成功に数えるため、
  「成功ゼロの run が3回」による自己停止はこの場合の歯止めにならない。
  最初の1回で、通信層が (1) 永続の拒否記録（`refusal_id`、日時、URL のハッシュ、種別）を書き、(2) 以後の通信を禁止する。
  呼び出し側が例外を握りつぶしても次の通信は出ない。
- 以後の ticket は、その `refusal_id` が ticket の `cleared_refusals` に入っていない限り、通信せず `declined:refused`。
  結果の push 失敗・期限切れ・再起動・無関係な `config_revision` 更新・executor の変更では解除されない。
- 調整役は manifest の `active_refusals` を、価格結果を受理できない場合（期限後、検証落ちを含む）でも取り込み、
  route を `paused_refused`（`refusal_id` つき）にする。この記録は `config_revision` が変わっても引き継ぐ。
  `config/routes.json` の当該 route に `cleared_refusals: ["<refusal_id>"]` を人が書いた時だけ解除する。
  解除済みの `refusal_id` を後から読んでも、再び停止理由にしない。
- 429・Retry-After: その段階の以後の通信をやめる（恒久停止ではない）。期限は次の ticket に引き継ぐ。
- 応答なし（接続失敗・タイムアウト。HTTP 503 や解析失敗は含めない）: 再試行を含む実際の HTTP 試行で連続5回になったら
  その段階を打ち切る。通常取得を実際に試みて成功（HTTP 応答を得て解析に進めた取得。正常な空一覧を含む。robots の成功だけは
  含めない）がゼロだった run が3回続いたら `declined:unreachable` で止まり、人が解除するまで再開しない。
  コード不一致・期限不足・Retry-After による辞退・再送は数えない。
- 拒否記録・Retry-After の期限・直前の要求時刻・停止用カウンターは PC の永続的な制御情報（入力状態とは別）に持つ。
  待機期限は制御情報と入力状態の長い方を採る。
- 段階の止め方: 通信層が段階単位の理由（期限・時計、拒否、429・Retry-After、応答なし5回）で通信を禁じたら、収集はそこで終わり、
  状態を保存して子が自分で終了する。段階の停止そのものを理由に、タスクの試行・失敗・完了を記録しない
  （要求を送る前に止まったタスクと未着手のタスクは、試行回数も失敗印も付けずにキューへ残す。商品の `latest_fetch_failed` も付けない）。
  既に発生した要求の結果（実際の失敗、拒否の事実、使用済みの予算）と成功した観測・journal は保持する。
  Collector 自身の制限時間は `collect_until`（上限 `U` 基準）までとし、通常は自分で新しいタスクを取るのをやめる。
  停止の原因になった実通信が robots.txt の取得だった場合（拒否・429・応答なし）は、拒否の事実・実通信・使用済みの予算だけを
  記録し、商品タスクは未試行のまま残す。商品の要求が実際に出て実際に失敗し、停止条件に達した場合は、その失敗と停止理由を残す。
  許可ホスト以外の URL: 入力状態・plan に含まれていれば、通信前に辞退する（調整役の取り込み検証と同じ規則）。
  収集中のページで見つけた許可外のリンクはキューにも状態にも入れず、件数だけを数える。許可外へのリダイレクトは
  その遷移を拒否し（許可外へは通信しない）、当該タスクの実際の失敗として元の URL で記録する。ほかのタスクは続行する。
  結果の状態は `completed`、`stopped:deadline`／`refused`／`retry_after`／`no_response`／`error`、
  親が実際に強制終了した場合だけ `stopped:killed`。

## 6. 取り込み時の検証（調整役）

- 処理順: JSON として読めた manifest の拒否情報は、送信者と独立に従来どおり反映する。次に、自分が発行した
  `ticket_commit` で段階を特定する。未知・確定済みの段階は記録だけする。未確定の段階では `agent_id` が登録 ID と
  完全一致しない（欠落・不正・別 ID を含む）manifest は `other_agents` だけを加算し、記録する。
  状態・ack・カウンター・コード・受信時刻・result のコミット SHA は変えず、スキーマ不正による段階拒否にも進めない。
  `other_agents` は段階を特定できた場合だけ数え、ゼロの場合は監査に載せない。
- 登録 ID が一致した manifest だけを完全なスキーマ検証と通常処理に進める。ack 前の `declined:*` は直ちに
  `satellite_declined:<理由>`（`agent_mismatch` を含む）で確定し、後の ack には echo を出さない。
  ack 後の `declined:clock_unverified`／`clock_skew`／`expired` も従来どおり確定する。
  `satellite_absent`／`satellite_deadline` の期限判定は調整役の時計で行い、登録済み収集者の manifest と期限処理だけが段階を進める。
- 未検証の記録先に manifest の値を使わない。既知の段階は調整役自身の run ID・phase から
  `state/satellite/late/<run_id>-<phase>.json` を選ぶ（manifest がキー欠落・型不正でも、段階が特定できればここ）。
  段階を特定できないもの（未知の `ticket_commit`、JSON 不正、非オブジェクト）は固定の
  `state/satellite/unattributed.json` の `records` に記録する。キーは poll で得た satellite コミット SHA。
  各記録はその SHA・受信時刻・理由（`other_agent`／`late`／`unattributed`／`invalid`）と、オブジェクトならサイズを制限した写しを持つ。
  写しは16 KiBまでとし、超過時は先頭部分だけを保持する。各ファイルは100件までで古いものから落とす。
  記録の例外はすべて飲み込み、段階の結果に影響させない。
- 対応: `ticket_commit`・`run_id`・`phase`・`store`・`code_fingerprint`・`input_sha256`・設定のハッシュが発行した ticket と一致し、
  `output_sha256` が添付の状態と一致する。
- 状態全体: 既知のトップレベルキーと型、`store`、`run_id`、件数とサイズの上限。
  取得先・取得元を表す URL 項目（キー名が `url`／`*_url`／`*_urls` の値。文字列・文字列の配列・null だけを許す）、
  値そのものが URL である文字列、URL 索引のキー、設定・計画内の通信先は、許可ホストに限定する。
  取得に使わない証拠の文章（`evidence` 以下の仕様の説明など）に現れる URL は、この制限の対象外とする
  （保存済みの商品仕様にはメーカーサイトの URL が含まれる）。`schema_availability` は、URL の値として許すのは schema.org の在庫語彙の有限集合だけとし、null・空文字・URL でない原文は保持する。
  実際の通信先の制限は、全要求とリダイレクトに対して通信層が独立に適用する。PC 側の入力検査も同じ規則を使う。
- journal: 入力にあった行は順序・内容とも変わっていない（前置一致）。新規行は `run_id` が今回、`observation_id` が再計算と一致、
  `store`・`seller_id` がその店、`observed_at` が ticket の `issued_at` 以降かつ調整役の受信時刻以前。
  入力にあった観測には時間境界を当てない。
- offers: 各 offer の基礎となる観測は、入力の offer、または今回追加された当該 offer の最新 journal 行と一致すること。
  その基礎観測に対し、今回の対応する失敗記録を根拠とする `latest_fetch_failed` の追加だけを許す。
  価格・販売者・run ID・観測時刻の変更、行の削除は拒否する。
- 書き込みは `state/stores/<store>.json` の原子的な置換だけ。受信ブランチの merge や任意パスの展開はしない。
  検証に落ちたら状態は書かず `satellite_rejected:<理由>`（拒否情報の反映だけは別に行う）。

## 7. 比較範囲と計画（scope version 2）

- `scope.optional: ["tsukumo:direct"]` を足す。必須（`in_scope`）は dospara / ark / koubou / sofmap のまま。
- 計画: 必須の確認一式・順位・予算判定を現行の手順で先に確定する（任意の店の比較先や backoff を混ぜない）。
  その後、候補行の状態が `planned`・`already_confirmed`・`no_known_comparator` のものを既存の優先順位の順に対象とし、
  任意の店の既知比較先と `unverified_urls` の確認を、その店のホスト予算から予約する。それ以外の状態の候補は対象にせず、
  計画に理由（`mandatory_not_ready`）を残す。任意の店の連続失敗による間隔延長は任意予約にも適用する。
  v1 では任意の店の検索（探索）をしない。
- 任意の店自身のセール品も従来どおり通知候補になり、必須の店の確認予算を既存の優先順位で使う。
- 判定: 今回 run で確認できた任意の店の価格は通常の比較先（棄却にも独立比較先の数にも数える）。未確認の任意の比較先は
  保留理由にしない。帰結として、PC が動いている run では (i) 任意の店の安値で棄却される、(ii) 任意の店が2者目になって
  A が成立する、(iii) 任意の店が唯一の現在比較先になって B が成立する、があり得る。PC の稼働の有無で
  `accepted → rejected → accepted` となり、再通知が生じ得る。数値基準とイベント規則は変えない。
- 表示は候補単位。`comparison_scope.unconfirmed` に区分 `optional_not_confirmed` と理由（`satellite_absent`／
  `satellite_declined`／`deadline`／`not_reached`／`fetch_failed`／`backoff`／`not_searched`／`mandatory_not_ready` など）を
  URL ごとに出す。同じ店で一部だけ確認できた場合に一括で確認済みとしない。「検索していない」と「検索して0件」を区別する。
  同じ金額基準・`same_product` が真の直近の確認（日時、額、在庫）を載せ、それより新しい在庫切れ・取得失敗の記録があれば
  併記する。「今回は比較価格に使用していない」と明記する。判定名は「比較範囲 v2（必須4店＋任意: ツクモ）」。
- 過去の安値を理由に現在の通知を保留する規則は入れない。

## 8. robots.txt

- RFC 9309 に沿った照合を実装する（User-Agent グループは製品トークン `PCSaleMonitor` の大文字小文字無視の一致、
  なければ `*`。最長一致、同長なら Allow 優先、`*` と `$`、パーセント符号化の正規化）。
  401/403・5xx の扱いと Crawl-delay の取得は従来どおり。全店に適用する。
- 有効化の前に、4店の保存済み robots 応答と保存済み URL 群に対して新旧の判定差を通信なしで洗い出し、内容を確認する。

## 9. PC 側の設置

- 専用 clone と venv。`python -m sale_monitor.satellite run`（常駐）／`update`／`status`／`clear-local`
  （`unreachable` と受付停止の解除。`refused` は ticket の `cleared_refusals` でしか解除できない）。
- エージェントは1つだけ起動する。Windows の既定 home は `%USERPROFILE%\.pc-sale-monitor-satellite`。
  `--home` で上書きでき、旧 `%LOCALAPPDATA%\pc-sale-monitor-satellite` からの自動移行はしない。
  `agent.lock` の OS ロックを取得してから `agent_id` を生成・保存する。`status` は `agent_id` を表示する。
  既存 ID の読取はロック不要。無ければロックを取って作り、実行中のエージェントがロックを持つ場合は再読込する
  （まだ生成途中なら null を表示し、別 ID は作らない）。
- 30秒ごとに `git ls-remote` で relay を見る（GitHub API は使わない）。
- 多重起動防止は、プロセス終了で解放される OS のロック。収集の子はジョブオブジェクトに入れ、親が死ねば終わる。
  タスクスケジューラでログオン時に起動し、異常終了で再起動する。ログはローカル。
- push は所有者の既存の git 資格情報を使う。新しい秘密情報を Actions に置かない。

### 収集者の交代

- 登録した1つのエージェントだけが収集し、未登録のエージェントは収集しない（収集者は高々1つで、ゼロの場合もある）。
- 通常の交代は、旧エージェントと収集中の子プロセスを停止し、自動起動の登録を削除してから、home フォルダー全体を移動する。
  `agent_id`、拒否ラッチ、Retry-After、連続失敗数、未集計分、処理済み ticket をすべて引き継ぎ、設定は変更しない。
- home を失った後の再登録は、停止状態の継続保証から外れる手動復旧とする。時間の経過は、Retry-After の満了や拒否の解除を証明しない。残っている記録（Actions 側の経路状態、`monitor-satellite` に届いている manifest の `active_refusals` など）で確認できる拒否・待機期限は従来の規則に従う。復元できない状態（未送信の拒否ラッチ、Retry-After、連続失敗数、未集計分）を失ったまま再開する判断は、通常の移行（home 全体の移動）とは別のものとして、人が明示的に行う。
- 同じ `agent_id` の home のコピーを2つ同時に動かす運用は保証外とする。1 ticket 内では echo は1つなので収集者は1つだが、
  ticket をまたいだ通信の重複は起こり得る。
- 版が混在する場合、進行中の run は開始コミットのコードを使い続けるため、旧調整役の ticket が新 PC に届くことがある。
  PC は指紋確認を先に行い、ack 前に `declined:code_mismatch` で辞退する。新調整役は旧形式（`agent_id` 無し）の辞退を
  記録するだけで、段階は `satellite_absent` となり、理由は別記録に残る。旧調整役が公開する結果名は保証しない。

## 10. v1 に入れないもの

任意の店の検索、期限後の価格・キューの自動合流、Actions からの定期疎通確認、ticket を契機とする PC のコード更新、
機能ごとの executor、過去の安値による限定保留、受け渡しブランチの履歴整理。

## 11. 試験の契約

- satellite が居ない run の価格判定とイベント生成は、同一の入力状態・判定時刻で、任意の店を範囲外に置いた場合と同じ。
- `executor: "satellite"` の店の子プロセスを調整役が起動せず、調整役からその店への通常収集の通信がゼロ。
- 発見時刻の観測を含む正常な確認結果を受理できる。発見結果の不受理・段階間の PC 再起動で、別の状態から確認を始めない。
- 遅い ack、締切境界、壁時計の変更、スリープ復帰（echo 待機中を含む）で期限を延ばさない。往路・調整役の処理待ち・復路の遅延を
  分けて与え、期限を越えて要求を開始しないことを確認する。壁時計を前後に動かしても停止時刻が変わらない。期限後・run ID 違い・検証落ちの結果を現在比較に使わない。
- 入力 URL・リダイレクト・journal の改変を、通信前または取り込み前に拒否する。対象の店以外の状態ファイルを書き換えない。
- 「新規観測 → 同じ商品 URL の後続取得が失敗 → 失敗印」の順序を受理する。旧 offer の価格・run ID・時刻の変更、行の削除は拒否する。
- 最初の拒否後の追加通信がゼロで、結果送信失敗・再起動・設定版の変更の後も、`cleared_refusals` に入るまで停止する。
  解除済みの拒否を後から読んでも再停止しない。
- 指紋・依存の不一致、echo が遅い（120秒超）・来ない・`nonce` 違い・別 ticket の echo、壁時計が確実に30秒以上ずれている場合は、
  店への通信ゼロのまま辞退する。
- 必須の計画（選抜・順位・予約）が、任意の店の既知比較先の有無で変わらない。任意の未確認で保留しない。
  確認できた任意の比較先による棄却、A/B の成立、再通知への影響を確認する。
- 受け渡しの git 障害で、ほかの店の公開が止まらない。
- robots の `*`・`$`・最長一致・Allow 優先・パーセント符号化・グループ選択。
- 実際の持ち越し状態を使い、外部通信を偽物に置き換えて、計画から公開 JSON まで通す。
