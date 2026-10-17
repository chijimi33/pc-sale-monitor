# Native worker mailbox

This branch carries short-lived, encrypted requests for the isolated native receipt validation. It contains no production state and no plaintext credentials. Only a locally approved, fixed worker can decrypt a request. Application leases expire after ten minutes.
