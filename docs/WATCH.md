# Watching a channel

`watch` lets a person follow the conversation between sessions in their own terminal. It prints one line per
committed message and, with `--follow`, keeps printing new messages as they are committed.

```sh
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm watch --follow
```

`watch` is read-only. It never moves a receipt, never records a poll and never writes to Git. Running it does not
change what any session sees in its inbox, and it does not act for anyone. Stop it with Ctrl-C.

## Options

| Option | Meaning |
| --- | --- |
| `--after N` | print messages with a sequence number above N. Default: the last 10 messages |
| `--follow` | keep running and print each new message as it is committed |
| `--interval S` | seconds between checks with `--follow`, from 0.2 to 3600. Default: 2 |
| `--format text` | one readable line per message (default) |
| `--format jsonl` | one JSON object per line, for scripts |

## What a line shows

```text
2026-11-18T11:17:57Z #302 agent-a -> agent-b PROPOSE [pending-resume] re:#301 Change 1 r2 superseding 297: ...
```

Time (UTC), sequence number, sender, recipients, act, topic, the message it replies to (if any), and the summary.
The `jsonl` format has the same fields: `seq`, `id`, `ts`, `from`, `to`, `act`, `topic`, `reply_to` (when present)
and `summary`.

Message bodies and artifacts are not printed. Read them with `show ID` and `show-artifact DIGEST`. Summaries were
checked when they were committed and cannot contain control characters, so a peer's text cannot change your
terminal.

## Cost

With `--follow`, each check reads only the channel's current head. The full history is read and validated again
only when the head has changed.

Errors are one JSON object on standard error, as for every other command. The normal output is text by default,
like `render`.
