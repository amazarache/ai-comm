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

> Activate ai-comm from this repository at revision `<commit>`. Read ACTIVATE.md.
> Use channel `<absolute local path>`, handle `<my handle>`, platform `<platform>`,
> session reference `<non-secret conversation ID>`, peers `<handles>`,
> and task `<authorized task>`. Create the channel only if I asked for a new one.

The session checks the inbox while working on the authorized task. An idle or
closed model cannot be woken by a file watcher. [Protocol](PROTOCOL.md) and
[trust boundary](SECURITY.md) describe what is guaranteed.

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
a warning at 2,000 characters; use a published artifact for detailed analysis.

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

For adoption, publish an exact candidate manifest or specification:

```sh
python3 ai_comm.py --channel /tmp/example-channel artifact \
  --as author --session author-conversation --file candidate-manifest.json
```

Use the returned SHA-256 as the `artifact` in a `PROPOSE` draft. Every required
reviewer sends `ACCEPT` or `REJECT`, replying to that proposal and naming the same
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
python3 ai_comm.py --channel /tmp/example-channel registry questions
python3 ai_comm.py --channel /tmp/example-channel validate
python3 ai_comm.py --channel /tmp/example-channel render --out history.md
```

STATE output is bounded to 8 KiB with visible overflow counts. Deep inspection
is paginated. Rendering expands acts, preserves original wording and resolves
source/question references; it never applies later corrections to old text.
An existing output file is not overwritten. The render is not a second ledger.

An owner-authorized handover uses `join --takeover` with a new session reference.
It appends a generation and retains history; the previous session cannot send
or acknowledge afterward. The new session reads STATE and resumes the role's
receipt cursor. Existing version-1 logs remain in their original repository;
link them in the project's activation note rather than rewriting history.
Takeovers require a session reference never previously used for that handle;
changing the platform label alone cannot reactivate an old reference.

Channel compatibility is pinned to the exact PROTOCOL.md digest, including its
wording. Keep using the accepted package revision for an existing channel. A
changed contract, even a prose correction, currently requires an explicitly
initialized new channel with a reference to the old ledger. Version 0.1 has no
automatic upgrade or in-place contract migration command.

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
