# ai-comm

A small communication protocol for explicitly activated AI sessions sharing a
local filesystem. Messages have readable prose and a short intent code. One
command validates and commits a message; incremental inbox reads avoid replaying
an entire conversation.

**Local POSIX filesystem, Python 3.10+, Git. No Python dependencies.** GitHub
hosts the tool. Your channel is a separate local bare Git repository and is never
automatically uploaded. There is no telemetry, browser dependency or background
model service.

## Activate in another session

Point the session at a pinned revision of this repository and [ACTIVATE.md](ACTIVATE.md):

Before joining, settle the checkout, channel, roles, reviewers and upgrade plan in
the [setup decisions](ACTIVATE.md#setup-decisions). It includes prompts for the
initiator and peer.

> Activate ai-comm from this repository at revision `<commit>`. Read ACTIVATE.md.
> Use channel `<absolute local path>`, handle `<my handle>`, platform `<platform>`,
> session reference `<non-secret conversation ID>`, peers `<handles>`,
> and task `<authorized task>`. Create the channel only if I asked for a new one.

The session checks the inbox while working on the authorized task. An idle or
closed model cannot be woken by a file watcher. [Protocol](PROTOCOL.md) and
[trust boundary](SECURITY.md) describe what is guaranteed.

## Start the exchange from the discovered skill

After both sessions have read this pinned package skill and the project role
table, the owner starts each session directly:

- Codex: `$ai-comm start A <subject>` and `$ai-comm start B`
- Claude Code: `/comm start A <subject>` and `/comm start B` when the project
  provides the optional comm skill link; otherwise use `/ai-comm start A
  <subject>` and `/ai-comm start B`

A performs the handshake after B joins, then opens the subject for review. These
requests never initialize a channel or take over an identity without the
owner's direct instruction in that session. Use status to inspect progress
without changing the inbox receipt and stop to stop only your own listener.
Nothing wakes a session after its host leaves it idle.

## Two-session quick start

Clone and pin the package with Git. Choose a new, dedicated channel path outside
the package checkout. The path's parent must already exist.

```sh
git clone https://github.com/amazarache/ai-comm.git
cd ai-comm
git checkout <reviewed-commit>
python3 ai_comm.py --channel /tmp/example-channel init \
  --task 'Review a design together' --reviewers author reviewer
python3 ai_comm.py --channel /tmp/example-channel join \
  --as author --platform codex --session author-conversation
python3 ai_comm.py --channel /tmp/example-channel join \
  --as reviewer --platform claude --session reviewer-conversation
```

Each actual session should normally perform its own join. Session references are
provenance, never passwords or login tokens. Handles are lowercase ASCII slugs.

Send a draft. Reuse the same retry key after an uncertain result; a changed draft
with that key is rejected. Choose a new key for a new message.

```sh
python3 ai_comm.py --channel /tmp/example-channel send \
  --as author --session author-conversation --key first-review \
  --draft examples/ask.json
python3 ai_comm.py --channel /tmp/example-channel inbox \
  --as reviewer --session reviewer-conversation
```

After reading the returned page, acknowledge its `through` value:

```sh
python3 ai_comm.py --channel /tmp/example-channel ack \
  --as reviewer --session reviewer-conversation --through 1
python3 ai_comm.py --channel /tmp/example-channel wait \
  --as reviewer --session reviewer-conversation --timeout 30
```

`inbox` does not acknowledge automatically. It starts from the previous receipt,
including messages sent while the reader was absent. Acknowledgements cannot
move backwards or skip beyond the scanned page. There is no topic filter that
silently consumes other topics.

A `wait` without `--new-since` returns the same page as `inbox`, including its
`through` value, as soon as an unacknowledged message is available or the
timeout passes. You do not need a separate `inbox` call after it. Read the page,
then acknowledge its `through` value explicitly, as above.

### Watch without repeating notifications

An optional caller-owned cursor suppresses repeat alerts without consuming unread
messages. Use this in a watcher, separately from the model's inbox and receipts:

```sh
python3 ai_comm.py --channel /tmp/example-channel wait \
  --as reviewer --session reviewer-conversation --timeout 30 --new-since 0
```

After handling each notification page, pass its `notification_through` on the next
call. Follow `more` to drain additional pages. Notification mode has no `through`
field and never changes the receipt's `read` or `seen` boundary: the model must
still read `inbox` before it can acknowledge that page. A timeout can return a
new notification cursor after scanning messages addressed to other participants.
Negative or future cursors fail. Already acknowledged messages are not alerted.

Keep this cursor per watcher and session generation; reset it to zero after a
takeover. Losing it or restarting at zero replays unread alerts safely. Only
advance it after handling the returned page; a cursor alone cannot guarantee
exactly-once downstream processing. Default `wait` and `inbox` still return every
unacknowledged message. These options do not wake an idle model.

## Message intent and evidence

Acts are `PING`, `ACK`, `PONG`, `ASK`, `FACT`, `CORRECT`, `PROPOSE`, `REVIEW`,
`ACCEPT`, `REJECT`, and `DECIDE`. The act describes intent; `summary` and optional
`body` remain ordinary prose. Summary limit: 300 characters. Long bodies produce
a warning at 2,000 characters; use a published artifact for detailed analysis. The
warning reports the Python `len` count (Unicode code points) and gives the exact
artifact publication steps. See [structured errors](docs/ERRORS.md).

Validate a draft without publishing it:

```sh
python3 ai_comm.py --channel /tmp/example-channel send \
  --as author --session author-conversation --key first-review \
  --draft examples/ask.json --dry-run
```

The response says `dry_run: true` and `published: false`. Any proposed sequence,
id, timestamp, or nonce is provisional; a real send can still fail if the channel
changes before it runs. Dry-run does not publish, update receipts, reserve the
retry key, or durably allocate a nonce.

`CORRECT` must name an earlier `reply_to`. Optional confidence on `FACT`,
`CORRECT`, and `DECIDE` is `verified`, `likely`, or `unverified`. A channel can
require it and supporting sources when initialized:

```sh
python3 ai_comm.py --channel /tmp/evidence-channel init \
  --task 'Audit measurements' --reviewers author reviewer \
  --require-confidence FACT CORRECT --require-sources FACT CORRECT
```

Declare a source once in `sources`, as `{id, kind, ref, detail}`, then cite its
ID in `refs`. A source ID cannot be repurposed. Questions use structured
`{op: "open", id, text}` / `{op: "close", id}` events. Registries are derived
from committed messages. The tool does not fetch references or execute content.

Dense claim shorthand was tested and rejected: it barely reduced bytes beyond
compact prose and introduced meaning errors. See [experiment](docs/EXPERIMENT.md).

## Liveness and agreement are different

The first handshake uses three durable messages: `PING`; a replying `PONG`
which acknowledges the ping and supplies a new nonce; then a final `ACK`.
The tool generates and validates the nonce fields. Each reply names `reply_to`
and addresses the preceding sender. Routine read receipts do not create commits.

`inbox`, `ack`, `wait`, successful `send`, and identity-bearing `state` also write a passive heartbeat under `ai-comm-runtime/poll/`. It never enters Git or receipts and proves only that a process using that identity polled; it does not prove the model read or acted. `state` reports each participant as `active`, `stale`, `never`, or `unknown`; the default stale threshold is 300 seconds and can be changed with `--stale-after` (1–86400). A completed PING handshake still tests model-level responsiveness. A `wait` records one heartbeat when the bounded wait completes, so it reflects the completed poll rather than its start. This is a deliberate timing choice: it differs from sampling immediately after authentication. To recover from a forged or skewed future record, a stored timestamp more than five seconds ahead of the observed clock is replaced only by a fresh sample (no more than five seconds old); a delayed older sample never erases a later valid record. Heartbeat writes are best effort and may add a warning without changing a successful command result.

For adoption, publish an exact candidate manifest or specification:

```sh
python3 ai_comm.py --channel /tmp/example-channel artifact \
  --as author --session author-conversation --file candidate-manifest.json
```

Use the returned SHA-256 as the `artifact` in a `PROPOSE` draft. A reviewer
reads the exact published bytes before deciding:

```sh
python3 ai_comm.py --channel /tmp/example-channel show-artifact <sha256>
```

Every required reviewer sends `ACCEPT` or `REJECT`, replying to that proposal and naming the same
digest. `DECIDE` requires the latest stance of every configured reviewer to be
ACCEPT. An ACK, a different digest, or an unresolved rejection does not qualify.
After DECIDE, further changes require a new proposal. No code is auto-executed.

For code review, the artifact should list **all** proposed relative paths and
their hashes, including tests and activation docs. Independently compare the
checkout with the manifest before accepting; a hash alone proves neither quality
nor the authenticity of the writer.

## Inspect and resume

```sh
python3 ai_comm.py --channel /tmp/example-channel state
python3 ai_comm.py --channel /tmp/example-channel log --after 0 --limit 20
python3 ai_comm.py --channel /tmp/example-channel show 00000001-author
python3 ai_comm.py --channel /tmp/example-channel show-artifact <sha256>
python3 ai_comm.py --channel /tmp/example-channel registry questions
python3 ai_comm.py --channel /tmp/example-channel validate
python3 ai_comm.py --channel /tmp/example-channel render --out history.md
```

STATE output is bounded to 8 KiB with visible overflow counts. Deep inspection
is paginated. Rendering expands acts, preserves original wording and resolves
source/question references; it never applies later corrections to old text.
Each source's details appear once per export; later citations name the message
where those details appeared. A partial `render --since N` includes full details
at the first retained citation, even if the original declaration predates N.
An existing output file is not overwritten. The render is not a second ledger.

An owner-authorized handover uses `join --takeover` with a new session reference.
It appends a generation and retains history; the previous session cannot send
or acknowledge afterward. The new session reads STATE and resumes the role's
receipt cursor. Existing version-1 logs remain in their original repository;
link them in the project's activation note rather than rewriting history.
Takeovers require a session reference never previously used for that handle;
changing the platform label alone cannot reactivate an old reference.

### Release compatibility

| Package releases | Protocol contract | Channels they can open |
|---|---|---|
| `0.1.0`–`0.1.3` | `2.0` (`709af4b0…`) | Channels whose stored protocol digest matches the full bundled protocol digest. |
| `0.2.0` | `2.0` (`709af4b0…`) | Existing v2.0 channels; protocol bytes remain unchanged. |
| `1.0.0` | `3.0` (see the bundled `PROTOCOL.md`) | New protocol-3 channels, or migrated channels linked to a frozen protocol-2 ledger. |
| `1.1.0` | `3.0` (unchanged from `1.0.0`) | Existing protocol-3 channels; migration planning is additive. |
| `1.2.0` | `3.0` (unchanged) | Existing protocol-3 channels; local package identity and optional release discovery are additive. |
| `1.3.0` | `3.0` (unchanged) | Existing protocol-3 channels; passive runtime liveness reporting is additive. |
| `1.4.0` | `3.0` (unchanged) | Existing protocol-3 channels; adds a project-local, agent-neutral session skill and owner-gated offline upgrade planning. |
| `1.4.1` | `3.0` (unchanged) | Existing protocol-3 channels; adds an owner-approved, opt-in credential-helper path for private-origin release checks. |
| `1.4.2` | `3.0` (unchanged) | Existing protocol-3 channels; adds skill-guided start A/B, status, and stop flows, host-specific invocation, and notifier-based release awareness with durable prompt deduplication. |

Package and protocol versions are related but distinct. A protocol MAJOR (for
example 3.0 to 4.0) always bumps the package MAJOR. A protocol MINOR (3.0 to
3.1) bumps the package MINOR, but still requires a linked-channel migration
because channels pin exact protocol bytes; migration by itself does not make a
package release MAJOR. “Breaking” means incompatible behavior in an existing
package contract: CLI arguments or behavior, output fields, stored formats, or
error codes. Such CLI or storage changes bump package MAJOR even when the
protocol is unchanged. Compatible additions bump MINOR; docs and fixes bump
PATCH. Every release should update this table and its changelog entry.

Channel compatibility is pinned to the exact PROTOCOL.md digest, including its
wording. Keep using the accepted package revision for an existing channel. A
changed contract, even a prose correction, currently requires an explicitly
initialized new channel with a reference to the old ledger. Version 0.2 adds
offline release awareness. `release-status` reads only already-fetched local Git
refs; the CLI never fetches or contacts a URL. The optional
`scripts/check_for_release.py` helper fetches only after explicit permission and
can report through a user-configured schedule; it never installs or re-pins.
`state` reports `local_package`; pass both `--as HANDLE --session REF` to show a
join-time versus running-version note for that exact session. See
[release awareness](docs/RELEASES.md) for fetch and advisory rules. GitHub
Actions checks Linux with Python 3.10–3.13; repeated stability checks are
manual-only. Tagged releases
include a source archive and SHA256SUMS, with notes from the matching changelog
section. This private repository has no provenance attestation and makes no
publisher-authenticity claim. Release checks read committed HEAD; partial clones
on older Git versions receive no advice rather than causing a lazy network fetch.

`upgrade-plan` turns verified local release metadata into a reviewable plan. It
does not fetch, install, re-pin, or modify a channel. It prints Git and test
commands as argument arrays. If protocol bytes changed, pass `--channel` to
inspect participants and optionally provide each participant's explicit status
with `--participant HANDLE=will_upgrade` or
`--participant HANDLE=stays_behind`. Until every current participant has a valid
status, the migration remains incomplete and no handoff is produced. The owner
still authorizes any fetch or re-pin, and channel migration requires the old
channel's accepted and decided handoff. See [release planning](docs/RELEASES.md#read-only-upgrade-planning).

### Optional symbolic request constraints

For compact requests, sessions may use this fixed ASCII legend in message bodies
or artifacts: `->` means “for/aimed at”, `+` means “include”, `&` means “and”,
`!` means “not”, `<=` and `>=` are inclusive bounds, `xN` means exactly N,
and `::` means “set to”. For example,
`explain(mTLS) -> junior engineer + analogy x1 & ascii_diagram & pitfalls <= 3`.
This shorthand describes requested outputs only. Keep facts, evidence, units,
uncertainty, decisions, consent and safety settings in ordinary language. Treat
unknown or ambiguous symbols as a request to clarify; the message envelope and
consensus rules do not change.
An extracted source archive runs and passes its tests without Git metadata;
`release-status` then reports `not_git_checkout`. Sessions discover releases only
with the owner's opt-in fetch permission and ask the owner before any upgrade
([ACTIVATE.md](ACTIVATE.md) section 6).

## Durability and limits

A channel lock serializes publication; Git objects are prepared with a temporary
index, then an atomic compare-and-swap advances `refs/heads/main`. Readers see
only committed snapshots. A killed writer cannot publish half a message.
Idempotent retries prevent duplicate publication, not duplicate task execution.

The first release bounds drafts to 64 KiB, envelopes to 128 KiB, text artifacts
to 1 MiB and a loaded channel snapshot to 64 MiB. It revalidates the snapshot on
each operation; it is designed for small review/work sessions, not high-volume
telemetry. Archive explicitly before that bound. No remote synchronization,
network-filesystem locking or Windows support is claimed.
Every snapshot read visits all messages, so cost grows at least linearly with
history and content size; source/question-heavy or agreement-heavy histories can
add more validation work. In the synthetic 3,000-message benchmark, median full
snapshot validation improved from 0.736 s to 0.570 s for ASCII prose, and from
0.759 s to 0.675 s for multilingual prose. These are local warm-filesystem
measurements, not latency guarantees. [Method and reproduction](docs/PERFORMANCE.md)
describe the fixture and limitations. Full history and integrity checks remain;
there is no persistent cache or index that can bypass validation.
Text accepts UTF-8 prose but rejects invisible Unicode format/bidi controls and
Unicode line/paragraph separators; use ordinary LF for multiline text. This also
excludes zero-width joiners. Stored artifacts are revalidated on every read.

## Validation

```sh
python3 -m unittest discover -s tests -v
```

Tests exercise real Git transactions, concurrent processes, a killed writer,
retry deduplication, restart/backlog, stale sessions, receipt corruption, evidence
and consensus constraints, safe rendering, and an unrelated project's index.
