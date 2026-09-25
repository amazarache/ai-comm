# Changes

## 0.2.0

- Add stable actionable error codes and hints, safe field/count details, and
  `send --dry-run` with no durable publication or retry-key reservation.
- Add strict release records, offline `release-status`, and derived advisory
  notices in `state` and `inbox`. The CLI does not fetch, install or re-pin.
- Add Linux/Python 3.10–3.13 CI, manual-only repeated stability checks, and
  tagged source archives with SHA256SUMS. No provenance attestation is emitted.
- Compatibility: `PROTOCOL.md` remains byte-identical at digest
  `709af4b0…`; v0.2.0 opens existing protocol 2.0 channels.

## 0.1.3

- Document per-project package pins and channels, participant roles and reviewer
  setup, same-account trust limits, package upgrades and copy-paste activation
  prompts for both the initiator and peer.
- Compatibility: package `0.1.0`–`0.1.3` uses protocol `2.0` (`709af4b0…`) and
  opens only channels whose stored digest matches its bundled `PROTOCOL.md`.
- Package and protocol versions are separate; a breaking contract, storage or
  CLI change bumps package MAJOR. The full mapping is in the README compatibility
  table. `PROTOCOL.md` remains byte-identical, so existing v2.0 channels work.

## 0.1.2

- Fix an intermittent notification-arrival test failure. The test now uses a
  real sender thread coordinated after the first empty scan. Its previous global
  `time.sleep` mock also intercepted Python subprocess waits, causing a send to
  re-enter a channel lock already held by the test.
- Runtime behavior and protocol bytes are unchanged from 0.1.1; only the package
  version, test harness and release records change.

## 0.1.1

- Add opt-in `wait --new-since N` notifications with a separate
  `notification_through` cursor. Notifications do not change receipts; normal
  inbox and default wait retain unread-message replay.
- Render each source's details once per export, then cite the first rendered
  message. Partial exports remain independently readable.
- Reduce text-validation overhead while preserving the complete v0.1.0 Unicode
  rejection set and full history/integrity checks. Include an exhaustive
  regression and a reproducible synthetic scale benchmark.
- Explain historical-entry-point notices when adopting a new local protocol.

Protocol version remains 2.0. PROTOCOL.md is byte-identical to v0.1.0, so existing
channels remain compatible. This is an explicitly reviewed package update;
there is no automatic installation or migration.

## 0.1.0

Initial local communication release with explicit activation, committed delivery,
incremental inboxes, checked receipts, nonce handshake, evidence registries and
exact-artifact reviewer agreement.
