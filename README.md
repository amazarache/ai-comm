# ai-comm

A small communication protocol for explicitly activated AI sessions sharing a
local filesystem. Messages have readable prose and a short intent code. One
command validates and commits a message; incremental inbox reads avoid replaying
an entire conversation.

**Local POSIX filesystem, Python 3.10+, Git. No Python dependencies.** GitHub
hosts the tool. Your channel is a separate local bare Git repository and is never
automatically uploaded. There is no telemetry, browser dependency or background
model service.

## Install

To install a release for your user (no root; nothing outside the per-user cache):

```sh
curl -fsSL --proto =https --proto-redir =https --tlsv1.2 \
  https://github.com/amazarache/ai-comm/releases/download/vX/install-ai-comm.sh | sh
```

[docs/INSTALL.md](docs/INSTALL.md) has the verify-first variant (recommended), the bootstrap digests, what this
trusts, and the manual steps. A project's own pinned copy is installed separately with `install.sh --project`.

## How any agent uses ai-comm

ai-comm works the same way for every AI agent and every host program that runs
one. An agent needs three things from its host: read files, run local commands,
and ask its owner a question. Everything else is optional.

1. The owner pins one reviewed revision of this package in the project
   (`.tools/ai-comm`) and names the task.
2. One session creates the channel, only when the owner asks for a new one.
3. Each session joins as itself: its own handle (such as `builder` or
   `reviewer`), a host label of the owner's choosing, and its own non-secret
   session reference: the host's own session identifier, or one the owner
   assigns to that session if the host has none.
4. The two sessions exchange PING, PONG and ACK, then work through proposals,
   reviews, ACCEPT and DECIDE on exact artifacts.
5. While working, each session reads its inbox and acknowledges what it read.
   Being woken while idle depends on what the host can do; see the wake
   guidance in [ACTIVATE.md](ACTIVATE.md).

If the host discovers skills, link the pinned skill into the directory it scans.
If not, tell the session: "read `.tools/ai-comm/skills/ai-comm/SKILL.md` and
follow it". Both paths lead to the same instructions.

Optionally, keep one verified copy of ai-comm per user: `install.sh --user`
maintains a cache of verified releases in your home directory, checks a
project's pin with that trusted copy before anything runs from it, can register
one entry skill for hosts that read a skills directory, and can add an
`ai-comm` command. Projects keep their own pins either way. See
[installing for one user](docs/INSTALL.md).

## Activate in another session

Point the session at a pinned revision of this repository and [ACTIVATE.md](ACTIVATE.md):

Before joining, settle the checkout, channel, roles, reviewers and upgrade plan in
the [setup decisions](ACTIVATE.md#setup-decisions). It includes prompts for the
initiator and peer.

> Activate ai-comm from this repository at revision `<commit>`. Read ACTIVATE.md.
> Use channel `<absolute local path>`, handle `<my handle>`, host label `<host-label>`,
> session reference `<non-secret session reference>`, peers `<handles>`,
> and task `<authorized task>`. Create the channel only if I asked for a new one.

The session checks the inbox while working on the authorized task. An idle or
closed model cannot be woken by a file watcher. [Protocol](PROTOCOL.md) and
[trust boundary](SECURITY.md) describe what is guaranteed.

## Start the exchange from the discovered skill

After both sessions have read this pinned package skill and the project role
table, the owner starts each session directly: invoke the `ai-comm` skill with
`start A <subject>` in one session and `start B` in the other, using the host's
own way of invoking a skill, or in plain words ("ai-comm start A <subject>").

A performs the handshake after B joins, then opens the subject for review. These
requests never initialize a channel or take over an identity without the
owner's direct instruction in that session. Use status to inspect progress
without changing the inbox receipt and stop to stop only your own listener.
ai-comm itself does not wake idle sessions; separately approved host listeners
or nudge helpers must remain available.

## Two-session quick start

Clone and pin the package with Git. Choose a new, dedicated channel path outside
the package checkout. The path's parent must already exist.

Once a channel is running, follow the drafting conventions in
[ACTIVATE.md](ACTIVATE.md) section 3 so two sessions do not draft competing
proposals for the same topic.

```sh
git clone https://github.com/amazarache/ai-comm.git
cd ai-comm
git checkout <reviewed-commit>
python3 ai_comm.py --channel /tmp/example-channel init \
  --task 'Review a design together' --reviewers author reviewer
python3 ai_comm.py --channel /tmp/example-channel join \
  --as author --platform host-a --session author-conversation
python3 ai_comm.py --channel /tmp/example-channel join \
  --as reviewer --platform host-b --session reviewer-conversation
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

### Delivery status of your own messages

`outstanding` reports which of your messages a peer has not yet acknowledged,
from the peer's read receipt. It is read-only and content-free: it writes no
receipt, commit or heartbeat, and returns ids, sequences, acts and timestamps,
never summaries or bodies.

```sh
python3 ai_comm.py --channel /tmp/example-channel outstanding \
  --as author --session author-conversation --to reviewer \
  --state unseen --unreplied --after 0 --limit 1
```

The outstanding set is every message from you addressed to the peer with a
sequence above the peer's `read_through`. Each entry is `unseen` (beyond the
peer's last scanned `inbox` page) or `seen` (scanned, not yet acknowledged), and
`replied` is true when the peer has sent a message whose `reply_to` names it.
`--state`, `--unreplied` and `--after SEQ` filter the complete set before
`--limit` (1–100, default 10); `count` is the size of the whole set and `matched`
the number passing the filters. `--exclude-act ACT` (repeatable, documented acts
only) leaves messages of that act out of the set itself, before the limit; a
watcher can pass `--exclude-act ACK` so a terminal handshake `ACK`, which needs no
response, never counts as overdue. After a peer takeover, `seen` is reported equal to
`read`, as the peer's own next `inbox` would treat it.

Polling (`wait`, notifications, `state`) never moves a receipt, so it cannot hide
an unread message. The result is a snapshot: a receipt can move right after it is
read. An `unseen` entry shows that the peer's model has not yet scanned that
message; it does not prove the peer is unreachable, and a peer can be busy for
a while after scanning. A watcher can compare each class's oldest entry against
its own threshold and keep per-class watermarks, so that a newer overdue message
is never hidden by an older one.

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
| `1.5.0` | `3.0` (unchanged) | Tagged but not published and not in the release index; superseded by `1.5.1`. |
| `1.5.1` | `3.0` (unchanged) | Existing protocol-3 channels; first published release with the guided, owner-consented upgrade controller and the new-project installer. |
| `1.5.2` | `3.0` (unchanged) | Existing protocol-3 channels; `upgrade check` reports no offer on an up-to-date pin. |
| `1.5.3` | `3.0` (unchanged) | Existing protocol-3 channels; every instruction is host-neutral. |
| `1.6.0` | `3.0` (unchanged) | Existing protocol-3 channels; adds the per-user cache of verified releases, a trusted check of a project's pin, cache-side upgrade declarations, a per-user entry skill, an optional `ai-comm` launcher and the cache's own update offers. |
| `1.7.0` | `3.0` (unchanged) | Existing protocol-3 channels; adds the read-only `outstanding` delivery status of your own messages to a peer, and the GPL-3.0 licence. |
| `1.8.0` | `3.0` (unchanged) | Existing protocol-3 channels; adds the one-line install bootstrap `install-ai-comm.sh`. |

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

## Licence

Copyright (C) 2026 Adrian MAZARACHE.

ai-comm is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License,
version 3, as published by the Free Software Foundation (SPDX `GPL-3.0-only`). It is distributed without any
warranty. The full text is in [LICENSE](LICENSE).
