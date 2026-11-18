# Pending work and resuming a session

Two read-only commands answer "what is still owed, and by whom?" from the committed channel history alone.
They never move a receipt, never record a poll and never write to Git. Running them changes nothing.

```sh
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm pending [--for HANDLE] [--unclassified] [--offset N] [--limit N]
python3 -I .tools/ai-comm/ai_comm.py --channel .ai-comm resume --as HANDLE --session SESSION [--limit N]
```

Both print one JSON object. Errors are one JSON object on standard error, as for every other command.

## Why these exist

A session can end, crash or be compacted after it fetched or read a message but before it finished the work.
A read cursor cannot tell what is still open. `pending` and `resume` rebuild open work from the committed messages,
so a fresh session finds it again. Fetching (`inbox`), reading (`ack`) and replying are separate facts, and none of
them closes work on its own.

## What counts as open work

| Source | Who owes what | What closes it |
| --- | --- | --- |
| A structured question (any message whose `questions` opens one, including a PROPOSE) | each addressee: `answer_question` | only the question's close event (`{"op": "close", "id": ...}`). Deciding or superseding the proposal that carried it does not close it |
| PROPOSE | each configured reviewer without a stance: `record_stance`. The author: `revise_or_withdraw` after a REJECT; `decide` when every reviewer has accepted | a reviewer's ACCEPT or REJECT closes that reviewer's item only. The proposal closes only with a DECIDE for it, or when the same author supersedes it (below) |
| PING | each recipient: `pong` | that recipient's PONG |
| PONG | the sender of the PING it answers: `final_ack` | that sender's ACK replying to the PONG. Other recipients of the PONG owe nothing |

A reply, a REVIEW, an acknowledgement or a newer message on the same topic never closes anything by itself. In
particular, a newer proposal leaves an older one open until it is decided or explicitly superseded.

## Unclassified candidates

Some requests are written as plain text inside an ordinary message. No rule can find them reliably, so they are
kept apart from the exact list above and shown with `pending --unclassified`:

- every ASK, FACT, CORRECT or REVIEW addressed to someone that opens no structured question, listed once per
  addressee with the action `may_contain_untracked_request`.

A candidate is not a task. It only says the message may contain a request that is not tracked. Most candidates in an
older channel contain none, and the count need not reach zero. A fetch, a read acknowledgement or any reply, direct
or indirect, leaves a candidate in place. Only the addressee's explicit `closes:` message (below) removes it. A
message that itself closes or supersedes something is not a candidate.

Every `pending` result reports the size of the other list (`unclassified_count` or `obligations_count`).

## Two conventions for closing work explicitly

Both use existing message fields only.

- Supersede your own proposal: the new PROPOSE's body starts with the line `supersedes: <older message id>`, and
  the message cites that id as a source `ref` (in `sources`, or through `refs` to a source declared earlier).
  Example draft:

  ```json
  {"act": "PROPOSE", "to": ["beta"], "topic": "review", "artifact": "<sha256>",
   "summary": "Revision 2 of the change.",
   "body": "supersedes: 00000012-alpha\nWhat changed: ...",
   "sources": [{"id": "r1-proposal", "kind": "conversation", "ref": "00000012-alpha", "detail": "revision 1"}]}
  ```

- Close a candidate addressed to you: send a message whose body starts with `closes: <message id>` and
  that cites the id the same way.

A record counts only when it is valid: `supersedes:` must be a PROPOSE naming an earlier PROPOSE by the same author;
`closes:` must come from an addressee of an earlier ASK, FACT, CORRECT or REVIEW that opened no structured question.
An invalid record (unknown target, wrong sender, wrong act) closes nothing and is itself listed as a candidate.

For new requests, use structured questions. They are tracked exactly and need no convention.

## Output

`pending` returns `items` (one entry per owed action: `id`, `seq`, `act`, `topic`, `owner`, `action`, `summary`,
and for proposals `artifact` and `stances`), with `count`, `next` and `more` for paging.

`resume` returns, for the calling participant:

- `you`: generation, `read_through`, `seen_through`, `unread_addressed`;
- `pending`: what you owe;
- `waiting_on_others`: what others owe on your messages;
- `open_proposals`: with artifact digests and each reviewer's latest stance;
- `unclassified_count`, the last five messages, and `next_steps`.

Read each item's full message with `show ID` and its artifact with `show-artifact DIGEST` before acting.
