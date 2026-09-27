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

Packages released before 1.0.0 use the frozen generic
`package_contract_does_not_match_channel_select_its_pinned_revisi` error for
protocol digest mismatches. They cannot distinguish an older, newer, or unknown
contract. Keep using the package pinned to that channel; verify any proposed
migration with a protocol-3-capable package and the channel's accepted handoff.

An error's `field` is a public draft key or a documented CLI argument. When a
more precise internal field is useful, `subfield` identifies it without
expanding the top-level field namespace. For example, a malformed source detail
uses `field: "sources"` and `subfield: "detail"`.

For the frozen mismatch code used by packages before 1.0.0,
`package_contract_does_not_match_channel_select_its_pinned_revisi`, the repair is:
your pinned package implements a different contract from the channel. Do not
edit anything; ask the owner about the upgrade in ACTIVATE.md section 6.

CLI argument error fields: `actor`, `ack`, `after`, `channel`, `draft`, `dry_run`, `file`, `id`, `input`, `key`, `kind`, `limit`, `new_since`, `offset`, `out`, `platform`, `require_confidence`, `require_sources`, `reviewers`, `session`, `since`, `takeover`, `task`, `through`, `timeout`.

Complete code catalog:

| Code | Meaning |
|---|---|
| `ack_out_of_range` | ack out of range |
| `advisory_requires_affects` | advisory requires affects |
| `agreement_must_reference_the_exact_proposal_and_artifact` | agreement must reference the exact proposal and artifact |
| `agreement_topic_differs_from_proposal` | agreement topic differs from proposal |
| `all_required_reviewers_must_accept_this_exact_proposal` | all required reviewers must accept this exact proposal |
| `artifact_exceeds_1_mib` | artifact exceeds 1 mib |
| `artifact_hash_mismatch` | artifact hash mismatch |
| `artifact_must_be_published_first` | artifact must be published first |
| `artifact_must_be_utf_8_text` | artifact must be utf 8 text |
| `artifact_not_found` | artifact not found |
| `assigned_envelope_exceeds_size_limit` | assigned envelope exceeds size limit |
| `at_least_two_distinct_reviewers_required` | at least two distinct reviewers required |
| `bounded_advisory_requires_fixed_in` | bounded advisory requires fixed in |
| `channel_busy_retry_with_the_same_key` | channel busy retry with the same key |
| `channel_does_not_exist_initialize_it_explicitly` | channel does not exist initialize it explicitly |
| `channel_history_modified_or_removed_a_durable_record` | channel history modified or removed a durable record |
| `channel_initialization_failed` | channel initialization failed |
| `channel_must_be_a_dedicated_bare_git_repository` | channel must be a dedicated bare git repository |
| `channel_required` | channel required |
| `channel_snapshot_exceeds_64_mib_limit_archive_explicitly` | channel snapshot exceeds 64 mib limit archive explicitly |
| `channel_symlink_refused` | channel symlink refused |
| `confidence_not_allowed` | confidence not allowed |
| `confidence_required_by_channel_policy` | confidence required by channel policy |
| `control_nonce_does_not_match` | control nonce does not match |
| `corrupt_receipt` | corrupt receipt |
| `corrupt_receipt_bounds` | corrupt receipt bounds |
| `draft_digest_mismatch` | draft digest mismatch |
| `draft_exceeds_size_limit` | draft exceeds size limit |
| `duplicate_json_key` | duplicate json key |
| `duplicate_recipient` | duplicate recipient |
| `duplicate_retry_key_in_history` | duplicate retry key in history |
| `durable_records_are_append_only` | durable records are append only |
| `empty_affects_range` | empty affects range |
| `empty_artifact` | empty artifact |
| `empty_body` | empty body |
| `empty_non_secret_session_reference` | empty non secret session reference |
| `empty_question` | empty question |
| `empty_reason` | empty reason |
| `empty_session_reference` | empty session reference |
| `empty_source_detail` | empty source detail |
| `empty_source_reference` | empty source reference |
| `empty_stored_artifact` | empty stored artifact |
| `empty_summary` | empty summary |
| `empty_task` | empty task |
| `fixed_in_inside_range` | fixed in inside range |
| `fixed_in_not_above_range` | fixed in not above range |
| `fixed_in_requires_affects` | fixed in requires affects |
| `git_operation_failed_no_success_is_implied` | git operation failed no success is implied |
| `handle_already_active_explicit_takeover_required` | handle already active explicit takeover required |
| `index_inconsistent` | index inconsistent |
| `index_invalid` | index invalid |
| `init_refuses_an_existing_path` | init refuses an existing path |
| `input_exceeds_size_limit` | input exceeds size limit |
| `invalid_affects_range` | invalid affects range |
| `invalid_after` | invalid after |
| `invalid_artifact_digest` | invalid artifact digest |
| `invalid_artifact_length` | invalid artifact length |
| `invalid_body_length` | invalid body length |
| `invalid_cli_arguments` | invalid cli arguments |
| `invalid_confidence` | invalid confidence |
| `invalid_confidence_policy` | invalid confidence policy |
| `invalid_control_character_in_artifact` | invalid control character in artifact |
| `invalid_control_character_in_body` | invalid control character in body |
| `invalid_control_character_in_non_secret_session_reference` | invalid control character in non secret session reference |
| `invalid_control_character_in_question` | invalid control character in question |
| `invalid_control_character_in_reason` | invalid control character in reason |
| `invalid_control_character_in_session_reference` | invalid control character in session reference |
| `invalid_control_character_in_source_detail` | invalid control character in source detail |
| `invalid_control_character_in_source_reference` | invalid control character in source reference |
| `invalid_control_character_in_stored_artifact` | invalid control character in stored artifact |
| `invalid_control_character_in_summary` | invalid control character in summary |
| `invalid_control_character_in_task` | invalid control character in task |
| `invalid_control_nonce` | invalid control nonce |
| `invalid_control_reply_relationship` | invalid control reply relationship |
| `invalid_draft_digest` | invalid draft digest |
| `invalid_envelope_fields` | invalid envelope fields |
| `invalid_generation` | invalid generation |
| `invalid_git_object_response` | invalid git object response |
| `invalid_git_size_response` | invalid git size response |
| `invalid_handle` | invalid handle |
| `invalid_historical_session` | invalid historical session |
| `invalid_limit` | invalid limit |
| `invalid_manifest_sha256` | invalid manifest sha256 |
| `invalid_message_id` | invalid message id |
| `invalid_non_secret_session_reference_length` | invalid non secret session reference length |
| `invalid_notification_sequence` | invalid notification sequence |
| `invalid_offset` | invalid offset |
| `invalid_open_question_delta` | invalid open question delta |
| `invalid_platform` | invalid platform |
| `invalid_policy` | invalid policy |
| `invalid_policy_acts` | invalid policy acts |
| `invalid_protocol_digest` | invalid protocol digest |
| `invalid_protocol_sha256` | invalid protocol sha256 |
| `invalid_question_delta` | invalid question delta |
| `invalid_question_deltas` | invalid question deltas |
| `invalid_question_id` | invalid question id |
| `invalid_question_length` | invalid question length |
| `invalid_reason_length` | invalid reason length |
| `invalid_receipt_field` | invalid receipt field |
| `invalid_receipt_sequence` | invalid receipt sequence |
| `invalid_recipient` | invalid recipient |
| `invalid_recipients` | invalid recipients |
| `invalid_release_advisory` | invalid release advisory |
| `invalid_release_commit` | invalid release commit |
| `invalid_release_record` | invalid release record |
| `invalid_release_record_kind` | invalid release record kind |
| `invalid_reply_target` | invalid reply target |
| `invalid_retry_key` | invalid retry key |
| `invalid_reviewer` | invalid reviewer |
| `invalid_reviewer_configuration` | invalid reviewer configuration |
| `invalid_sender` | invalid sender |
| `invalid_sequence` | invalid sequence |
| `invalid_session_record` | invalid session record |
| `invalid_session_reference_length` | invalid session reference length |
| `invalid_since` | invalid since |
| `invalid_source_declaration` | invalid source declaration |
| `invalid_source_declarations` | invalid source declarations |
| `invalid_source_detail_length` | invalid source detail length |
| `invalid_source_id` | invalid source id |
| `invalid_source_kind` | invalid source kind |
| `invalid_source_policy` | invalid source policy |
| `invalid_source_reference_length` | invalid source reference length |
| `invalid_source_references` | invalid source references |
| `invalid_stored_artifact_length` | invalid stored artifact length |
| `invalid_summary_length` | invalid summary length |
| `invalid_task_length` | invalid task length |
| `invalid_timestamp` | invalid timestamp |
| `invalid_topic` | invalid topic |
| `invalid_utf_8_json` | invalid utf 8 json |
| `json_exceeds_size_limit` | json exceeds size limit |
| `json_must_be_an_object` | json must be an object |
| `local_git_inspection_failed` | local git inspection failed |
| `message_id_mismatch` | message id mismatch |
| `message_not_found` | message not found |
| `message_path_mismatch` | message path mismatch |
| `message_sequence_gap_or_collision` | message sequence gap or collision |
| `missing_draft_field` | missing draft field |
| `non_finite_json_number` | non finite json number |
| `noncanonical_stored_message` | noncanonical stored message |
| `not_an_ai_comm_channel` | not an ai comm channel |
| `notification_cursor_exceeds_channel_history` | notification cursor exceeds channel history |
| `one_delta_per_question_per_message` | one delta per question per message |
| `only_configured_reviewers_may_record_agreement` | only configured reviewers may record agreement |
| `operation_failed` | operation failed |
| `package_contract_does_not_match_channel_select_its_pinned_revisi` | package contract does not match channel select its pinned revisi |
| `package_version_missing` | package version missing |
| `partial_clone_release_check_unsupported` | partial clone release check unsupported |
| `pong_nonce_must_be_fresh` | pong nonce must be fresh |
| `proposal_already_decided_propose_a_new_revision` | proposal already decided propose a new revision |
| `question_id_already_used` | question id already used |
| `question_is_not_open` | question is not open |
| `receipt_symlink_refused` | receipt symlink refused |
| `release_record_must_be_canonical_json` | release record must be canonical json |
| `release_record_not_canonical` | release record not canonical |
| `release_tag_version_mismatch` | release tag version mismatch |
| `reply_target_must_be_an_earlier_committed_message` | reply target must be an earlier committed message |
| `retry_key_reused_with_changed_draft` | retry key reused with changed draft |
| `reused_historical_session_reference` | reused historical session reference |
| `reviewer_not_addressed_by_proposal` | reviewer not addressed by proposal |
| `reviewers_must_be_distinct` | reviewers must be distinct |
| `runtime_symlink_refused` | runtime symlink refused |
| `session_history_gap` | session history gap |
| `session_path_mismatch` | session path mismatch |
| `source_evidence_required_by_channel_policy` | source evidence required by channel policy |
| `source_id_is_immutable` | source id is immutable |
| `state_budget_too_small` | state budget too small |
| `stored_artifact_must_be_utf_8_text` | stored artifact must be utf 8 text |
| `stored_draft_exceeds_limit` | stored draft exceeds limit |
| `stored_object_exceeds_limit` | stored object exceeds limit |
| `takeover_requires_a_never_used_session_reference_for_this_handle` | takeover requires a never used session reference for this handle |
| `this_act_requires_a_reply_target` | this act requires a reply target |
| `this_act_requires_an_artifact` | this act requires an artifact |
| `timeout_must_be_0_60_seconds` | timeout must be 0 60 seconds |
| `truncated_git_object` | truncated git object |
| `truncated_git_object_response` | truncated git object response |
| `unbounded_advisory_fixed_in` | unbounded advisory fixed in |
| `unexpected_channel_entry_path` | unexpected channel entry path |
| `unexpected_channel_entry_type` | unexpected channel entry type |
| `unexpected_control_echo` | unexpected control echo |
| `unexpected_control_nonce` | unexpected control nonce |
| `unknown_act` | unknown act |
| `unknown_draft_field` | unknown draft field |
| `unknown_question_operation` | unknown question operation |
| `unknown_source_reference` | unknown source reference |
| `unregistered_or_stale_session` | unregistered or stale session |
| `unregistered_recipients` | unregistered recipients |
| `unsupported_message_version` | unsupported message version |
| `unsupported_or_malformed_channel` | unsupported or malformed channel |
| `upgrade_requires_release_record` | upgrade requires release record |
| `upgrade_summary_incomplete` | upgrade summary incomplete |

| `channel_contract_newer_than_package` | channel declares a newer protocol version |
| `channel_contract_older_than_package` | channel contract is older than this package |
| `channel_contract_unknown` | channel contract is unknown or inconsistent |
| `handoff_reviewer_cannot_stay_behind` | reviewer cannot stay behind during migration |
| `invalid_handoff_artifact` | invalid migration handoff artifact |
| `invalid_handoff_kind` | invalid migration handoff kind |
| `invalid_handoff_participants` | invalid migration participant list |
| `invalid_handoff_reviewers` | invalid migration reviewer set |
| `invalid_tool_version` | invalid tool version |
| `predecessor_arguments_must_be_paired` | predecessor and handoff must be supplied together |
| `predecessor_acceptance_missing` | a predecessor reviewer acceptance is missing |
| `predecessor_decision_invalid` | predecessor handoff decision is invalid |
| `predecessor_decision_commit_invalid` | predecessor DECIDE commit cannot be verified |
| `predecessor_decision_has_no_parent` | predecessor DECIDE commit has no parent |
| `predecessor_decision_not_final` | predecessor DECIDE is not the final message |
| `predecessor_decision_reviewer_invalid` | predecessor DECIDE was sent by a non-reviewer |
| `predecessor_handoff_digest_invalid` | handoff digest is malformed |
| `predecessor_handoff_invalid` | predecessor handoff is invalid |
| `predecessor_handoff_invalid_kind` | predecessor handoff kind is invalid |
| `predecessor_handoff_missing` | predecessor handoff is missing |
| `predecessor_handoff_participants_invalid` | predecessor handoff participants are invalid |
| `predecessor_handoff_protocol_digest_invalid` | predecessor handoff protocol digest is invalid |
| `predecessor_handoff_reviewers_invalid` | predecessor handoff reviewers are invalid |
| `predecessor_handoff_tampered` | predecessor handoff digest does not match its contents |
| `predecessor_head_unreadable` | predecessor HEAD cannot be inspected |
| `predecessor_head_not_decision` | predecessor HEAD is not the commit that added DECIDE |
| `predecessor_invalid_head` | invalid predecessor head |
| `predecessor_invalid_link` | invalid predecessor link |
| `predecessor_invalid_channel` | predecessor is not a valid protocol-2 ledger |
| `predecessor_not_found` | predecessor path does not exist |
| `predecessor_not_bare` | predecessor is not a bare channel |
| `predecessor_package_too_old` | package is below the handoff minimum version |
| `predecessor_participants_mismatch` | handoff participants differ from predecessor |
| `predecessor_policy_mismatch` | predecessor task or reviewer policy mismatch |
| `predecessor_reviewer_cannot_stay_behind` | configured reviewer cannot stay behind |
| `predecessor_source_protocol_mismatch` | handoff source digest differs from predecessor |
| `predecessor_source_required` | first message must cite the predecessor ledger |
| `predecessor_symlink_refused` | predecessor path is a symlink |
| `predecessor_target_protocol_mismatch` | handoff target digest differs from this package |
| `tool_version_changed_requires_fresh_session` | package version changed; use a fresh session reference |

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

Every call site assigns a stable code independently from its error wording.
`ERROR_CODES` in `ai_comm.py` is the complete public set. Additions require an
entry in this table and a triggering test. The suite asserts the code set matches
this catalog.

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
