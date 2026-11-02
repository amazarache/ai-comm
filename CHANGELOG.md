# Changes

## 1.6.0

- Add a per-user cache of verified releases, `install.sh --user <command>`
  (`scripts/install_user.py`), under `$XDG_DATA_HOME/ai-comm` (default
  `~/.local/share/ai-comm`). `install --version X --fetch-permitted` fetches exactly
  tag `vX` and the release index from the package's manifest-bound origin, checks
  that the archive it runs from (if any), the annotated tag and the index name the
  same manifest, runs the tag and manifest checks and the full suite in private
  staging, and publishes an immutable release; `current` and `previous` links
  switch with `activate` and `rollback`. `status` and `check` are offline;
  `check --fetch-permitted` refreshes only the index and `v*` tags and records
  trusted bindings. `check` reports `up_to_date` only on fresh metadata. The cache
  follows the origin of its first install. Every change is journaled; an
  unfinished operation blocks the next one until `recover`, which finishes it only
  from the state it left and never deletes what it cannot prove is its own.
  `uninstall --confirm` keeps anything unexpected or changed. `docs/INSTALL.md`
  describes a verified first install on a new machine.
- Add `scripts/verify_pin.py`: checks a project's pin with the cache's code and
  runs nothing from the pin. Git is read without a work tree, with every object
  re-hashed, every transport disallowed and promisor layouts refused; every
  released working file is hashed. Reports `verified` (with the upgrade path for
  its version), `unknown`, `modified` or `absent`.
- Add `scripts/declare_project.py`: `init` writes a project's unintegrated upgrade
  declaration; `install` replaces it with exact owner-reviewed bytes, validated
  against the real project, under the project's lock, with no guided upgrade in
  progress and the expected previous digest. The controller's declaration check
  is now the pure `validate_project_manifest`, with `load_project_manifest`
  unchanged in behaviour.
- Add the per-user entry skill `entry/ai-comm/SKILL.md` and `register` /
  `unregister`, which link it into a skills directory the user names after
  checking the effective skill names already there; the optional `launcher`
  command writes an `ai-comm` command that runs a project's own pin only when it
  verifies; `offer status | claim | answer` give the cache's own update offer one
  notifier at a time, with the project controller's answers and snooze steps.
  Links and the launcher are removed only while unchanged.
- The two installers share `scripts/release_install.py`; `install.sh --project`
  is unchanged in behaviour. The cache commands exit 0 for `--help`.
- No protocol, channel, existing CLI output or error-code change. PROTOCOL.md is
  unchanged at the 3.0 contract digest.

## 1.5.3

- Make every instruction host-neutral: README, ACTIVATE and the session skill
  no longer name any agent host or product command. Skill installation uses the
  skills directory the host scans (`<skills-dir>`) with a one-name check, a
  verified copy fallback and the unchanged read fallback. Wake guidance is
  described by host capability: bounded reading while active (ordinary `wait`
  or `inbox`, then acknowledge), an exit-wake listener for hosts that re-invoke a
  session when a background command exits, and a session notifier for hosts
  that let another process queue a message into one session. Approval, receipt,
  session-ownership and scope rules are unchanged.
- Add a short "How any agent uses ai-comm" section to the README.
- Tests use neutral handles and host labels, cover a third participant with an
  unseen host label and two sessions sharing one host label, and a neutrality
  test fails if a known agent-host or vendor name appears in the package. No
  protocol, CLI, output-field, storage or error-code change.

## 1.5.2

- Fix `ai_comm.py upgrade check` on a pin that is already at the newest verified
  release: it now reports `status: ok` with no prompt instead of stopping with
  `upgrade_plan_invalid`. The controller reads the core plan's expected
  `upgrade_target_not_newer` result from stderr (exit 2, empty stdout, strict
  bounded JSON); every other error, malformed output or explicit target still
  stops. The controller test fixture now follows the real CLI error contract,
  and a contract test checks the real CLI's output. No protocol change.

## 1.5.1

- Fix a test-only defect that stopped the 1.5.0 release workflow: the
  end-to-end upgrade test created a fixed `v1.5.0` tag in a clone of the
  repository, which collides once that real tag exists. The test now uses the
  tree's own version and replaces a same-named tag only inside its throwaway
  clone. No change to the package code, protocol or upgrade behaviour.
- The `v1.5.0` tag remains in the repository but is not a published release and
  is not listed in the release index.

## 1.5.0

- Add `upgrade_controller.py` and `ai_comm.py upgrade`: a guided, owner-consented
  upgrade with one scoped question (yes, fetch only, not now, skip, never),
  single-use consent bound to project, source, from-commit and target, private
  verification of the target, and a frozen controller copy outside the pin so
  that rollback and recovery keep working after the pin changes.
- Journal each transaction as hash-chained JSON Lines with intent and outcome
  records, probe-resolved recovery, torn-tail preservation and a project lock.
- Declare project listeners in `.ai-comm-local/upgrade/project.json`; refuse
  unsupported integrations before any change; peer phases are reported by their
  owner with a one-time nonce; completion requires live-validation and guidance
  DECIDEs.
- Add `install.sh` for new projects: consent-gated fetch of exactly one tag and
  the release index, verification of the selected index record and full suite in
  private staging, atomic publish, `unintegrated` project manifest, `.gitignore`
  preservation and journaled recovery. It never creates or joins a channel.
- No auto-apply option. Older pins keep separate Gate 1 and Gate 2 questions.
- Keep PROTOCOL.md unchanged at the 3.0 contract digest.

## 1.4.2

- Add skill-guided start A/B, status, and stop workflows with a role table,
  listener-first startup, durable PING handshake, host-specific invocation, and
  bounded waits that do not promise to wake an idle model.
- Add one release notifier per shared pin, release notices for older pinned
  sessions, separate Gate 1 and Gate 2 questions, and channel-recorded prompt
  status that deduplicates across takeovers without transferring owner approval.
- Document wake limits and exact-thread nudge controls; clarify credential
  helper erase behavior and make skill links preserve existing paths safely.
- State the expected format in rejection hints for every shared format
  validator (topics, handles, ids, retry keys, digests, nonces, message ids).
  Hints are built from fixed rule text only and never reflect the rejected
  value. Error codes and fields are unchanged.
- Document `show-artifact` in command help and the README, including reading
  an artifact before ACCEPT, and state that a plain `wait` returns the same page
  and `through` value as `inbox`.
- Document drafting conventions for an established channel in ACTIVATE.md
  section 3: peers settle routine authorship, the first committed drafting
  notice wins, only active notices block a topic, and duplicate owner relays
  keep their own provenance. Role binding and owner gates are unchanged.
- Correct the README start note: ai-comm itself does not wake idle sessions,
  but separately approved host listeners or nudge helpers can.
- Keep PROTOCOL.md unchanged at the 3.0 contract digest.

## 1.4.1

- Add an explicit private-origin release-check option that reads only configured
  global Git credential helpers and passes them through Git's environment config
  instead of process arguments. Fetch in a temporary bare Git directory that
  shares only the target object store, then atomically publish only permitted refs.
- Fail closed when helper configuration is missing, unreadable or ambiguous;
  isolate network access from checkout-level URL rewrites, proxies, redirect and
  credential settings. Explicit 403 responses now report `auth_failed`, and
  redirects are refused even when credential-helper use is not enabled; when
  Git omits an authentication status, retain the generic `fetch_failed` code.
- Document that Git may invoke a helper's normal credential `store` or `erase`
  operation on fetch success or authentication failure.
- Keep the concurrent heartbeat ordering test strict while reducing contention,
  and test lock exhaustion separately.
- Keep `PROTOCOL.md` unchanged at the 3.0 contract digest.

## 1.4.0

- Add an agent-neutral project skill and document per-host project-local
  installation, discovery checks, and a hash-verified copy fallback.
- Keep the complete upgrade path owner-gated: separate bounded release-ref
  refresh approval from approval to apply a named version.
- Require 1.4.0+ offline repin plans to use an already-local verified tag;
  missing pin or newest-target tags require Gate 1, while unrelated missing
  historical tags are reported as warnings.
- Keep `PROTOCOL.md` unchanged at the 3.0 contract digest.

## 1.3.0

- Add passive, best-effort process heartbeats outside the Git ledger and receipts.
- Record `wait` heartbeats once at bounded-call completion (a deliberate deviation from sampling immediately after identity validation).
- Replace a stored heartbeat more than five seconds ahead of the observed clock only when the new sample is fresh; a delayed sample never erases a later valid record.
- Tighten warning classification and require ASCII decimal stale thresholds.
- Report active, stale, never, or unknown participant liveness in `state`; distinguish process polling from model responsiveness.
- Keep `PROTOCOL.md` unchanged at the 3.0 contract digest.

## 1.2.0

- Add a verified `state.local_package` identity for Git and archive installs;
  label participant `tool_version` as join-time and compare it only when the
  caller supplies its own handle and session.
- Add an optional `check_for_release.py` helper. It verifies the HTTPS origin
  before network access, fetches only the release-index branch and version tags
  when explicitly permitted, and reports stable JSON plus scheduler exit codes.
  It never installs, checks out or re-pins; the CLI remains offline.
- Document owner-triggered checks, revocable scheduled permission and an
  optional UTC cron example.
- Keep scheduled lock files out of package dirtiness, emit JSON when helper
  loading fails, constrain fetch ref updates, and prevent bytecode writes.
- Document release-check error codes, local-package reason codes, and the
  distinction between advisory level and applicability to the pinned version.
- Clarify package/protocol versioning, including protocol MINOR migration and
  package MAJOR rules.
- Keep `PROTOCOL.md` unchanged at the 3.0 contract digest.

## 1.1.0

- Add a read-only `upgrade-plan` command that derives release transitions and
  safe owner notices from locally verified release metadata; it never fetches,
  installs or re-pins.
- Add protocol-transition fields to release status and verified upgrade notices.
  Unknown or unverified claims remain explicitly unknown.
- For protocol changes, show migration readiness only after every current
  participant has an explicit valid status. Generate a canonical handoff only
  when complete; reviewers cannot be marked as staying behind.
- Return exact re-pin and migration commands as argument arrays, with validated
  channel paths and specific actionable CLI errors.
- Keep `PROTOCOL.md` unchanged at the 3.0 contract digest.

## 1.0.0

- Introduce protocol 3.0 with versioned session tool identities, explicit frozen-ledger
  migration links, actionable protocol mismatch classification, and offline peer
  advisory annotations derived only from verified local release data.
- Keep old protocol-2 channels readable with their pinned package; migration requires
  unanimous reviewer acceptance and a final DECIDE in the old ledger.
- Add tests for version table classification and fail-closed peer advisory behavior.
- Add the optional ASCII request-constraint shorthand and its usage limits.

## 0.2.0

- Add stable actionable error codes and hints, safe field/count details, and
  `send --dry-run` with no durable publication or retry-key reservation.
- Add strict release records, offline `release-status`, and derived advisory
  notices in `state` and `inbox`. The CLI does not fetch, install or re-pin.
- Add Linux/Python 3.10–3.13 CI with commit-pinned actions and pull-request
  whitespace checks, manual-only repeated stability checks, and tagged GitHub
  Releases with source archives and SHA256SUMS. The leak scan covers credentials,
  private network addresses and home paths. No provenance attestation is emitted.
- Make release checks total and offline-safe, preserve applicable notices within
  bounded state output, document every stable error code, and validate equality
  ranges, reused dry-run keys, and malformed question/advisory inputs.
- Normalize error fields to draft keys or documented CLI arguments; carry
  validator-specific detail separately in the optional `subfield`.
- Support release-archive installs: package identity and the manifest check
  inspect only the package's own Git checkout and never a parent repository;
  an archive install reports `release-status` as `not_git_checkout`. CI runs
  the suite from an extracted `git archive`.
- Document owner-consent upgrades: opt-in, read-only, revocable release-check
  permission; a fixed notice asking the owner before any upgrade; and a
  one-line bootstrap for sessions on v0.1.x.
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
