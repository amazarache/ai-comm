# Offline release awareness

The communication CLI never fetches or contacts a URL. To refresh release
awareness, an operator must explicitly fetch the named refs in the package
checkout and then run `release-status`:

```sh
git -C .tools/ai-comm fetch origin refs/heads/release-index:refs/remotes/origin/release-index
git -C .tools/ai-comm fetch origin 'refs/tags/v*:refs/tags/v*'
python3 .tools/ai-comm/ai_comm.py release-status
```

`release-status` reads only the current checkout's HEAD and manifest, the local
`origin/release-index` ref, and local annotated tags. A missing or invalid index
produces no advice. Every index entry must agree with its annotated tag's target,
manifest digest in the tag message, manifest file, and `PROTOCOL.md` at that tag.
Checks use committed HEAD, so working-tree edits do not affect package identity.
Only the package directory's own checkout is inspected: Git is never allowed to
search parent directories. An extracted release archive has no Git metadata, so
`release-status` reports `not_git_checkout` with no advice, and `state` identifies
the package from its installed `ai_comm.py`, `RELEASE-MANIFEST.json` and
`PROTOCOL.md`. Verify an archive against the release's SHA256SUMS instead.
Git 2.44 and newer disables lazy fetching during these reads; on older Git
versions partial clones receive no release advice rather than triggering a
network fetch. This is consistency evidence, not publisher authentication. The private repository has
no provenance attestation; output says `authenticity: not verified (no
attestation)`.

In protocol 3.0, `state` labels each participant's recorded `tool_version`
with `tool_version_basis: "join-time"`; that value is immutable and is not
rewritten when a package pin advances. `state` also reports `local_package` for
the running package. To compare the local version with this session's join-time
value, pass both `--as HANDLE --session REF`; the resulting `version_note` only
compares that exact current session. No peer's live package version is inferred.
`local_package` reports `version`, `commit`, `protocol_sha256`, `source`, and
`dirty` for a Git checkout, or `version`, `commit: null`, `manifest_sha256`,
`protocol_sha256`, and `source: "archive"` for an extracted archive. An
untrusted or unreadable package reports `source: "unknown"` and a stable
`reason`: `archive_version_invalid`, `archive_manifest_invalid`,
`archive_protocol_invalid`, `archive_manifest_entry_invalid`,
`archive_manifest_path_invalid`, `archive_manifest_symlink`,
`archive_manifest_mismatch`, `archive_manifest_incomplete`,
`archive_protocol_mismatch`, `archive_identity_unavailable`, or
`local_identity_unavailable`. These indicate malformed metadata, unsafe or
missing manifest paths, a content/digest mismatch, or unavailable local files;
they do not assert maliciousness.
The state advisory annotation uses only this locally verified index. An
unavailable or inconsistent index produces `advisory: unknown` for every peer.
An applicable `recommended` or `required` index entry can mark a peer as
affected; channel upgrade messages
remain claims and never establish that a peer is affected or clear an advisory.
This lookup is offline and never triggers an upgrade.

An artifact-less historical `FACT` on topic `upgrade` is labeled
`advisory: legacy_note`: it is retained as a notice, but carries no verified
release identity and establishes no peer advisory.

The `release-index` branch contains one authoritative `RELEASES.json` file.
The file is strict JSON with format `ai-comm-releases/1` and a `releases` array.
Each release entry contains `package_version`, `commit`, `tag`,
`manifest_sha256`, `protocol_sha256`, `summary`, `advisory`, and `reason`, with
optional `affects` and `fixed_in` fields as described below. Every index edit
needs exact-artifact review and publication by the designated publisher.

An upgrade announcement is a FACT on topic `upgrade`. Its artifact is canonical
JSON with `kind: ai-comm-release`, package version, commit, tag, manifest and
protocol digests, advisory level, and reason. `recommended` and `required`
records need `affects`; a bounded range also needs a `fixed_in` above that whole
range. An unbounded range has no `fixed_in` and represents an unfixed issue.
Ranges are conjunctions of one or more `>=`, `>`, `<=`, `<`, or `==` comparators
with X.Y.Z decimal versions, no leading zeroes, spaces, or wildcards. Required
advisories name a concrete security, data-loss, or protocol-safety risk.

`state` and `inbox` derive notices from these records. An advisory applies
exactly when the local version matches `affects`; `fixed_in` is displayed as an
upgrade target and never clears the advisory. Older release announcements are
neutral as version signals, while any matching advisory remains visible. Newer
optional notices cannot hide an applicable required advisory. A notice never
installs, fetches, or re-pins a package.

Fetching the index is opt-in: the owner grants an explicit, read-only, revocable
permission limited to the `release-index` branch and `v*` tags. A session asks
its owner before any upgrade, using the notice template in ACTIVATE.md section 6.

## Optional scheduled discovery

`scripts/check_for_release.py` is separate from `ai_comm.py`; the communication
CLI remains offline. Without `--fetch-permitted`, the helper reads only the
already-fetched local refs. With that flag, it first compares `origin` to the
package's manifest-bound `RELEASE_ORIGIN` constant, or to an explicit owner
`--expected-origin` override. Only HTTPS URLs without credentials, query
parameters or fragments are accepted. HTTPS is intentionally the only accepted
scheme: it keeps the fetch source narrow and auditable; SSH, file and local
origins may be considered later as an explicit opt-in. Comparison ignores only
a trailing slash and `.git`. A mismatch or missing origin is rejected before
any fetch.

The permitted fetch is exactly the `release-index` branch and `v*` tags, with
implicit tag following and submodule recursion disabled. It runs in a fresh,
temporary bare Git directory that shares only the checkout's object store. The
helper uses the manifest-approved HTTPS URL directly and atomically publishes
only the two permitted ref patterns into the checkout; it does not read the
checkout's fetch, proxy, URL rewrite, redirect, or credential settings for the
network operation. It does not check out files or change the working tree or
`FETCH_HEAD`. `GIT_NO_LAZY_FETCH=1` is set. A failed fetch or ref publication is
an error, never an up-to-date result. The helper validates the fetched data
through `release-status` and runs `upgrade-plan` only when a newer verified
release exists. Redirects are refused for the verified origin and Git HTTP
request paths. It emits one JSON result on operational success
and failure; errors never include raw Git stderr, credential-helper values,
usernames, passwords or URL credentials.

For a private origin, before asking permission, disclose that Git may call the
configured helper's `store` operation after success or `erase` after an
authentication failure. A store helper may write a credential to a store such
as `~/.git-credentials`; after an authentication failure, erase removes the
rejected credential, which can be valid after a transient 401. Then ask:

> Refresh release refs from `<origin>`, using your configured git credential helper for that fetch only?

After an explicit yes, add `--use-git-credential-helper`. This reads only global
`credential.helper` values using the user's normal `HOME`/`XDG_CONFIG_HOME`
resolution, then passes those helper entries to this single verified-origin
fetch through Git's environment-based config interface, keeping helper values
out of the process argument list. The temporary fetch disables system and
global config, then adds only the approved helpers and redirect limits. Empty
helper entries keep their reset semantics. If no effective helper is configured,
or the values cannot be read unambiguously, the helper stops without fetching.
Configured helpers are executable programs running with the user's rights. The
release-check tool does not itself write credentials, change the pin, or
authorize Gate 2.

The JSON schema is `ai-comm-release-check/1`. `result` is `up_to_date`,
`update_available`, `advisory_applies` or `error`; `status` carries the release
index status; `fetched` means a fetch was attempted (so a partial failure is
visible); `fetch_error` is a stable error code or null; `pinned` and `latest`
are package versions (`latest` is null unless the index is valid);
`channel_action` is the verified protocol transition or null; `advisory` is
`none`, `optional`, `recommended` or `required`;
`advisory_applies_to_pinned` indicates whether a recommended or required notice
matches the pinned version; and `owner_notice` contains the upgrade-plan notice
when a newer verified release exists. A newer release may carry a `recommended`
or `required` advisory that does not apply to the current pin; that remains
`update_available` with exit `10` and `advisory_applies_to_pinned: false`. Do
not infer applicability from the `advisory` value alone.

`fetch_error` is null on success and otherwise one of these stable codes:
`invalid_arguments`, `package_unavailable`, `package_identity_invalid`,
`package_manifest_mismatch`, `package_origin_unavailable`, `not_git_checkout`,
`invalid_origin`, `credential_bearing_origin`, `origin_missing`,
`origin_mismatch`, `credential_helper_requires_fetch_permission`,
`credential_helper_not_configured`, `credential_helper_config_unreadable`,
`auth_failed`, `origin_redirect_refused`,
`fetch_failed`, `index_missing`, `index_invalid`,
`index_inconsistent`, `index_tags_missing`, `release_status_failed`,
`release_status_invalid_json`, `release_status_invalid_shape`,
`release_status_invalid_version`, `upgrade_plan_failed`,
`upgrade_plan_invalid_json`, `upgrade_plan_invalid_shape`, and
`operation_failed`. The codes identify a failed validation or subprocess stage;
output never includes the raw subprocess error. Git may omit the HTTP status
from an authentication error; in that case classification is conservatively
`fetch_failed` rather than `auth_failed`.

Exit status is `0` for up to date, `10` for an update available, `11` when a
recommended or required advisory applies to the pinned version, and `2` for an
invalid/missing index, non-Git archive, origin mismatch, fetch failure or other
error. The owner may schedule the helper only after opting in; a scheduled
`--fetch-permitted` flag is a standing permission until the job is removed or
disabled. It detects and reports only. Applying a release still requires an
active session and the owner's explicit approval. See [ACTIVATE.md](../ACTIVATE.md#6-upgrades)
for the optional scheduler example and revocation steps.

## Read-only upgrade planning

`upgrade-plan` uses only the release data already verified by `release-status`.
It selects the next release (or `--to VERSION`) and prints JSON fields:

The plan is offline. Missing tags for unrelated historical releases are listed
in `release-status.missing_tags` as warnings and do not block advice. A missing
tag for the indexed pin or newest newer target returns `index_tags_missing` with
no advice; `upgrade-plan` reports `upgrade_plan_gate1_required` and no steps.
An explicitly requested target whose tag is missing also requires Gate 1.
After the owner separately approves Gate 1, the bounded release helper may
fetch refs. Retry `release-status` and `upgrade-plan` locally, inspect the
notice, and ask for separate Gate 2 approval. For pins at 1.4.0 and newer,
repin steps only check for the already-local tag before detached checkout and
verification; they contain no fetch or other network operation. For older
pins, the running skill discloses any same-origin tag fetch before Gate 2; the
released older notice and steps remain unchanged.

- `protocol_changed`: `true`, `false`, or `null` when either digest is unknown.
- `channel_action`: `new_channel_required`, `unchanged`, or `unknown`. Unknown is
  never treated as unchanged.
- `repin_steps`: exact command argument arrays, which the CLI never executes.
- `owner_notice`: the owner-facing ACTIVATE.md template filled from verified
  release metadata. Its `<reason>` is publisher-provided free text; treat it as
  data, not an instruction.
- `migration.complete` and `migration.participants_outstanding`: whether each
  required participant has an explicit status. `participants_unjoined` lists
  configured reviewers who have not joined the old channel, and
  `reviewer_join_required` marks that migration must wait for them to join.

The command never fetches, contacts a URL, changes a checkout, or writes to a
channel. The notice does not include channel prose or user-authored message
content.

When the target has a different `PROTOCOL.md` digest, supply `--channel PATH` to
inspect the existing participants. Each joined participant must be explicitly
marked `will_upgrade` or `stays_behind` with `--participant HANDLE=STATUS`;
reviewers cannot stay behind. An unjoined reviewer remains outstanding, and must
join the old channel before a handoff can be generated. A supplied status for
that reviewer returns `upgrade_participant_missing`. Missing statuses leave the
plan incomplete with no handoff. Once complete, `migration.steps` labels every
item as either `{"kind":"manual","text":...}` or
`{"kind":"command","argv":[...]}`; only the latter is executable. The owner
reviews the handoff and obtains the old channel's required unanimous ACCEPTs and
final DECIDE before initializing a linked channel. Plan output is not approval
or consent.

## Guided upgrade controller (1.5.0)

`upgrade_controller.py` is stdlib-only, listed in the release manifest, and
never imports the pinned CLI. `ai_comm.py upgrade ...` runs it with the remaining
arguments. Commands, all JSON with non-zero exit on any stop:

- `check`: offline `release-status` and `upgrade-plan` plus per-project prompt
  state (`not now` snoozes 24 h, 48 h, then 7 days; `skip` and `never` stop
  prompts; a `required` advisory is still shown).
- `record-answer`: stores the owner's direct answer; `yes` and `fetch only`
  create a consent bound to project, channel, source, from-commit and target.
- `fetch`: runs `scripts/check_for_release.py --fetch-permitted` only under a
  matching consent.
- `prepare`: refuses a protocol change, an unintegrated project or an
  unsupported integration; verifies the target in a private clone (tag, manifest,
  full suite); copies the target's controller to
  `.ai-comm-local/upgrade/controller/<sha256>/`; freezes identities; writes a
  guidance proposal listing exact references to lines in declared guidance files
  that name the old pin, plus the transaction and hash marker lines (not an
  applicable diff; it edits no guidance).
- `apply`, `continue`, `rollback`, `recover`, `report`, `finalize`, `status`.

The pin location is fixed at the project's `.tools/ai-comm`; consent and each
transaction bind the canonical project, pin and channel paths and the channel
root commit. The applier's handle and session are frozen at `prepare` and
re-validated for every mutation. Consent is validated and consumed before the
first live action of any owner. Rollback first resolves unfinished steps by
probe, or stops with `peer_reconcile_required` for a peer's step. Finalize
re-verifies the live state and requires a fresh attestation from each
peer-owned integration.

State lives only in the project's `.ai-comm-local/upgrade/`: private (0700/0600)
and user-owned. The journal is hash-chained JSON Lines with fsync. An
unterminated final line is preserved in `torn-<seq>.bin` and chained by a note;
any complete invalid line is `journal_corrupt` and is never repaired. Each side
effect is preceded by an intent and followed by an outcome; an intent without
an outcome is resolved only by a read-only probe. A project lock admits one
applier.

## New-project installer (1.5.0)

`install.sh --project DIR --version X.Y.Z --fetch-permitted
[--use-git-credential-helper]` runs `scripts/install_project.py` from an already
verified package: a clean Git checkout whose release manifest checks, or an
extracted release archive that the owner checked against the release's
`SHA256SUMS` and whose every manifest entry and protocol digest verify. With the owner's
direct consent (`--fetch-permitted`) it fetches exactly `refs/tags/vX.Y.Z` and the
release index from that package's manifest-bound `RELEASE_ORIGIN`, through the
same isolated, redirect-refusing fetch as `check_for_release.py`, into a private
staging repository under `DIR/.ai-comm-local/install-<id>/`. It verifies the
selected index record against the annotated tag (commit, manifest, protocol),
runs `check_tag`, `check_release_manifest` and the full suite, removes build
debris and requires the exact tag tree again, then publishes the staging
repository atomically as `DIR/.tools/ai-comm` through directory handles opened
without following symlinks. Installing a version older
than the newest indexed one reports partial release awareness instead of fetching
more tags. It writes `.ai-comm-local/upgrade/project.json` as `unintegrated`,
appends only missing `.tools/` and `.ai-comm-local/` lines to `.gitignore`, and
never initializes, joins or sends to a channel. It refuses an existing pin or
upgrade state, a symlinked path, unsafe ancestry and a project inside `.tools/`.
After an interruption, `install.sh --project DIR --recover` touches only private
staging directories holding a valid install journal for this project; it removes
unpublished staging, completes an install whose verified pin was already published, and
otherwise only reports.

## Per-user cache, pin verifier and entry skill (1.6.0)

`install.sh --user <command>` runs `scripts/install_user.py`; [INSTALL.md](INSTALL.md)
is the user guide. The cache lives in `$XDG_DATA_HOME/ai-comm` (default
`~/.local/share/ai-comm`), private (0700/0600), with immutable
`releases/X.Y.Z/` checkouts, atomically replaced `current` and `previous` links,
`state.json`, `bindings.json`, `index.json`, `refresh.json`, `offer.json`, private
staging and journal directories and a single-writer lock. Changing the cache
never changes a project's pin, channel, declaration, listener or guidance.

- `install --version X --fetch-permitted` shares its verification with the
  project installer (`scripts/release_install.py`): exactly `refs/tags/vX` and the
  release index from the manifest-bound origin, the index record checked against
  the annotated tag, and, when the running installer is an extracted archive of
  `X`, its manifest digest equal to both; then `check_tag`, the manifest check,
  the full suite and the exact tag tree, before an atomic publish. The cache
  records its origin at the first install; another origin stops with
  `origin_changed`.
- Trusted bindings (`version`, `tag`, `tag_object`, `commit`, `manifest_sha256`,
  `protocol_sha256`) come only from the cache's own verified fetches: `install`
  binds its own tag; a consented `check --fetch-permitted` rebuilds them for every
  indexed tag it fetched, keeping an older binding only while it still equals the
  refreshed record. Offers require exact agreement with the index. `check`
  reports `verdict` (`update_available`, `up_to_date`, `unknown`) and `metadata`
  (`refreshed`, `cached`, `stale`, `failed`, `missing`); `up_to_date` needs a
  refresh within seven days that is not dated in the future and no failed
  attempt since.
- A cached release is checked before use by `release_install.verify_tree`: the
  recorded commit's commit and tree objects are re-hashed, and every working
  file is compared by path, blob hash and executable bit, with no extra file;
  index flags, HEAD and `git status` are not consulted.
- Every multi-object change is journaled (intent before, outcome after,
  `state.json` last). An unfinished install, activation, uninstall, registration
  or launcher change blocks every new mutation (`operation_pending`) until
  `recover`, which resumes a step only from the exact state it left, marks a
  stale one `superseded`, and resolves what it cannot prove is its own as
  `abandoned` while keeping it. `uninstall` removes only exactly verified
  releases, journal-recognized operation directories and unchanged links and
  launcher; everything else is kept and reported.

`scripts/verify_pin.py --project DIR` checks a project's `.tools/ai-comm` from a
cached release and runs nothing from the pin. Git is used only for inert reads
against the pin's object store (`--git-dir`, no work tree or index, no
replacement objects, every transport disallowed through `GIT_ALLOW_PROTOCOL` and
`protocol.allow=never`, `GIT_NO_LAZY_FETCH=1`); a pin with alternates or any
partial-clone or promisor setting in its effective configuration is `unknown`
before any object is read. HEAD must equal a trusted binding's commit, a pin tag
`vX` must equal the binding's tag object, the committed manifest must hash to the
binding's digest, the tree's paths minus the manifest must equal its entries, and
every entry's working bytes (opened component by component without following
links) must match its size, digest and executable bit. The class names the
upgrade path: `controller` (1.5.2 and later), `controller-check-workaround`
(1.5.0, 1.5.1), `legacy-manual` (1.4.0 to 1.4.2), `unsupported`. Untracked files
in the pin are outside the check.

`scripts/declare_project.py` writes `.ai-comm-local/upgrade/project.json` for a
verified pin: `init` writes the unintegrated declaration and never overwrites;
`install --file F --sha256 H --previous P` validates F's exact bytes with the
controller's pure `validate_project_manifest` against the live project identity,
then, under the project's upgrade lock, refuses a guided transaction in progress
and a live file whose digest is not `P` before replacing it atomically. Nothing
the declaration names is run.

`entry/ai-comm/SKILL.md` is the per-user entry skill; `register --skills-dir DIR`
links `DIR/ai-comm` to it only if no other entry in `DIR` or in a directory
registered earlier has the effective skill name `ai-comm`, read from bounded,
inert `SKILL.md` front matter (front matter it cannot read exactly counts as a
conflict). `launcher --bin-dir DIR` writes `DIR/ai-comm`, which resolves
`current` once and runs that release's `scripts/project_cli.py`; it checks its
own release first and replaces itself with the project's `ai_comm.py` only for a
verified pin. While links or a launcher exist, `activate`, `rollback` and an
activating `install` refuse a release that lacks what they reach. `offer claim`
makes one session the notifier of the cache's pending offer (a claim lapses after
24 hours or when a newer release is offered); `offer answer` records the owner's
answer and lists exactly what it permits.

## Delivery status (1.7.0)

`outstanding` is read-only and content-free; see the README section "Delivery
status of your own messages". It needs no upgrade step of its own: a project
adopts 1.7.0 by the guided controller like any MINOR release, and nothing in a
channel changes. A project-local watcher can use it to report a peer's overdue
messages, but the package itself starts no listener and sends nothing. The
manual stability workflow is split into parallel jobs with the same repetition
counts; Gate A requires its `all-jobs` check to succeed.

## Automation

`ci.yml` runs on main, candidate branches, and pull requests against Linux and
Python 3.10–3.13. It validates the release manifest and protocol digest, runs the
test suite, runs the suite again from an extracted `git archive` with no Git
metadata, and scans tracked source files for credential patterns, private
network addresses, and home paths. CI provides evidence only; it does not ACCEPT
or DECIDE a release. `stability.yml` is manual-only and runs 20 full-suite
repetitions (four parallel shards of five) plus 200 runs each of two focused
race tests, in separate jobs; its `all-jobs` check succeeds only when every job
succeeded. `release.yml` checks pushed tags,
then creates a GitHub Release with the source archive, SHA256SUMS, and matching
`CHANGELOG.md` section. Manual dispatch verifies an existing tag without
publishing. The workflow force-fetches the annotated tag before checking it.
Only its package job gets `contents:write`; no extra action is used to publish.
It does not publish a provenance attestation or claim publisher authenticity.
