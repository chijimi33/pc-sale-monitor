# Isolated acquisition experiments

The lab writes only under the E-drive experiment root on Windows, or the
`RUNNER_TEMP/pc-sale-monitor-lab` directory in GitHub Actions. Each run needs a new
output directory. It does not publish production prices, send notifications, or
add formal audits.

## Bounded URL plans

`transport-study` uses six selected product URLs by default. An explicit `--plan`
can select 1–20 unique HTTPS URLs from the configured store seed hosts. Give each
URL its intended role: `home`, `list`, or `product`. Every one of the ten monitored
stores must appear either in `resources` or in `not_requested` with a reason.
Rakuten remains excluded. A role is acquisition intent, not proof of page content.

The plan has exactly these fields:

```json
{
  "format": "pc-sale-monitor-request-plan-v1",
  "created_at": "2026-10-04T00:00:00+09:00",
  "source_data_sha": "<40-character pinned data-branch commit>",
  "resources": [
    {"store": "ark", "url": "https://www.ark-pc.co.jp/", "kind": "home"}
  ],
  "not_requested": [
    {"store": "<each remaining store>", "reason": "<why it is outside this run>"}
  ],
  "plan_hash": "<digest of all the other fields>"
}
```

This is a schema illustration; replace the placeholders and enumerate the
remaining stores before running it. To seal a completed plan in Python, remove
`plan_hash`, then assign `plan['plan_hash'] = monitor_lab.safety.digest(plan)`.
That digest uses sorted keys, compact JSON, UTF-8, and no ASCII escaping. The hash
detects changes; it does not establish trusted authorship or price evidence.

Example PowerShell commands, with an existing verified plan and new output paths:

```powershell
python -m monitor_lab transport-study --plan "$plan" --output "$capture" --budget 240 --methods urllib pooled
python -m monitor_lab verify-capture --input "$capture"
python -m monitor_lab replay-capture --input "$capture" --output "$replay"
```

`--budget` is shared across methods, accepts a positive number up to 2,100 seconds,
and defaults to 2,100. Native DNS, TLS, and OS stalls still have no verified hard
cancellation guarantee. Hosts share spacing and failure waits across methods; a
403 or active retry wait can hold later entries without issuing another request.
The `pooled` CLI option records receipt method `pooled_http11`. The verifier checks
this explicit mapping, resource roles, receipt order, and final scope counts.
Browser experiments remain limited to Koubou.

Successful home and list responses are retained but skipped by product replay.
Only successful, complete product responses reach the normalizer. Parsing is not
proof of complete product fields or eligibility. Read `scope` in the study result
for actual HTTP attempts, successful responses, distinct successful product URLs,
and omitted-store reasons. Held entries are not failed HTTP attempts; repeated
responses are not additional unique products. All ten stores stay in the coverage
denominator, and `full_store_coverage_proven` remains false.

Verification and replay read retained evidence without network requests. Preserve
the original bundle when a check fails; diagnose the validator or capture rather
than editing historical receipts. Partial capture verification requires the
explicit `--allow-partial` option and does not make an interrupted run complete.

## Discovery and decisions from a scoped capture

The regular `run` command accepts `--capture-input` in replay mode. It uses the
selected method's verified capture outcomes for every permitted URL, including
failures and missing attempts. It never falls back to the older six primary-page
HTML fixtures. The pinned input still provides the original backlog, history,
and event registry; their dates may differ from the capture and remain explicit.

```powershell
python -m monitor_lab run --input "$inputs" --snapshot ark_repaired --architecture B --mode replay --capture-input "$capture" --capture-method urllib --output "$result" --budget 180 --max-tasks 20
```

Captured home/list resources become discovery tasks with ordinary sale-signal
filtering. They do not mark every linked catalog product as a sale. Products
discovered with sale evidence become candidates; other captured product resources
wait for discovery or a comparison request. Repeat `--candidate-url URL` to select a known
candidate explicitly. Each candidate must be a product URL in this capture.
An arbitrary number of captured product resources is supported within the plan
limit; six primary-page fixtures are required only by the original mode.

All discovered dependencies retain their source and original age. URLs without
captured responses stay pending; failed or exhausted responses cannot turn into
successful retries. Non-HTML adapters remain an explicit external wait in this
path. The same persisted queue, current-comparison checks, history rules, and
event handling are used for the resulting observations and decisions.

This is a simulation of queue order using recorded outcomes. Its clock is
separate from the original observation times and real elapsed time. Read
`capture_replay` for source attempts, replayed attempts, evidence gaps, and the
two data revisions. Coverage reports `recorded_source_http_attempts` separately
from new `confirmed_http_attempts`, which stays zero. Reopening the same output
resumes its saved budget and consumed outcomes instead of fetching again.
Later discovery tasks may reparse a successful home/list response already
committed in the same run. Each derived receipt retains the original observation
time, method, body hash, capture manifest and receipt index, and names its original
dispatch. `discovery_analysis_reuses` counts these separately; their attempts,
waits and elapsed acquisition time are zero. Task history uses
`lab_analysis_reuses`, and child discovery evidence preserves the source link.
This does not retry failed tasks or reuse product prices. Failed, incomplete,
interrupted or changed evidence cannot supply a successful reuse. Host gates,
the original deadline and the work-slot cap still apply. The standalone
`queue-study` command retains its consume-once behavior.
Capture and local store settings must match. Live mode and separate discovery
fixture bundles cannot be combined with this option.
If a host is blocked or its wait cannot fit, the scheduler can advance to an
allowed later cycle that has other runnable work. It retains the original host
gate, request cap, and deadline; advancing the cycle does not authorize a retry.

## Bounded follow-up acquisition

Prepare a separate follow-up from explicit pending dependency task IDs in a
scoped-capture experiment. Preparation revalidates the original capture and
reparses the relevant source HTML to confirm each destination. It retains task
ages, attempts, dependencies, parent evidence and original host gates without
changing the source queue. Every unselected store remains explicitly listed.

```powershell
python -m monitor_lab prepare-followup --experiment "$previousRun" --capture "$previousCapture" --task-id "$pendingTask" --output "$followup"
python -m monitor_lab transport-study --followup "$followup" --methods urllib --budget 90 --output "$newCapture"
```

`transport-study --followup` rechecks the prepared intent and all referenced
inputs before constructing a transport. It starts a separate, bounded capture
with new response times; original host blocks and unexpired waits still prevent
requests. Its portable provenance links to the original experiment, source
capture, exported state and task evidence. Simulation-translated waits cannot
replace original live host gates. `--plan` and `--followup` cannot be combined.
Capturing a dependency does not complete its original queued task, establish an
empty search, or make any discovered price eligible by itself.

Apply that capture to a new complete copy of the original experiment:

```powershell
python -m monitor_lab apply-followup --intent "$followup" --capture "$newCapture" --output "$continuedRun" --budget 120 --max-tasks 20
```

This revalidates the source intent and requires the capture's full retained
provenance to match. Only the selected tasks run, using their original search
query, parsing scope, age and dependencies. All source tasks and transaction
history survive. Each application has a separate persisted collection budget;
reopening it cannot reset either its own budget or the earlier collection.
Receipt indexes and the replay clock belong to the selected capture and phase.
An interrupted reservation remains unknown, consumes a slot and retains a wait.

List responses expand dependencies without promoting comparison results into
sale candidates. New product observations go into `phase_observations`; prior
observations, decisions and events remain unchanged. This command does not
reevaluate historical candidates, publish prices, notify or add a stability
sample. New out-of-scope children remain pending and can supply the next explicit
`prepare-followup` intent with this continued experiment and capture.
When an existing product has discovery evidence from multiple phases, preparation
revalidates each ancestral snapshot, capture and activation transaction. The same
ancestry authenticates inherited translated host waits without clearing them.
Keep referenced source directories: missing, changed, cyclic or overly deep
ancestry is rejected before acquisition.

The observed Sofmap query shell loads its product list from a separate HTML
fragment. The lab recognizes its selected tab, matching keyword and retained GET
loader contract, including first-load `isFirst=true`, without executing JavaScript.
It omits the volatile cache-busting `_` parameter and records that omission.
It records a further list
dependency; an empty shell or displayed count is never proof of an empty search,
a product identity or a price. A changed or ambiguous contract is a parse error.

## Fresh candidate/comparator phase

To evaluate candidates after discovery, explicitly select their existing task
IDs and prepare a new product capture. Preparation derives every related known
product task, includes catalog matches, and retains unfinished store searches.
URLs must have authenticated captured product or discovery evidence. Duplicate
queue entries for one product remain separate tasks but share one request.
Their listing evidence is combined before the shared observation is evaluated,
so processing order cannot erase an expiry or conflicting claim.

```powershell
python -m monitor_lab prepare-comparison --experiment "$continuedRun" --capture "$previousCapture" --candidate-id "$candidateTask" --output "$comparisonIntent"
python -m monitor_lab transport-study --comparison "$comparisonIntent" --methods urllib --budget 90 --output "$comparisonCapture"
python -m monitor_lab apply-comparison --intent "$comparisonIntent" --capture "$comparisonCapture" --output "$comparisonRun" --budget 120 --max-tasks 20
```

The intent and capture have a separate provenance type; `--comparison`, `--plan`
and `--followup` are mutually exclusive. The 20-task bound is explicit; larger
derived dependency sets are rejected instead of silently truncated. Original
host blocks and waits still apply. The source is a scoped collection or verified
follow-up; comparison outputs are currently terminal evaluation artifacts, not
inputs for another follow-up or comparison phase.

Application preserves the complete queue, old observations, decisions, event
state and collection budgets. Only freshly parsed responses from this capture
enter its `phase_observations` and `phase_decisions`. Missing candidates never
fall back to historical prices. Missing/changed comparator identities and
unfinished comparison discovery hold both payment and points decisions.
Historical source files are hash-checked and used only for rule B. Resume keeps
the same request cap and deadline. These are laboratory decisions with no
notifications, publication or formal audit credit.

For Sofmap, the lab selects JAN/EAN/UPC only from the verified primary product
specification/info tables or product-bound JSON-LD. Product numbers establish
scope but do not supply JAN. It retains leading zeros, conflicting claims and
the original parser value as evidence. Unknown/conflicting primary evidence
does not fall back to a footer or recommendation identifier. Other product
conditions, shipping, warranty and stock checks remain in force. This override
does not alter the production parser.

## Validation

```powershell
python -m unittest discover -s monitor_lab/tests -v
```

Ordinary PR updates run offline tests and capture/replay smoke checks. The separate
live-acquisition workflow still uses the default six-product plan. A custom local
plan does not alter the workflow or establish scheduled production stability.
