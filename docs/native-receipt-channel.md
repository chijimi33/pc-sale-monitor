# Native receipt channel: isolated validation

This branch connects the signed native receipt implementation to a real GitHub
Actions job. It does not change the four-hour production workflow, publish to
`monitor-data`, run a self-hosted Actions runner, enable Qwen, or install a local
scheduled task. The source/config/data/run bindings and strict product checks
remain mandatory. A successful discovery receipt is not a verified price.

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

## Remaining production work

The probe's two-task selection is an integration sample, not a production queue
scheduler. Deployment still needs persistent fair selection, resumable delivery,
atomic intake coordinated with the production state writer, explicit collection
metrics/provenance, and validation of the genuine scheduled runs. This PR alone
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
