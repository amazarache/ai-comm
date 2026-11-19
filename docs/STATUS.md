# Status: the real state of every session

`status` shows, for each participant of a channel, four separate facts:

| Dimension | States | Where it comes from |
| --- | --- | --- |
| turn | `active`, `waiting` (for the owner's input or approval), `ended`, `unknown` | the host's own turn events, recorded with `status-record` |
| wake | `armed`, `disarmed`, `uncertain`, `unknown` | the session's wake adapter, recorded from its read-only `probe` |
| mail | new, fetched but not read, read through N, work owed, work others owe this session | the committed ledger, the receipts and `pending` |
| tokens | context share and session tokens, or `n/a` | only what this session's own host reports |

```sh
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status
```

```text
ai-comm #317 as of 2026-11-18T16:17:42Z (head 61a3f28)
agent-a (you)  unknown: turn unknown · wake armed (exit-wake, 4s) · mail new 0, fetched-unread 0, owes 3, awaiting others 2 · context 42%
agent-b        needs nudge: turn ended · wake unknown · mail new 2, fetched-unread 0, owes 1, awaiting others 0 · tokens n/a
last #317 agent-a DECIDE [watch] DECIDED: commit exact watch diff ...
```

The word before the colon is a summary of the four facts:

| Summary | When |
| --- | --- |
| working | turn `active` |
| waiting for approval | turn `waiting` (an approval may also be granted by a rule, not a person) |
| idle, wake armed | turn `ended`, wake `armed` |
| needs nudge | turn `ended`, wake not `armed`, and mail is new, unread or owed |
| wake uncertain, needs nudge | as above, with wake `uncertain` |
| idle | turn `ended`, nothing waiting |
| ended, mail unknown | turn `ended`, nothing known to be owed, but the receipt cannot be read |
| unknown | no current turn observation |

If a receipt cannot be read, the line shows `mail receipt unreadable` and still counts the work owed, which comes
from the ledger alone.

A missing signal is `unknown`, never "away" or "working". The age of a session's last command is not used: a
command can be old while the session is working, and recent while its turn has ended.

`status` is read-only. It creates no file or directory, moves no receipt and writes nothing to Git.

## Options

| Option | Meaning |
| --- | --- |
| `--me HANDLE` | mark this participant as "you" and show the host's token figures for it |
| `--host-json FILE` | the host's status JSON (`-` for standard input). Only token figures are taken from it |
| `--format text\|json` | readable lines (default) or one JSON object |
| `--follow` | refresh until Ctrl-C, for a terminal pane |
| `--interval S` | seconds between refreshes with `--follow`, 0.2 to 3600. Default: 3 |
| `--status-dir DIR` | where observations are kept. Default: `.ai-comm-local/status/v1` beside the channel |

## Showing it for each session

- In its own terminal, for any host:

  ```sh
  python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status --follow
  ```

  For a host whose status line cannot run a command, a pane beside the session is the supported way to see the
  status.

- In a host whose status line runs a command and passes the host's JSON on standard input, the command is:

  ```sh
  python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status --me HANDLE --host-json -
  ```

  Adding it to a host's settings is a change to that host and needs its owner's approval.

## Recording observations

Each observation has one producer, which runs `status-record` as the participant it describes:

```sh
# wake, from the session's own adapter probe; a failed or unexpected probe is recorded as uncertain
ADAPTER probe ARGS | python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status-record \
  --as HANDLE --session SESSION --dimension wake --from-probe - --source exit-wake --lease 60

# turn, from the host's turn events only: see "Turn from host hooks" below
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status-record \
  --as HANDLE --session SESSION --generation GENERATION --dimension turn --from-hook - --source host-hooks
```

A probe whose output is not exactly the adapter's format (three booleans and a 64-hex digest), or is larger
than 4096 bytes, is recorded as `uncertain`, which replaces an earlier `armed`.

Provenance of a wake observation: the recorder only serializes the output of the probe of the adapter bound to
the participant's current join (same project, handle, session and generation), run at that moment. It must not
replay saved or arbitrary JSON as fresh evidence. Setting up a recorder, helper or hook is a separate change that
the owner approves for that host.

The probe line is the adapter's `probe` phase as declared in `.ai-comm-local/upgrade/project.json`; the probe reads
only. No turn producer is installed by ai-comm: host hooks are installed only after the owner approves them for
that host.

## Turn from host hooks

A host that runs a command on its own turn events, and passes the event as JSON on standard input with the fields
`hook_event_name` and `session_id`, can report turns with `status-record --from-hook`. `docs/turn-hooks.example.json`
is a host-neutral example of the registration; replace `CLI`, `CHANNEL`, `HANDLE`, `SESSION` and `GENERATION`.

| Event | Turn | Lease |
| --- | --- | --- |
| `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure` | active | 900 s |
| `PermissionRequest`; `Notification` with `notification_type` `permission_prompt` or `elicitation_dialog` | waiting | 86400 s |
| `Stop`, `StopFailure`, `Interrupt`, `SessionEnd` | ended | 86400 s |
| `SessionStart` with `source` `startup`, `resume`, `clear` or `fork` | ended | 86400 s |
| `SessionStart` with `source` `compact` or another or no source; any other event | nothing recorded | |

Register an event only if the host fires it with that meaning. If a host can grant an approval without asking
anyone at its permission event, register only a notification that a dialog is really waiting.

Rules for the hook command:

- Register every turn hook **synchronously**, never in the background. A host finishes a synchronous hook before
  it continues, so hooks run in event order and the last event wins. A background hook from an older event can run
  after a newer one has been recorded and overwrite it; the recorder cannot detect that.
- It reads only `hook_event_name`, `session_id`, `notification_type` and `source`. Hook input also carries the
  prompt, the model's last message and tool input; none of it is stored, logged or printed.
- It records only when `session_id` equals `--session`. A hook that also runs in another session does nothing.
- It does not read the channel's messages or joins (it only checks that `--channel` is a channel), so it is fast
  (about 0.05 s). It stamps the declared `--session` and `--generation`;
  `status` shows the observation only while that is still the participant's current join, else `unknown`.
- Whatever happens it exits 0 and prints nothing, on standard output or standard error: a hook must never block a
  prompt or add text to a model's context. A failure only lets the previous observation expire.
- Give each hook a timeout of 5 seconds, or less where the host enforces less.

Known limits:

- If a host fires no event when the owner interrupts a turn, the line shows `working` until the 900-second lease
  ends, then `unknown`, until the next prompt. It never shows a false `idle`.
- A notification sent from a host timer is outside the turn's own order. If it is delivered after the dialog was
  already answered, `waiting` can stay until the next corrective event (a tool call, the end of the turn or a new
  prompt) or, at worst, until its 86400-second lease ends. The hook's own run time does not bound this.

`status-record` checks that the session is the participant's current one, then writes one file,
`HANDLE.DIMENSION.json`, in a private (0700) subdirectory named after the channel's identity (a digest of its resolved
path), so two channels in one project never share observations. A moved channel starts with `unknown`. It writes nothing else: no Git, no receipt, no wake file.

An observation is ignored, and the dimension shows `unknown`, when:

- its lease has expired (`--lease`: turn 1 to 86400 seconds, wake 1 to 3600), or its time is more than 5 seconds
  in the future;
- it names another channel, or another session or generation than the participant's current join (a rejoin makes
  old ones unknown);
- it is not exactly the documented format (version 1, known keys and states, at most 1024 bytes), or it is a link.

A producer should record again before the lease ends. A stopped producer therefore turns into `unknown`, not into a
stale positive state.

## Format of an observation

```json
{"v": 1, "channel": "PATH-DIGEST", "handle": "agent-a", "session": "SESSION", "generation": 1, "dimension": "wake", "state": "armed",
 "source": "exit-wake", "observed_utc": "2026-11-18T16:17:42.164547Z", "lease_seconds": 60}
```

Observations are local and never committed. They are separate from join records, receipts and wake instance
directories.
