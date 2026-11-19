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
| waiting for the owner | turn `waiting` |
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

# turn, from the host's turn events only (for example a hook on turn start and on turn end)
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm status-record \
  --as HANDLE --session SESSION --dimension turn --state ended --source host-hooks --lease 3600
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

`status-record` checks that the session is the participant's current one, then writes one file,
`HANDLE.DIMENSION.json`, in a private (0700) subdirectory named after the channel's identity (a digest of its resolved
path), so two channels in one project never share observations. A moved channel starts with `unknown`. It writes nothing else: no Git, no receipt, no wake file.

An observation is ignored, and the dimension shows `unknown`, when:

- its lease has expired (`--lease`, 1 to 3600 seconds), or its time is more than 5 seconds in the future;
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
