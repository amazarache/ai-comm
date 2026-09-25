# CLI validation errors

Errors keep the legacy `error` string and add a stable machine-readable code,
field, and repair hint. A response may also contain `allowed`, `got`, or bounded
receipt cursors when those values are safe to disclose:

```json
{"error":"confidence is not allowed for this act","code":"confidence_not_allowed","field":"confidence","allowed":["CORRECT","DECIDE","FACT"],"got":"ASK","hint":"Remove confidence; it is allowed only on FACT, CORRECT, and DECIDE."}
```

Codes are part of the CLI compatibility surface. Removing or renaming a code is
a MAJOR change. New validation rules add new codes. The text in `error` may be
clarified without changing its code; keep the code mapping stable when editing a
validator. Fields are lowercase identifiers. `got` is limited to validated
enum values, safe field names, counts, lengths, and policy lists. Payloads,
unregistered handles, raw references, source details, and unknown values are
never echoed. Every error includes a next-step hint. An error does not claim
success; if a caller loses the result around publication, inspect the channel and
retry with the same key so a committed message is returned idempotently.

Common repair codes:

| Code | Field | Meaning and next step |
|---|---|---|
| `confidence_not_allowed` | `confidence` | Remove confidence unless the act is FACT, CORRECT, or DECIDE. |
| `invalid_confidence` | `confidence` | Choose `verified`, `likely`, or `unverified`. |
| `confidence_required_by_channel_policy` | `confidence` | Supply a permitted confidence value for this act. |
| `this_act_requires_a_reply_target` | `reply_to` | Point to an earlier committed message. |
| `unregistered_recipients` | `to` | Register recipients first; `got` reports only the count. |
| `ack_out_of_range` | `ack` | Choose a cursor between the returned `read_through` and `scanned_through`. |
| `upgrade_requires_release_record` | `artifact` | Publish a valid canonical release record and cite its digest. |
| `invalid_affects_range` | `affects` | Use strict comma-separated comparators with no spaces. |
| `fixed_in_not_above_range` | `fixed_in` | Place the fix at or above an exclusive upper bound, or above an inclusive bound. |
| `unbounded_advisory_fixed_in` | `fixed_in` | Omit a fix version when an affected range has no upper bound. |
| `index_invalid` | `index` | Correct the release index; no update advice is emitted. |
| `index_inconsistent` | `index` | Reconcile the index with the local annotated tag; no update advice is emitted. |
| `channel_required` | `channel` | Pass the explicit channel path for channel operations. |

Other validator codes are lowercase identifiers derived from fixed rule names;
preserving a rule name preserves its code. If the error wording needs to change,
pass the existing code explicitly so the CLI API stays stable. Their `error` and
`hint` identify the rejected constraint and repair. A new rule must be documented
here and covered by a triggering test before release.

## `send --dry-run`

`send --dry-run` returns the same validation failures and warnings as a real
send, without publishing a commit, changing a receipt, reserving the retry key,
or durably allocating a control nonce. It returns `"dry_run": true` and
`"published": false`. Any proposed sequence, id, timestamp, nonce, or echo is
provisional and may differ on a real send. The channel may change after the
dry-run, so a later real send can still fail validation.

For bodies above 2,000 Unicode code points (Python `len`), the warning names
the soft limit and shows how to publish the text with `artifact --file PATH`
and reference the returned digest.
