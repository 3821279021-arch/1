# Security policy

V3.1 is the current supported series. V3.0 snapshots remain readable; deploy fixes on the current version.

Report vulnerabilities through the repository's **Security → Report a vulnerability** private reporting channel if enabled. If it is unavailable, contact the maintainer privately before sending details. A public repository/contact has not been supplied in this delivery; the owner must enable private reporting or add a working private address before publication. Do not post keys or exploit details in a public issue.

BYOK credentials are owner/scoped, encrypted at rest or kept temporarily in memory. Protect `data/`, the encryption key, backups and the server host. The operator can decrypt hosted keys; encryption is not isolation from the operator. Use HTTPS and one application worker. Key deletion/replacement invalidates running routes.

Custom provider endpoints retain URL, public-DNS and pinned-request SSRF checks. No redirect should bypass those checks. Model output is untrusted; frontend uses text rendering, and actions are validated by the rule engine.

Sessions, API keys, recovery codes and Authorization headers must never enter exported telemetry. Research traces include private roles/actions and prompts; they belong to the local operator or original host after completion. Public replay has narrower permissions. Default experiment directories are private and ignored by Git/Docker; review any artifact before sharing.

The runner accepts environment variable **names**, not inline API keys. It serializes runs, reserves budgets before network calls and persists reservations across crashes. Hosted model answers are not guaranteed deterministic. No provider hidden chain-of-thought is requested for storage or exported.
