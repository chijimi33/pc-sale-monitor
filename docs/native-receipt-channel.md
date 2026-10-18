# Native receipt channel

The four-hour workflow has a dedicated Yahoo controller that delegates public
requests to fixed, locally approved Windows code. GitHub owns the queue, reparses
signed response bodies, and publishes verified state through the existing global
state-writer concurrency group. The standalone validation workflows never publish
`monitor-data`. Neither the channel nor CI success enables Qwen. Deployment and
genuine scheduled validation must be recorded separately from code/test results.
A successful discovery receipt is not a verified price.

## Repeated sessions and publication

The production session lasts up to 35 minutes, with at most 200 batches of up to
four tasks. The existing persisted lane/age/retry scheduler selects tasks. Each
batch is signed by a fresh controller key and carries the authoritative current
state; only the prefix actually received advances the scheduler. The request
artifact is verified against the controller key from the encrypted session
bootstrap. The controller's private key stays outside all artifact directories.
The old two-task probe below remains available as a separate protocol check.

Before its first retailer request the Windows worker atomically saves a claim.
After interruption, the same claim can only deliver a completed, signed prefix;
it cannot fetch again. A claim with no checkpoint is reported as ambiguous and
left pending. Expired checkpoints stay local and cannot be retimestamped. Upload
retries first find the existing artifact and compare its exact signed bytes and
bodies. They never overwrite a conflicting artifact. A controller-signed finish
notice lets the worker leave a completed session promptly.

Each receipt contributes its final cumulative HTTP counters once. A missing
receipt or partial batch makes the total request count unknown; the count of
verified received requests remains available separately. Prices, seller identity,
shipping requirements and scheduled-run provenance use the existing gates.

Artifact runtime credentials permit uploads, so artifact names are not a trust
boundary. The publisher downloads only artifact IDs returned by the trusted
collection jobs (including unique matrix outputs). It also requires Yahoo's
controller-signed export, the actual source/run/data bindings, and a matching
initial state hash. The controller key is read only after proving its commit is
an ancestor of the real `native-worker-mailbox` branch. An unchanged repeated
export is idempotent; a changed destination rejects intake and preserves it.
Omitting a native flag from incoming state cannot bypass the configured check.

`run-native-session-probe` exercises the same collection scope and publisher on a
disposable copy. It uses the real numeric Actions run ID, whose event is a PR
label, and explicitly records zero production writes and no scheduled/formal
credit. It requires at least two accepted batches and byte-stable repeated intake.
The native job and both probes share `native-yahoo-worker` concurrency.

The per-job limit is 500 artifacts; 200 request/receipt pairs leave room for the
finish notice, state export and validation index. Bodies already saved in receipt
artifacts are not duplicated in the index artifact. Session credentials use
GitHub's artifact service, not continuous GitHub REST polling. The idle mailbox
lookup must remain at most once every two minutes. Storage/throughput still need
to be measured in the real deployment.

## Authority and data flow

1. A same-repository PR bearing `run-native-channel-probe` starts the isolated
   controller. It checks out that exact PR head and reads the current data branch.
2. The controller selects at most two existing Yahoo root searches (one comparison
   and one sale lane) without removing old tasks or claiming scheduler fairness.
   It issues a ten-minute lease containing the exact original tasks and state hash.
3. A separate `native-worker-mailbox` branch holds the lease and an encrypted
   artifact runtime credential. X25519 + HKDF-SHA256 + AES-GCM binds the ciphertext
   to all lease metadata. The repository write token stays in the controller job.
4. A one-shot native worker resolves the mailbox ref using GitHub's public API,
   then reads that immutable commit over HTTPS. Raw branch URLs may cache an
   earlier lease for minutes. A local policy must
   explicitly approve the source commit and complete Python runtime hash. It
   never checks out code, installs packages, or runs commands from the mailbox.
   It reads the immutable data commit, compares state/config hashes and each task,
   then uses the ordinary retailer client. Requests retain its normal cooldowns.
   Mailbox polling must respect GitHub's anonymous API quota; an unavailable or
   rate-limited lookup does not authorize reuse of an older lease.
5. The worker signs immutable response checkpoints with Ed25519, and uploads the
   last completed checkpoint using the official `@actions/artifact` library.
   The child process receives only the artifact credential. SDK diagnostics are
   not persisted locally because signed download URLs may appear in them.
6. The controller downloads an immutable artifact without extracting it, verifies
   its digest, validates ZIP names/types/sizes, then checks the signature and
   reparses the responses. It does not import a worker-supplied completed state.
   Its receiver state and receipt are retained as a validation artifact only.

The registered worker key authenticates the sender, not the truth of a retailer's
content. Repository writers can issue work; the local approval file, supported
task checks and fixed retailer allowlist constrain what the worker will do.
The ten-minute limit is an application lease; GitHub controls the underlying
artifact token lifetime. No token is placed in a command argument or plaintext
file. Only public keys are committed. Rotating/revoking a worker requires changing
the registry/local policy; the probe does not automatically rotate keys.

## Local provisioning and execution

Keep the private directory outside every checkout and public artifact directory.
Before generating a key, restrict that directory's Windows ACL to the current
user and SYSTEM. `python -m sale_monitor.native_channel generate --key <private>
--output <public-json>` stores the private key using current-user Windows DPAPI.
The command refuses an existing key. Authentication/password entry is not needed.

The local policy contains `enabled`, `source_sha`, `runtime_sha256`,
`public_key_hex`, and `exchange_key_hex`. Approve the exact reviewed source once;
do not derive or update approval automatically from the mailbox. Run the fixed
checkout with `python -m sale_monitor.native_channel work --key <private>
--policy <policy> --output <new-evidence-directory> --uploader <artifact.mjs>`.
Use that checkout's locked Node dependencies and a new output directory per attempt.
Windows Git checkout/archive conversion can change LF to CRLF. Provision a
dedicated checkout with local `core.autocrlf=false` before checkout, or materialize
the exact committed blobs, and compare all Python file bytes. A runtime mismatch
must stop before fetching retailer pages; never substitute the remote hash into
the local approval file without making the actual bytes match.

Failed or late uploads leave local checkpoints available, but expired leases are
not retimestamped or silently accepted. A retry requires an explicit new lease.
Duplicate receipt delivery is idempotent; a different checkpoint cannot replace
an already consumed lease. Concurrent receiver writers are locked and the state
replacement is atomic. A source/config/base-state mismatch rejects intake.

## Deployment and acceptance boundary

The older probe's two-task selection is an integration sample. The session worker
entry point is `python -m sale_monitor.native_operations work --root <owned-runtime>
--key <DPAPI-key> --policy <approved-policy> --sdk <locked-session.mjs>` from the
exact approved source directory. A separately provisioned scheduled task must use
that fixed command, ignore overlapping starts, and preserve unrelated services.
Do not automatically approve the source hash found in the mailbox. This code alone
does not satisfy coverage, age, shipping, seller identity, formal audit, scheduled
stability, or Qwen handoff gates. Unknown shipping remains pending.

Protocol tests cover native state preservation, signed-body replay, duplicate and
stale receipts, encryption tampering, local approval, expiry, credential host
constraints, ZIP traversal/duplicates/symlinks/size, and Windows DPAPI persistence.
Live integration evidence is recorded separately from these synthetic tests.
The first real round trip also captured a Yahoo `zeroMatch` response. The parser
accepts it only on page one with an exact query/header/URL match, integer zero
count, null results and pagination, and no rewrite or suggestions. Other empty
shells and contradictory responses stay pending.

Upstream implementation references:
- https://github.com/actions/toolkit/tree/main/packages/artifact
- https://github.com/actions/toolkit/blob/main/packages/artifact/src/internal/shared/config.ts
- https://docs.github.com/en/actions/reference/security/secure-use
- https://github.com/actions/upload-artifact#number-of-artifacts
- https://github.com/actions/download-artifact/tree/v4#download-artifacts-by-id
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#using-job-outputs-in-a-matrix-job
