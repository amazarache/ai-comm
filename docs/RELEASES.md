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
implicit tag following and submodule recursion disabled. `GIT_NO_LAZY_FETCH=1`
is set. Git may update those remote/tag refs and `FETCH_HEAD`; the helper does
not check out files or change the working tree. A failed partial fetch is an
error, never an up-to-date result. The helper validates the fetched data through
`release-status` and runs `upgrade-plan` only when a newer verified release
exists. It emits one JSON result on operational success and failure; errors
never include raw Git stderr or URL credentials.

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
`origin_mismatch`, `fetch_failed`, `index_missing`, `index_invalid`,
`index_inconsistent`, `index_tags_missing`, `release_status_failed`,
`release_status_invalid_json`, `release_status_invalid_shape`,
`release_status_invalid_version`, `upgrade_plan_failed`,
`upgrade_plan_invalid_json`, `upgrade_plan_invalid_shape`, and
`operation_failed`. The codes identify a failed validation or subprocess stage;
output never includes the raw subprocess error.

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

The plan is offline. If the verified index refers to a tag absent from the
local checkout, `release-status` returns `index_tags_missing` with no advice;
`upgrade-plan` returns `upgrade_plan_gate1_required` and no steps. After the
owner separately approves Gate 1, the bounded release helper may fetch refs.
Retry `release-status` and `upgrade-plan` locally, inspect the notice, and ask
for separate Gate 2 approval. For pins at 1.4.0 and newer, repin steps only
check for the already-local tag before detached checkout and verification; they
contain no fetch or other network operation. Older pins may require a same-
origin tag fetch after Gate 2 approval, as the notice must disclose.

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

## Automation

`ci.yml` runs on main, candidate branches, and pull requests against Linux and
Python 3.10–3.13. It validates the release manifest and protocol digest, runs the
test suite, runs the suite again from an extracted `git archive` with no Git
metadata, and scans tracked source files for credential patterns, private
network addresses, and home paths. CI provides evidence only; it does not ACCEPT
or DECIDE a release. `stability.yml` is manual-only and runs 20 full-suite
repetitions plus 200 focused race-test runs. `release.yml` checks pushed tags,
then creates a GitHub Release with the source archive, SHA256SUMS, and matching
`CHANGELOG.md` section. Manual dispatch verifies an existing tag without
publishing. The workflow force-fetches the annotated tag before checking it.
Only its package job gets `contents:write`; no extra action is used to publish.
It does not publish a provenance attestation or claim publisher authenticity.
