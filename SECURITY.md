# Security and reliability boundary

ai-comm is a local collaboration tool for cooperating processes with filesystem
access. It is not a multi-tenant service. OS directory permissions protect channel
data; handles, conversation references, hashes and Git author fields do not
authenticate against another writer with the same OS privileges. A privileged
writer can alter refs, restore an old complete repository or change the tool.
External signatures and remote transport are outside this release's scope.

New channel/runtime directories are private. A channel has no remote by default.
The tool never pushes, contacts a manufacturer, reads credential stores or emits
telemetry. Normal Git cloning/publishing of the **tool repository** uses the
user's configured Git credentials, outside the channel CLI.

Do not send secrets. Conversation references must be non-secret provenance, not
login session cookies. Immutable audit records are a poor place for credentials
or data that must later be erased. Peer messages confer no execution authority.

## Controls reviewed

- OWASP §1 **Data Sources**, **Data Types**, **Data Range**, **Data Length**:
  central validation covers drafts and stored JSON, duplicate keys, unsupported
  versions, envelope limits, identifiers, citations, corrections and transitions.
  Stored corruption fails rather than resetting receipt state.
  Text validation rejects Unicode format/bidi controls (including zero-width
  joiners) and validates stored artifact text as well as uploads.
- §12 **Reference Validation**, §5 **Resource Restriction**: participant names
  cannot become arbitrary paths; runtime symlinks are refused. Artifact input is
  an explicit local CLI file argument, never a path selected from peer content.
- §14 **Locking**, **Concurrency**: an OS lock spans writes; a separate temporary
  Git index and atomic ref update isolate publication from application staging.
  Receipt files use fsync and atomic replacement. Process exit releases the lock.
- §14 **System APIs**, **Dynamic Execution**: Git receives argument arrays, never
  a command shell. No message text is evaluated. Git plumbing does not invoke
  commit hooks. Ambient Git index/config overrides are removed for channel work.
- §2 **Contextual Encoding**, §7 **Log Injection**: structured output is JSON;
  Markdown render embeds escaped text. Errors do not echo draft payloads or print
  tracebacks. Failures exit nonzero and successes return the committed snapshot.
- §14 **Integrity Checks**, §7 **Integrity**: artifact/draft SHA-256 checks,
  append-only record checks and Git history provide consistency and an audit
  trail. They are not cryptographic proof of the author's identity.
- §5 **Logic Flows**: current session generations, receipt bounds, control reply
  relationships and exact-artifact reviewer stances are checked before publication.
  A takeover cannot reuse any earlier session reference for that handle.

The optional `wait --new-since` notification cursor does not create or update a
receipt, including its scanned boundary. It cannot authorize acknowledgements;
the ordinary inbox must still be read. Losing the notification cursor may repeat
alerts, but cannot discard unread messages. Caller-supplied cursors and stored
receipt data retain input, identity, bounds and symlink checks.

The v0.1.1 text-validation optimization retains the v0.1.0 rejection set, checked
against every Unicode code point in both multiline modes. It does not cache or
skip full snapshot/history validation. Render deduplication still escapes every
source and citation, and each partial export resolves its own first citation.

Web authentication, cookies, TLS endpoints, database accounts and network upload
controls are inapplicable because this release exposes none of those services.
Do not infer safety for a future network adapter from this local review.

## Recovery and limits

A crash before the delivery ref changes leaves no delivered message. A crash
afterward may lose the caller's success response; retry the identical draft/key.
Git may retain unreachable objects and abandoned temporary indexes after an
abrupt kill. They remain inside the private channel; do not delete live locks to
recover. Routine Git object maintenance can be performed explicitly when idle.

Corrupt receipts require inspection; deleting a receipt deliberately replays from
zero and can repeat task processing unless the session handles it idempotently.
Takeover preserves the role's read cursor and resets the new session's scanned
boundary to that cursor. This is not proof that the new model knows old context;
the activation workflow requires STATE and referenced evidence.

The test suite covers interrupted/concurrent publication and the supported local
Git environment. It does not establish power-loss durability for every disk,
network-filesystem semantics or protection from a malicious same-user writer.

To report a problem, provide a minimal synthetic reproduction and affected commit.
Do not include a private channel, credentials or household/work records in a
public issue.

## Guided upgrade controller

The controller acts only under a recorded single-use owner consent for the exact
project, source, from-commit and target. It runs integration steps as argv
arrays without a shell, parses at most 64 KiB of JSON output and never writes raw
hook output to the channel or journal. Project ancestors may be owned by root or
the user; a symlinked ancestor, an other-writable one without the sticky bit, or
a group-writable one whose group is not the user's private group is refused.
Peer phase reports check the declared owner, the current channel session and a
one-time nonce. As with the channel itself, this prevents mistakes and stale
data, not a malicious process running as the same user.

The new-project installer runs only from a clean, manifest-verified package,
fetches only with explicit permission, uses that package's manifest-bound origin,
verifies before publishing, and never writes outside the target project's
`.tools/ai-comm`, `.ai-comm-local/` and `.gitignore`.

## Runtime liveness records

Package 1.3 writes best-effort heartbeat files beneath the channel runtime directory, outside Git history. They are mode 0600 in a mode 0700 directory and contain only the participant handle, generation, UTC poll time, command name, and package version. A same-OS-user process can forge or replace these files; liveness is advisory process evidence, not authenticated peer or model activity. Symlinks and non-regular target files are refused.
