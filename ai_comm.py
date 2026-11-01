#!/usr/bin/env python3
"""Explicit, local, Git-backed communication between cooperating sessions."""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

VERSION = "1.5.1"
RELEASE_ORIGIN = "https://github.com/amazarache/ai-comm.git"
PROTOCOL_VERSION = "3.0"
PREVIOUS_PROTOCOLS = (("2.0", "709af4b00ffcabeeca1b175d7d7d6db5103df829dabba1e529eef26dce23a000", "0.2.0"),)
ACTS = {"PING", "ACK", "PONG", "ASK", "FACT", "CORRECT", "PROPOSE",
        "REVIEW", "ACCEPT", "REJECT", "DECIDE"}
CONFIDENCE = {"verified", "likely", "unverified"}
CONF_ACTS = {"FACT", "CORRECT", "DECIDE"}
DRAFT_KEYS = {"act", "to", "topic", "summary", "body", "reply_to", "refs",
              "sources", "questions", "artifact", "confidence"}
CLI_ERROR_FIELDS = {"actor", "ack", "after", "channel", "draft", "dry_run", "file",
                    "id", "input", "key", "kind", "limit", "new_since", "offset", "out",
                    "participant", "platform", "require_confidence", "require_sources", "reviewers",
                    "session", "since", "stale_after", "takeover", "target", "task", "through", "timeout"}
PUBLIC_ERROR_FIELDS = DRAFT_KEYS | CLI_ERROR_FIELDS
INTERNAL_FIELD_MAP = {
    "recipient": ("to", "recipient"), "handle": ("actor", None),
    "sender": ("actor", None), "reply_target": ("reply_to", "target"),
    "control_nonce": ("reply_to", "nonce"), "artifact_digest": ("artifact", "digest"),
    "draft_digest": ("draft", "digest"), "source_id": ("sources", "id"),
    "source_kind": ("sources", "kind"), "source_detail": ("sources", "detail"),
    "source_reference": ("sources", "ref"), "question": ("questions", "text"),
    "question_id": ("questions", "id"), "reason": ("artifact", "reason"),
    "advisory": ("artifact", "advisory"), "affects": ("artifact", "affects"),
    "fixed_in": ("artifact", "fixed_in"), "package_version": ("artifact", "package_version"),
    "commit": ("artifact", "commit"), "tag": ("artifact", "tag"),
    "manifest_sha256": ("artifact", "manifest_sha256"),
    "protocol_sha256": ("artifact", "protocol_sha256"),
    "protocol_digest": ("channel", "protocol_sha256"), "index": ("input", "index"),
    "generation": ("session", "generation"), "non_secret_session_reference": ("session", "reference"),
    "session_reference": ("session", "reference"), "platform": ("platform", None),
    "reviewer": ("reviewers", "reviewer"), "message_id": ("id", None),
    "sequence": ("input", "sequence"), "notification_sequence": ("new_since", None),
    "receipt_sequence": ("through", None), "receipt_field": ("ack", "receipt"),
    "retry_key": ("key", None), "stored_artifact": ("artifact", None),
}
META_KEYS = {"v", "id", "seq", "ts", "from", "session", "generation", "key",
             "draft_sha256", "nonce", "echo"}
HANDLE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
IDENT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
KEY = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
PROTOCOL_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
TEXT_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MULTILINE_CONTROLS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
MID = re.compile(r"[0-9]{8}-[a-z][a-z0-9-]{0,31}\Z")
MAX_DRAFT = 65536
MAX_MESSAGE = 131072
MAX_ARTIFACT = 1048576
REF = "refs/heads/main"
RELEASE_ADVISORIES = {"optional", "recommended", "required"}
RELEASE_FIELDS = {"kind", "package_version", "commit", "tag", "manifest_sha256",
                  "protocol_sha256", "advisory", "reason"}


class ProtocolError(Exception):
    """A safe, actionable failure without payload or credential disclosure."""

    def __init__(self, message, *, code=None, field=None, allowed=None, got=None, hint=None, details=None):
        super().__init__(message)
        self.message = message
        raw_field = field if isinstance(field, str) and re.fullmatch(r"[a-z_]{1,48}", field) else "input"
        mapped = INTERNAL_FIELD_MAP.get(raw_field)
        if mapped:
            safe_field, subfield = mapped
        elif raw_field in PUBLIC_ERROR_FIELDS:
            safe_field, subfield = raw_field, None
        else:
            safe_field, subfield = "input", raw_field
        safe_code = code or "operation_failed"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", safe_code):
            safe_code = "operation_failed"
        self.details = {"error": message, "code": safe_code, "field": safe_field,
                        "hint": hint or f"Review the {safe_field} value and retry after correcting it."}
        if subfield and re.fullmatch(r"[a-z_]{1,48}", subfield):
            self.details["subfield"] = subfield
        safe_values = ACTS | CONFIDENCE | RELEASE_ADVISORIES
        if isinstance(allowed, (list, tuple)) and all(isinstance(v, str) and v in safe_values for v in allowed):
            self.details["allowed"] = list(allowed)
        if type(got) is int and 0 <= got <= 99999999:
            self.details["got"] = got
        elif isinstance(got, str) and got in safe_values:
            self.details["got"] = got
        if isinstance(details, dict):
            status = details.get("status")
            if isinstance(status, str) and status in {
                    "index_missing", "index_invalid", "index_inconsistent",
                    "not_git_checkout", "ok"}:
                self.details["status"] = status
            for name in ("read_through", "scanned_through"):
                value = details.get(name)
                if name in details and type(value) is int and 0 <= value <= 99999999:
                    self.details[name] = value


ERROR_CODES = frozenset([
    'ack_out_of_range',
    'advisory_requires_affects',
    'agreement_must_reference_the_exact_proposal_and_artifact',
    'agreement_topic_differs_from_proposal',
    'all_required_reviewers_must_accept_this_exact_proposal',
    'artifact_exceeds_1_mib',
    'artifact_hash_mismatch',
    'artifact_must_be_published_first',
    'artifact_must_be_utf_8_text',
    'artifact_not_found',
    'assigned_envelope_exceeds_size_limit',
    'at_least_two_distinct_reviewers_required',
    'bounded_advisory_requires_fixed_in',
    'channel_busy_retry_with_the_same_key',
    'channel_contract_newer_than_package',
    'channel_contract_older_than_package',
    'channel_contract_unknown',
    'channel_does_not_exist_initialize_it_explicitly',
    'channel_history_modified_or_removed_a_durable_record',
    'channel_initialization_failed',
    'channel_must_be_a_dedicated_bare_git_repository',
    'channel_required',
    'channel_snapshot_exceeds_64_mib_limit_archive_explicitly',
    'channel_symlink_refused',
    'confidence_not_allowed',
    'confidence_required_by_channel_policy',
    'control_nonce_does_not_match',
    'corrupt_receipt',
    'corrupt_receipt_bounds',
    'draft_digest_mismatch',
    'draft_exceeds_size_limit',
    'duplicate_json_key',
    'duplicate_recipient',
    'duplicate_retry_key_in_history',
    'durable_records_are_append_only',
    'empty_affects_range',
    'empty_artifact',
    'empty_body',
    'empty_non_secret_session_reference',
    'empty_question',
    'empty_reason',
    'empty_session_reference',
    'empty_source_detail',
    'empty_source_reference',
    'empty_stored_artifact',
    'empty_summary',
    'empty_task',
    'fixed_in_inside_range',
    'fixed_in_not_above_range',
    'fixed_in_requires_affects',
    'git_operation_failed_no_success_is_implied',
    'handle_already_active_explicit_takeover_required',
    'handoff_reviewer_cannot_stay_behind',
    'index_inconsistent',
    'index_invalid',
    'init_refuses_an_existing_path',
    'input_exceeds_size_limit',
    'invalid_affects_range',
    'invalid_after',
    'invalid_artifact_digest',
    'invalid_artifact_length',
    'invalid_body_length',
    'invalid_cli_arguments',
    'invalid_confidence',
    'invalid_confidence_policy',
    'invalid_control_character_in_artifact',
    'invalid_control_character_in_body',
    'invalid_control_character_in_non_secret_session_reference',
    'invalid_control_character_in_question',
    'invalid_control_character_in_reason',
    'invalid_control_character_in_session_reference',
    'invalid_control_character_in_source_detail',
    'invalid_control_character_in_source_reference',
    'invalid_control_character_in_stored_artifact',
    'invalid_control_character_in_summary',
    'invalid_control_character_in_task',
    'invalid_control_nonce',
    'invalid_control_reply_relationship',
    'invalid_draft_digest',
    'invalid_envelope_fields',
    'invalid_generation',
    'invalid_git_object_response',
    'invalid_git_size_response',
    'invalid_handle',
    'invalid_heartbeat_command',
    'invalid_handoff_artifact',
    'invalid_handoff_kind',
    'invalid_handoff_participants',
    'invalid_handoff_reviewers',
    'invalid_historical_session',
    'invalid_limit',
    'invalid_manifest_sha256',
    'invalid_message_id',
    'invalid_non_secret_session_reference_length',
    'invalid_notification_sequence',
    'invalid_offset',
    'invalid_open_question_delta',
    'invalid_platform',
    'invalid_policy',
    'invalid_policy_acts',
    'invalid_protocol_digest',
    'invalid_protocol_sha256',
    'invalid_question_delta',
    'invalid_question_deltas',
    'invalid_question_id',
    'invalid_question_length',
    'invalid_reason_length',
    'invalid_receipt_field',
    'invalid_receipt_sequence',
    'invalid_recipient',
    'invalid_recipients',
    'invalid_release_advisory',
    'invalid_release_commit',
    'invalid_release_record',
    'invalid_release_record_kind',
    'invalid_reply_target',
    'invalid_retry_key',
    'invalid_reviewer',
    'invalid_reviewer_configuration',
    'invalid_sender',
    'invalid_sequence',
    'invalid_session_record',
    'invalid_session_reference_length',
    'invalid_since',
    'invalid_stale_after',
    'invalid_source_declaration',
    'invalid_source_declarations',
    'invalid_source_detail_length',
    'invalid_source_id',
    'invalid_source_kind',
    'invalid_source_policy',
    'invalid_source_reference_length',
    'invalid_source_references',
    'invalid_stored_artifact_length',
    'invalid_summary_length',
    'invalid_task_length',
    'invalid_timestamp',
    'invalid_tool_version',
    'invalid_topic',
    'invalid_upgrade_target',
    'invalid_utf_8_json',
    'json_exceeds_size_limit',
    'json_must_be_an_object',
    'local_git_inspection_failed',
    'message_id_mismatch',
    'message_not_found',
    'message_path_mismatch',
    'message_sequence_gap_or_collision',
    'missing_draft_field',
    'non_finite_json_number',
    'noncanonical_stored_message',
    'not_an_ai_comm_channel',
    'notification_cursor_exceeds_channel_history',
    'one_delta_per_question_per_message',
    'only_configured_reviewers_may_record_agreement',
    'operation_failed',
    'package_contract_does_not_match_channel_select_its_pinned_revisi',
    'package_version_missing',
    'partial_clone_release_check_unsupported',
    'pong_nonce_must_be_fresh',
    'predecessor_arguments_must_be_paired',
    'predecessor_acceptance_missing',
    'predecessor_decision_invalid',
    'predecessor_decision_commit_invalid',
    'predecessor_decision_has_no_parent',
    'predecessor_decision_not_final',
    'predecessor_decision_reviewer_invalid',
    'predecessor_handoff_digest_invalid',
    'predecessor_handoff_invalid',
    'predecessor_handoff_invalid_kind',
    'predecessor_handoff_missing',
    'predecessor_handoff_participants_invalid',
    'predecessor_handoff_protocol_digest_invalid',
    'predecessor_handoff_reviewers_invalid',
    'predecessor_handoff_tampered',
    'predecessor_head_unreadable',
    'predecessor_head_not_decision',
    'predecessor_invalid_head',
    'predecessor_invalid_link',
    'predecessor_invalid_channel',
    'predecessor_not_found',
    'predecessor_not_bare',
    'predecessor_package_too_old',
    'predecessor_participants_mismatch',
    'predecessor_policy_mismatch',
    'predecessor_reviewer_cannot_stay_behind',
    'predecessor_source_protocol_mismatch',
    'predecessor_source_required',
    'predecessor_symlink_refused',
    'predecessor_target_protocol_mismatch',
    'proposal_already_decided_propose_a_new_revision',
    'question_id_already_used',
    'question_is_not_open',
    'receipt_symlink_refused',
    'release_record_must_be_canonical_json',
    'release_record_not_canonical',
    'release_tag_version_mismatch',
    'reply_target_must_be_an_earlier_committed_message',
    'retry_key_reused_with_changed_draft',
    'reused_historical_session_reference',
    'reviewer_not_addressed_by_proposal',
    'reviewers_must_be_distinct',
    'runtime_symlink_refused',
    'session_history_gap',
    'session_path_mismatch',
    'source_evidence_required_by_channel_policy',
    'source_id_is_immutable',
    'state_budget_too_small',
    'state_identity_incomplete',
    'stored_artifact_must_be_utf_8_text',
    'stored_draft_exceeds_limit',
    'stored_object_exceeds_limit',
    'takeover_requires_a_never_used_session_reference_for_this_handle',
    'this_act_requires_a_reply_target',
    'this_act_requires_an_artifact',
    'timeout_must_be_0_60_seconds',
    'tool_version_changed_requires_fresh_session',
    'truncated_git_object',
    'truncated_git_object_response',
    'unbounded_advisory_fixed_in',
    'unexpected_channel_entry_path',
    'unexpected_channel_entry_type',
    'unexpected_control_echo',
    'unexpected_control_nonce',
    'unknown_act',
    'unknown_draft_field',
    'unknown_question_operation',
    'unknown_source_reference',
    'unregistered_or_stale_session',
    'unregistered_recipients',
    'unsupported_message_version',
    'unsupported_or_malformed_channel',
    'upgrade_channel_invalid',
    'upgrade_channel_not_bare',
    'upgrade_channel_symlink',
    'upgrade_participant_requires_channel',
    'upgrade_participant_duplicate',
    'upgrade_participant_malformed',
    'upgrade_participant_missing',
    'upgrade_participant_not_required',
    'upgrade_participant_status_invalid',
    'upgrade_participant_unknown',
    'upgrade_plan_gate1_required',
    'upgrade_plan_unavailable',
    'upgrade_requires_release_record',
    'upgrade_reviewer_stays_behind',
    'upgrade_target_not_newer',
    'upgrade_target_unverified',
    'upgrade_summary_incomplete',
])

FIELD_HINTS = {
    "act": "Choose one of the documented communication acts; each act has a distinct meaning.",
    "body": "Use nonempty UTF-8 text within the documented size limit and omit prohibited controls.",
    "confidence": "Use confidence only on FACT, CORRECT, or DECIDE and choose verified, likely, or unverified.",
    "reply_to": "Set reply_to to the id of an earlier committed message that this message answers.",
    "to": "List 1–32 registered handles and ensure the recipient is available in this channel.",
    "artifact": "Publish the artifact first, then use its returned SHA-256 digest.",
    "sources": "Declare valid source records and cite their ids when channel policy requires evidence.",
    "questions": "Use a valid open or close question operation with a unique id.",
    "ack": "Acknowledge only a scanned inbox boundary between the prior receipt and seen cursor.",
    "session": "Use the registered, current session reference for this handle.",
    "participant": "Give each current channel participant exactly one HANDLE=will_upgrade or HANDLE=stays_behind value.",
    "target": "Choose a newer package version present in the locally verified release index.",
    "channel": "Use the explicit, initialized bare channel repository with its pinned protocol.",
    "input": "Check the documented input shape and limits; no message was published.",
    "summary": "Provide a nonempty summary of at most 300 characters.",
    "topic": "Use a lowercase identifier for the message topic.",
}


def error_field(message):
    lowered = message.lower()
    for token, field in (("confidence", "confidence"), ("recipient", "to"),
                         ("recipient", "to"), ("reply", "reply_to"),
                         ("artifact", "artifact"), ("source", "sources"),
                         ("question", "questions"), ("receipt", "ack"),
                         ("ack ", "ack"), ("session", "session"),
                         ("channel", "channel"), ("draft", "input"),
                         ("message", "input"), ("json", "input"),
                         ("git", "channel"), ("state", "channel"),
                         ("protocol", "channel"), ("reviewer", "input"),
                         ("proposal", "artifact"), ("nonce", "reply_to"),
                         ("control", "reply_to"), ("timeout", "input"),
                         ("cursor", "ack"), ("handle", "session"),
                         ("path", "channel"), ("stored", "channel")):
        if token in lowered:
            return field
    match = re.fullmatch(r"(?:invalid|empty|unknown) ([a-z_]{1,32})(?: .*)?", lowered)
    if match:
        return match.group(1)
    return "input"


def error_hint(message, field):
    lowered = message.lower()
    if "timestamp" in lowered:
        return "Use a canonical UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form."
    if "json" in lowered:
        return "Provide a UTF-8 JSON object with unique keys and no non-finite numbers."
    if "size limit" in lowered or "exceeds" in lowered and "limit" in lowered:
        return "Reduce the input to the documented size limit and retry."
    if "sequence" in lowered or "cursor" in lowered:
        return "Use a sequence within the currently scanned channel history."
    if "control reply" in lowered or "nonce" in lowered or "control echo" in lowered:
        return "Reply to the matching PING or PONG and use the nonce assigned by the tool."
    if "session" in lowered or "takeover" in lowered or "generation" in lowered:
        return "Use the currently registered session; an authorized takeover needs a fresh reference."
    if "handle" in lowered and "recipient" not in lowered:
        return "Use a registered lowercase handle and the currently registered session reference."
    if "retry key" in lowered or "retry key" == field:
        return "Reuse a key only for the identical draft; choose a new key for changed content."
    if "tag" in lowered or "index" in lowered or "manifest" in lowered:
        return "Reconcile the local index, annotated tag, and release manifest before relying on advice."
    if "protocol" in lowered or "channel contract" in lowered:
        return "Use the package revision whose bundled protocol digest matches this channel."
    if "question" in lowered:
        return FIELD_HINTS["questions"]
    if "source" in lowered or "evidence" in lowered:
        return FIELD_HINTS["sources"]
    if "artifact" in lowered or "proposal" in lowered or "agreement" in lowered:
        return FIELD_HINTS["artifact"]
    if "reply" in lowered:
        return FIELD_HINTS["reply_to"]
    if "recipient" in lowered or "handle" in lowered:
        return FIELD_HINTS["to"]
    if "policy" in lowered or "reviewer" in lowered:
        return "Check the channel initialization policy and configured reviewer set."
    return FIELD_HINTS.get(field, FIELD_HINTS["input"])


def require(condition, message, *, code=None, field=None, allowed=None, got=None, hint=None, details=None):
    if not condition:
        inferred = field or error_field(message)
        raise ProtocolError(message, code=code, field=inferred, allowed=allowed,
                            got=got, hint=hint or error_hint(message, inferred), details=details)


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def semver(value, name="version"):
    match = SEMVER.fullmatch(value) if isinstance(value, str) else None
    require(match is not None, f"invalid {name}", code=f"invalid_{name}", field=name,
            hint=f"Use a three-part numeric {name} such as 1.2.3, without leading zeroes.")
    return tuple(int(part) for part in match.groups())


def parse_protocol_version(value):
    match = PROTOCOL_SEMVER.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def protocol_version_table(current_digest=None, current_version=PROTOCOL_VERSION,
                           previous=PREVIOUS_PROTOCOLS):
    """Return digest -> (version, last package), or None for an ambiguous table."""
    digest = current_digest or sha(Path(__file__).with_name("PROTOCOL.md").read_bytes())
    rows = list(previous) + [(current_version, digest, VERSION)]
    by_digest, by_version = {}, {}
    for version, protocol_digest, package in rows:
        parsed = parse_protocol_version(version)
        if parsed is None or not HEX.fullmatch(protocol_digest) or not SEMVER.fullmatch(package):
            return None
        if protocol_digest in by_digest or version in by_version:
            return None
        by_digest[protocol_digest] = (version, package)
        by_version[version] = protocol_digest
    return by_digest


def classify_protocol_mismatch(channel_digest, declared_version, current_digest=None,
                               current_version=PROTOCOL_VERSION, previous=PREVIOUS_PROTOCOLS):
    table = protocol_version_table(current_digest, current_version, previous)
    declared = parse_protocol_version(declared_version)
    current = parse_protocol_version(current_version)
    if table is None or current is None:
        return "channel_contract_unknown", None
    known = table.get(channel_digest)
    if known:
        version, last_package = known
        # Protocol 2.0 predates policy.protocol_version; its exact known digest
        # remains classifiable without trusting or requiring that new field.
        if declared_version is None and version == "2.0":
            return "channel_contract_older_than_package", last_package
        if declared_version != version:
            return "channel_contract_unknown", None
        known_version = parse_protocol_version(version)
        if known_version < current:
            return "channel_contract_older_than_package", last_package
        return "channel_contract_unknown", None
    if declared is not None and declared > current:
        return "channel_contract_newer_than_package", None
    return "channel_contract_unknown", None


def parse_affects(value):
    """Parse the deliberately small, strict conjunction-only advisory range."""
    require(isinstance(value, str) and 0 < len(value) <= MAX_ARTIFACT,
            "invalid affects range", code="invalid_affects_range", field="affects",
            hint="Use comma-separated comparators such as >=1.0.0,<1.2.0 with no spaces.")
    terms = value.split(",")
    require(bool(terms), "invalid affects range", code="invalid_affects_range",
            field="affects", hint="Use one or more comma-separated version comparators.")
    parsed = []
    upper = None
    upper_inclusive = False
    equal = None
    lower = None
    lower_inclusive = False
    for term in terms:
        match = re.fullmatch(r"(>=|>|<=|<|==)(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", term)
        require(match is not None, "invalid affects range", code="invalid_affects_range",
                field="affects", hint="Use >=, >, <=, <, or == followed by X.Y.Z; omit spaces and wildcards.")
        op = match.group(1)
        version = tuple(int(part) for part in match.groups()[1:])
        parsed.append((op, version))
        if op in {"<", "<="} and (upper is None or version < upper or
                                  (version == upper and op == "<" and upper_inclusive)):
            upper, upper_inclusive = version, op == "<="
        elif op == "==":
            equal = version
        elif op in {">", ">="} and (lower is None or version > lower or
                                    (version == lower and op == ">" and lower_inclusive)):
            lower, lower_inclusive = version, op == ">="
    if equal is not None:
        require(all((equal > v if op == ">" else equal >= v if op == ">=" else
                     equal < v if op == "<" else equal <= v if op == "<=" else equal == v)
                    for op, v in parsed), "empty affects range", code="empty_affects_range",
                field="affects", hint="Choose compatible comparators that include at least one version.")
        # Equality is a closed, bounded interval for fixed_in validation.
        upper, upper_inclusive = equal, True
    if upper is not None and lower is not None:
        require(lower < upper or (lower == upper and lower_inclusive and upper_inclusive),
                "empty affects range", code="empty_affects_range", field="affects",
                hint="Choose compatible lower and upper bounds that include at least one version.")
        if lower == upper and lower_inclusive and upper_inclusive:
            require(all((lower > v if op == ">" else lower >= v if op == ">=" else
                         lower < v if op == "<" else lower <= v if op == "<=" else lower == v)
                        for op, v in parsed), "empty affects range", code="empty_affects_range",
                    field="affects", hint="Choose compatible comparators that include at least one version.")
    return parsed, upper, upper_inclusive


def range_matches(parsed, version):
    return all((version > bound if op == ">" else version >= bound if op == ">=" else
                version < bound if op == "<" else version <= bound if op == "<=" else
                version == bound) for op, bound in parsed)


def validate_release_record(record):
    require(isinstance(record, dict) and RELEASE_FIELDS <= set(record) <=
            (RELEASE_FIELDS | {"affects", "fixed_in"}), "invalid release record",
            code="invalid_release_record", field="artifact",
            hint="Publish canonical release-record JSON with the documented fields and no extras.")
    require(record["kind"] == "ai-comm-release", "invalid release record kind",
            code="invalid_release_record_kind", field="artifact",
            hint="Set kind to ai-comm-release.")
    semver(record["package_version"], "package_version")
    require(isinstance(record["commit"], str) and re.fullmatch(r"[0-9a-f]{40}", record["commit"]),
            "invalid release commit", code="invalid_release_commit", field="commit",
            hint="Use the 40-character lowercase commit id.")
    require(record["tag"] == "v" + record["package_version"], "release tag/version mismatch",
            code="release_tag_version_mismatch", field="tag",
            hint="Set tag to v followed by package_version.")
    for field in ("manifest_sha256", "protocol_sha256"):
        pattern(record[field], HEX, field)
    require(isinstance(record["advisory"], str) and record["advisory"] in RELEASE_ADVISORIES, "invalid release advisory",
            code="invalid_release_advisory", field="advisory",
            allowed=sorted(RELEASE_ADVISORIES), hint="Choose optional, recommended, or required.")
    text_field(record["reason"], "reason", 2000, True)
    advisory = record["advisory"]
    if advisory in {"recommended", "required"}:
        require("affects" in record, "advisory requires affects range",
                code="advisory_requires_affects", field="affects",
                hint="Add a strict semver affects range for recommended or required advice.")
    if "affects" in record:
        parsed, upper, upper_inclusive = parse_affects(record["affects"])
        if upper is None:
            require("fixed_in" not in record, "unbounded advisory cannot have fixed_in",
                    code="unbounded_advisory_fixed_in", field="fixed_in",
                    hint="Omit fixed_in for an unbounded range; the defect is described as unfixed.")
        else:
            require("fixed_in" in record, "bounded advisory requires fixed_in",
                    code="bounded_advisory_requires_fixed_in", field="fixed_in",
                    hint="Add the first version outside the affected range that resolves the issue.")
            fixed = semver(record["fixed_in"], "fixed_in")
            safe = fixed >= upper if not upper_inclusive else fixed > upper
            require(safe, "fixed_in must be above the affected range",
                    code="fixed_in_not_above_range", field="fixed_in",
                    hint="Set fixed_in at or above an exclusive upper bound, or above an inclusive bound.")
            require(not range_matches(parsed, fixed), "fixed_in remains inside affects",
                    code="fixed_in_inside_range", field="fixed_in",
                    hint="Choose a fixed_in version that does not satisfy affects.")
    elif "fixed_in" in record:
        require(False, "fixed_in requires an affects range", code="fixed_in_requires_affects",
                field="fixed_in", hint="Add affects before declaring a fixed_in target.")
    return record


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key", code="duplicate_json_key")
        result[key] = value
    return result


def parse(data, limit=MAX_MESSAGE):
    require(len(data) <= limit, "JSON exceeds size limit", code="json_exceeds_size_limit")
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                ProtocolError("non-finite JSON number", code="non_finite_json_number")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("invalid UTF-8 JSON", code="invalid_utf_8_json") from exc
    require(isinstance(result, dict), "JSON must be an object", code="json_must_be_an_object")
    return result


def read_input(path, limit=MAX_DRAFT):
    if path == "-":
        data = sys.stdin.buffer.read(limit + 1)
    else:
        with open(path, "rb") as stream:
            data = stream.read(limit + 1)
    require(len(data) <= limit, "input exceeds size limit", code="input_exceeds_size_limit")
    return data


def text_field(value, name, maximum, multiline=False):
    safe_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    require(isinstance(value, str) and 0 < len(value) <= maximum,
            f"invalid {name} length or type", code=f"invalid_{safe_name}_length",
            field=safe_name, hint=f"Use nonempty text within the {maximum}-character limit.")
    require(value.strip() != "", f"empty {name}", code=f"empty_{safe_name}",
            field=safe_name, hint=f"Provide nonempty {name} text.")
    controls = MULTILINE_CONTROLS if multiline else TEXT_CONTROLS
    require(controls.search(value) is None and
            (value.isascii() or not any(unicodedata.category(c) in {"Cf", "Cs", "Zl", "Zp"}
                                       for c in value)), f"invalid control character in {name}",
            code=f"invalid_control_character_in_{safe_name}", field=safe_name,
            hint=f"Remove control and formatting characters from {name}.")
    return value


# Fixed, human-readable rules for the shared format validators. Hints are built
# only from these constants and the field's fixed name, never from the rejected
# value, so a rejection cannot reflect untrusted input.
PATTERN_RULES = {
    IDENT: "1-64 characters: a lowercase ASCII letter first, then lowercase letters, digits, '_', '.' or '-'",
    HANDLE: "1-32 characters: a lowercase ASCII letter first, then lowercase letters, digits or '-'",
    KEY: "1-96 characters from A-Z, a-z, 0-9, '_', '.', ':' or '-'",
    HEX: "exactly 64 lowercase hexadecimal characters (a SHA-256 digest)",
    NONCE: "exactly 32 lowercase hexadecimal characters",
    MID: "an 8-digit sequence number, '-', then the sender handle (for example 00000012-claude)",
}


def pattern(value, regex, name):
    safe_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    rule = PATTERN_RULES.get(regex)
    hint = f"Use a valid {name}: {rule}." if rule else f"Use a valid {name} value."
    require(isinstance(value, str) and regex.fullmatch(value) is not None,
            f"invalid {name}", code=f"invalid_{safe_name}", field=safe_name, hint=hint)
    return value


def integer(value, name, low=0, high=99999999):
    safe_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    require(type(value) is int and low <= value <= high, f"invalid {name}",
            code=f"invalid_{safe_name}", field=safe_name,
            hint=f"Use an integer {low} through {high} for {name}.")
    return value


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_time(value):
    if not isinstance(value, str) or len(value) != 27:
        raise ValueError("malformed")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise ValueError("malformed")
    return parsed


def poll_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def poll_observed_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    require(isinstance(value, str), "invalid timestamp", code="invalid_timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProtocolError("invalid timestamp", code="invalid_timestamp") from exc
    require(parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value, "invalid timestamp", code="invalid_timestamp")


def git_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    env.update(extra or {})
    return env


def git(directory, *args, data=None, extra=None, ok=(0,)):
    proc = subprocess.run(["git", "--git-dir", str(directory), *args],
                          input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=git_env(extra), timeout=30)
    require(proc.returncode in ok, "Git operation failed; no success is implied", code="git_operation_failed_no_success_is_implied")
    return proc.stdout


def work_git(directory, *args, ok=(0,)):
    proc = subprocess.run(["git", "-C", str(directory), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=git_env(), timeout=30)
    require(proc.returncode in ok, "local package Git inspection failed",
            code="local_git_inspection_failed", field="channel",
            hint="Check the local package checkout and its already-fetched refs.")
    return proc.stdout


def validate_index_entry(entry):
    required = {"package_version", "commit", "tag", "manifest_sha256", "protocol_sha256",
                "summary", "advisory", "reason"}
    allowed = required | {"affects", "fixed_in"}
    require(isinstance(entry, dict) and required <= set(entry) <= allowed,
            "invalid release index entry", code="index_invalid", field="index",
            hint="Correct the entry fields to match the RELEASES.json schema.")
    semver(entry["package_version"], "package_version")
    require(isinstance(entry["commit"], str) and re.fullmatch(r"[0-9a-f]{40}", entry["commit"]),
            "invalid release commit", code="index_invalid", field="commit",
            hint="Use the 40-character lowercase commit id.")
    require(entry["tag"] == "v" + entry["package_version"], "release tag/version mismatch",
            code="index_invalid", field="tag", hint="Set tag to v followed by package_version.")
    pattern(entry["manifest_sha256"], HEX, "manifest_sha256")
    pattern(entry["protocol_sha256"], HEX, "protocol_sha256")
    text_field(entry["summary"], "summary", 300)
    record = {key: entry[key] for key in RELEASE_FIELDS if key in entry}
    record["kind"] = "ai-comm-release"
    record["package_version"] = entry["package_version"]
    record["tag"] = entry["tag"]
    record["commit"] = entry["commit"]
    record["manifest_sha256"] = entry["manifest_sha256"]
    record["protocol_sha256"] = entry["protocol_sha256"]
    record["advisory"] = entry["advisory"]
    record["reason"] = entry["reason"]
    if "affects" in entry:
        record["affects"] = entry["affects"]
    if "fixed_in" in entry:
        record["fixed_in"] = entry["fixed_in"]
    validate_release_record(record)
    return entry


HANDOFF_FIELDS = {"kind", "from_protocol_sha256", "to_protocol_sha256",
                  "to_package_version", "task", "reviewers", "participants",
                  "new_channel_hint", "reason"}


def validate_handoff(record, *, target_protocol_digest=None):
    require(isinstance(record, dict) and set(record) == HANDOFF_FIELDS,
            "invalid migration handoff", code="invalid_handoff_artifact", field="artifact")
    require(record["kind"] == "ai-comm-handoff", "invalid migration handoff kind",
            code="invalid_handoff_kind", field="artifact")
    pattern(record["from_protocol_sha256"], HEX, "protocol digest")
    pattern(record["to_protocol_sha256"], HEX, "protocol digest")
    if target_protocol_digest is not None:
        require(record["to_protocol_sha256"] == target_protocol_digest,
                "handoff targets a different protocol",
                code="predecessor_target_protocol_mismatch",
                field="artifact")
    semver(record["to_package_version"], "package_version")
    text_field(record["task"], "task", 2000, True)
    reviewers = record["reviewers"]
    require(isinstance(reviewers, list) and 2 <= len(reviewers) <= 16,
            "invalid handoff reviewers", code="invalid_handoff_reviewers", field="artifact")
    for handle in reviewers:
        pattern(handle, HANDLE, "reviewer")
    require(len(set(reviewers)) == len(reviewers), "duplicate handoff reviewer",
            code="invalid_handoff_reviewers", field="artifact")
    participants = record["participants"]
    require(isinstance(participants, list) and 1 <= len(participants) <= 32,
            "invalid handoff participants", code="invalid_handoff_participants", field="artifact")
    seen = set()
    for participant in participants:
        require(isinstance(participant, dict) and
                set(participant) == {"handle", "status", "reason"},
                "invalid handoff participant", code="invalid_handoff_participants", field="artifact")
        handle = pattern(participant["handle"], HANDLE, "handle")
        require(handle not in seen, "duplicate handoff participant",
                code="invalid_handoff_participants", field="artifact")
        seen.add(handle)
        require(participant["status"] in {"will_upgrade", "stays_behind"},
                "invalid handoff participant status", code="invalid_handoff_participants", field="artifact")
        text_field(participant["reason"], "reason", 2000, True)
        require(participant["status"] != "stays_behind" or handle not in reviewers,
                "reviewers cannot stay behind", code="handoff_reviewer_cannot_stay_behind",
                field="artifact")
    require(set(reviewers) <= seen, "handoff omits a reviewer",
            code="invalid_handoff_participants", field="artifact")
    text_field(record["new_channel_hint"], "channel", 2000, True)
    text_field(record["reason"], "reason", 8000, True)
    return record


def predecessor_handoff_error(exc):
    code = exc.details.get("code")
    mapped = {
        "invalid_artifact_digest": "predecessor_handoff_digest_invalid",
        "invalid_handoff_kind": "predecessor_handoff_invalid_kind",
        "invalid_protocol_digest": "predecessor_handoff_protocol_digest_invalid",
        "invalid_handoff_reviewers": "predecessor_handoff_reviewers_invalid",
        "invalid_handoff_participants": "predecessor_handoff_participants_invalid",
        "invalid_handle": "predecessor_handoff_participants_invalid",
        "handoff_reviewer_cannot_stay_behind": "predecessor_reviewer_cannot_stay_behind",
        "predecessor_target_protocol_mismatch": "predecessor_target_protocol_mismatch",
    }.get(code, "predecessor_handoff_invalid")
    return ProtocolError("predecessor handoff is invalid", code=mapped,
                         field="artifact")


def predecessor_snapshot_error(exc):
    code = exc.details.get("code")
    mapped = {
        "artifact_hash_mismatch": "predecessor_handoff_tampered",
        "all_required_reviewers_must_accept_this_exact_proposal": "predecessor_acceptance_missing",
        "only_configured_reviewers_may_record_agreement": "predecessor_decision_reviewer_invalid",
    }.get(code, "predecessor_invalid_channel")
    return ProtocolError("predecessor is not a valid protocol-2 ledger",
                         code=mapped, field="channel")


def validate_predecessor_record(predecessor, artifacts, protocol_digest, task, reviewers):
    try:
        _validate_predecessor_record(predecessor, artifacts, protocol_digest, task, reviewers)
    except ProtocolError as exc:
        if exc.details.get("code", "").startswith("predecessor_"):
            raise
        raise ProtocolError("linked predecessor record is invalid",
                            code="predecessor_invalid_link", field="channel") from exc


def _validate_predecessor_record(predecessor, artifacts, protocol_digest, task, reviewers):
    required = {"channel_head", "protocol_sha256", "handoff_artifact", "decide_message"}
    require(isinstance(predecessor, dict) and set(predecessor) == required,
            "invalid predecessor link", code="predecessor_invalid_link", field="channel")
    require(isinstance(predecessor["channel_head"], str) and
            re.fullmatch(r"[0-9a-f]{40,64}", predecessor["channel_head"]),
            "invalid predecessor head", code="predecessor_invalid_head", field="channel")
    pattern(predecessor["protocol_sha256"], HEX, "protocol digest")
    pattern(predecessor["handoff_artifact"], HEX, "artifact digest")
    pattern(predecessor["decide_message"], MID, "message id")
    raw = artifacts.get(predecessor["handoff_artifact"])
    require(raw is not None and sha(raw) == predecessor["handoff_artifact"],
            "predecessor handoff is missing", code="predecessor_handoff_missing", field="artifact")
    handoff = parse(raw, MAX_ARTIFACT)
    require(canonical(handoff) == raw, "predecessor handoff is not canonical",
            code="predecessor_handoff_invalid", field="artifact")
    try:
        validate_handoff(handoff, target_protocol_digest=protocol_digest)
    except ProtocolError as exc:
        raise predecessor_handoff_error(exc) from exc
    require(handoff["from_protocol_sha256"] == predecessor["protocol_sha256"],
            "predecessor protocol differs from handoff",
            code="predecessor_source_protocol_mismatch",
            field="channel")
    require(handoff["task"] == task and set(handoff["reviewers"]) == set(reviewers),
            "predecessor task or reviewers differ from handoff",
            code="predecessor_policy_mismatch", field="channel")
    require(semver(handoff["to_package_version"]) <= semver(VERSION),
            "package is older than handoff requirement", code="predecessor_package_too_old",
            field="channel")


def package_is_checkout(repo):
    # Only the package's own checkout counts; never let Git search parent directories.
    if not (repo / ".git").exists():
        return False
    try:
        top = work_git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n").decode()
    except ProtocolError:
        return False
    return bool(top) and Path(top).resolve() == repo


def _archive_package_identity(repo):
    """Identify an extracted package only when its installed manifest verifies."""
    try:
        version_src = (repo / "ai_comm.py").read_text(encoding="utf-8")
        match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', version_src, re.MULTILINE)
        require(match is not None, "invalid archive identity", code="archive_version_invalid")
        version = match.group(1)
        semver(version, "package_version")
        manifest_raw = (repo / "RELEASE-MANIFEST.json").read_bytes()
        manifest = parse(manifest_raw, MAX_ARTIFACT)
        require(isinstance(manifest, dict) and
                manifest.get("format") == "ai-comm-review-manifest/1" and
                isinstance(manifest.get("files"), list),
                "invalid archive identity", code="archive_manifest_invalid")
        require(isinstance(manifest.get("protocol_sha256"), str) and
                HEX.fullmatch(manifest["protocol_sha256"]),
                "invalid archive identity", code="archive_protocol_invalid")
        seen = set()
        for entry in manifest["files"]:
            require(isinstance(entry, dict) and set(entry) == {"path", "bytes", "sha256"},
                    "invalid archive identity", code="archive_manifest_entry_invalid")
            name = entry["path"]
            require(isinstance(name, str) and name and "\\" not in name and "\x00" not in name,
                    "invalid archive identity", code="archive_manifest_path_invalid")
            relative = PurePosixPath(name)
            require(not relative.is_absolute() and name == relative.as_posix() and
                    all(part not in {"", ".", ".."} for part in relative.parts) and
                    name != "RELEASE-MANIFEST.json" and name not in seen,
                    "invalid archive identity", code="archive_manifest_path_invalid")
            seen.add(name)
            target = repo.joinpath(*relative.parts)
            cursor = repo
            for part in relative.parts:
                cursor = cursor / part
                require(not cursor.is_symlink(), "invalid archive identity",
                        code="archive_manifest_symlink")
            content = target.read_bytes()
            require(target.is_file() and type(entry["bytes"]) is int and
                    entry["bytes"] == len(content) and isinstance(entry["sha256"], str) and
                    HEX.fullmatch(entry["sha256"]) and sha(content) == entry["sha256"],
                    "invalid archive identity", code="archive_manifest_mismatch")
        require("ai_comm.py" in seen and "PROTOCOL.md" in seen,
                "invalid archive identity", code="archive_manifest_incomplete")
        protocol = (repo / "PROTOCOL.md").read_bytes()
        require(sha(protocol) == manifest["protocol_sha256"],
                "invalid archive identity", code="archive_protocol_mismatch")
        return {"version": version, "commit": None, "source": "archive",
                "manifest_sha256": sha(manifest_raw),
                "protocol_sha256": sha(protocol)}
    except ProtocolError as exc:
        return {"version": None, "commit": None, "source": "unknown",
                "manifest_sha256": None, "protocol_sha256": None,
                "reason": exc.details["code"]}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        return {"version": None, "commit": None, "source": "unknown",
                "manifest_sha256": None, "protocol_sha256": None,
                "reason": "archive_identity_unavailable"}


def package_identity(directory=None):
    repo = Path(directory or Path(__file__).resolve().parent).resolve()
    if not package_is_checkout(repo):
        # Extracted release archive: validate installed files; there is no commit.
        return _archive_package_identity(repo)
    version_src = work_git(repo, "show", "HEAD:ai_comm.py").decode("utf-8")
    match = re.search(r'^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', version_src, re.MULTILINE)
    require(match is not None, "package version missing from HEAD", code="package_version_missing",
            field="channel", hint="Use a package HEAD with a valid VERSION constant.")
    version = match.group(1)
    semver(version, "package_version")
    manifest = work_git(repo, "show", "HEAD:RELEASE-MANIFEST.json")
    protocol = work_git(repo, "show", "HEAD:PROTOCOL.md")
    head = work_git(repo, "rev-parse", "HEAD").decode().strip()
    dirty = bool(work_git(repo, "status", "--porcelain=v1", "--untracked-files=normal"))
    return {"version": version, "commit": head, "source": "git", "dirty": dirty,
            "manifest_sha256": sha(manifest), "protocol_sha256": sha(protocol)}


def release_status(directory=None):
    repo = Path(directory or Path(__file__).resolve().parent).resolve()
    try:
        return _release_status(repo)
    except Exception:
        # Release metadata is advisory only and must never affect channel operations.
        try:
            identity = package_identity(repo)
            version, head = identity.get("version"), identity.get("commit")
        except Exception:
            version, head = None, None
        return {"status": "index_invalid", "pinned_version": version, "head": head,
                "advice": [], "message": "release index could not be safely validated; no advice given",
                "authenticity": "not verified (no attestation)"}


def _protocol_transition_fields(protocol_digest, local_protocol_digest):
    """Describe compatibility only when both digests are available and verified."""
    if (not isinstance(protocol_digest, str) or not HEX.fullmatch(protocol_digest) or
            not isinstance(local_protocol_digest, str) or not HEX.fullmatch(local_protocol_digest)):
        return {"protocol_changed": None, "channel_action": "unknown"}
    changed = protocol_digest != local_protocol_digest
    return {"protocol_changed": changed,
            "channel_action": "new_channel_required" if changed else "unchanged"}


def _annotate_release_entries(items, verified_releases, local_protocol_digest):
    verified = {item.get("package_version"): item for item in verified_releases
                if isinstance(item, dict) and isinstance(item.get("package_version"), str)}
    result = []
    for item in items:
        known = verified.get(item.get("package_version")) if isinstance(item, dict) else None
        fields = _protocol_transition_fields(
            known.get("protocol_sha256") if known else None, local_protocol_digest)
        result.append(dict(item, **fields))
    return result


def _release_status(repo):
    if not package_is_checkout(repo):
        identity = package_identity(repo)
        return {"status": "not_git_checkout", "pinned_version": identity.get("version"),
                "head": None, "advice": [],
                "message": "package is not a Git checkout (for example an extracted release "
                           "archive); release status needs a Git checkout of a tagged release; "
                           "no advice given",
                "authenticity": "not verified (no attestation)"}
    partial = work_git(repo, "config", "--get-regexp",
                       r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$",
                       ok=(0, 1)).strip()
    if partial:
        version_output = subprocess.run(["git", "--version"], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=git_env(), timeout=10).stdout
        version_match = re.search(rb"git version (\d+)\.(\d+)", version_output)
        require(version_match is not None and tuple(map(int, version_match.groups())) >= (2, 44),
                "partial clone cannot be inspected without lazy fetching",
                code="partial_clone_release_check_unsupported", field="channel",
                hint="Use Git 2.44 or newer so release checks can disable lazy fetching.")
    identity = package_identity(repo)
    ref = "refs/remotes/origin/release-index"
    exists = work_git(repo, "show-ref", "--verify", ref, ok=(0, 128)).strip()
    if not exists:
        return {"status": "index_missing", "pinned_version": identity["version"],
                "head": identity["commit"], "advice": [],
                "message": "release index is missing or has not been fetched; no advice given",
                "authenticity": "not verified (no attestation)"}
    raw = work_git(repo, "show", f"{ref}:RELEASES.json")
    try:
        index = parse(raw, MAX_ARTIFACT)
    except ProtocolError:
        return {"status": "index_invalid", "pinned_version": identity["version"],
                "head": identity["commit"], "advice": [],
                "message": "release index is invalid; no advice given",
                "authenticity": "not verified (no attestation)"}
    if set(index) != {"format", "releases"} or index.get("format") != "ai-comm-releases/1" or not isinstance(index.get("releases"), list):
        return {"status": "index_invalid", "pinned_version": identity["version"],
                "head": identity["commit"], "advice": [],
                "message": "release index is invalid; no advice given",
                "authenticity": "not verified (no attestation)"}
    entries = []
    try:
        seen_versions = set()
        for entry in index["releases"]:
            validate_index_entry(entry)
            require(entry["package_version"] not in seen_versions, "duplicate release version",
                    code="index_invalid", field="package_version",
                    hint="Keep exactly one index entry per package version.")
            seen_versions.add(entry["package_version"])
            entries.append(entry)
    except (ProtocolError, UnicodeError):
        return {"status": "index_invalid", "pinned_version": identity["version"],
                "head": identity["commit"], "advice": [],
                "message": "release index schema validation failed; no advice given",
                "authenticity": "not verified (no attestation)"}
    missing_tags = []
    try:
        for entry in entries:
            tag = entry["tag"]
            tag_ref = f"refs/tags/{tag}"
            local_ref = work_git(repo, "show-ref", "--verify", tag_ref, ok=(0, 1, 128)).strip()
            if not local_ref:
                missing_tags.append(entry)
                continue
            tag_type = work_git(repo, "cat-file", "-t", tag_ref).decode().strip()
            require(tag_type == "tag", "release tag is not annotated", code="index_inconsistent",
                    field="tag", hint="Publish an annotated tag for the indexed release.")
            target = work_git(repo, "rev-parse", f"{tag_ref}^{{}}").decode().strip()
            require(target == entry["commit"], "release tag target differs from index",
                    code="index_inconsistent", field="commit",
                    hint="Correct the index to match the annotated tag target.")
            tag_object = work_git(repo, "cat-file", "tag", tag_ref).decode("utf-8", "replace")
            manifest_pattern = r"(?i)\bmanifest(?:[- ]sha256)?\s*[:=]?\s*" + re.escape(entry["manifest_sha256"]) + r"(?:\b|$)"
            require(re.search(manifest_pattern, tag_object) is not None,
                    "release tag manifest differs from index",
                    code="index_inconsistent", field="manifest_sha256",
                    hint="Correct the index to match the manifest digest in the tag message.")
            tagged_manifest = work_git(repo, "show", f"{tag_ref}^{{}}:RELEASE-MANIFEST.json")
            require(sha(tagged_manifest) == entry["manifest_sha256"],
                    "release tag manifest file differs from index",
                    code="index_inconsistent", field="manifest_sha256",
                    hint="Correct the index to match the manifest file in the tagged commit.")
            tagged_protocol = work_git(repo, "show", f"{tag_ref}^{{}}:PROTOCOL.md")
            require(sha(tagged_protocol) == entry["protocol_sha256"],
                    "release tag protocol differs from index", code="index_inconsistent",
                    field="protocol_sha256",
                    hint="Correct the index to match the protocol digest in the tagged commit.")
    except (ProtocolError, UnicodeError):
        return {"status": "index_inconsistent",
                "pinned_version": identity["version"], "head": identity["commit"],
                "advice": [], "message": "release index validation failed; no advice given",
                "authenticity": "not verified (no attestation)"}
    if not entries:
        return {"status": "index_invalid", "pinned_version": identity["version"],
                "head": identity["commit"], "advice": [],
                "message": "release index contains no releases; no advice given",
                "authenticity": "not verified (no attestation)"}
    pinned_missing = any(entry["package_version"] == identity["version"]
                        for entry in missing_tags)
    latest_indexed = max(entries, key=lambda entry: semver(entry["package_version"]))
    missing_latest_target = (semver(latest_indexed["package_version"]) >
                             semver(identity["version"]) and
                             latest_indexed in missing_tags)
    verified_entries = [entry for entry in entries if entry not in missing_tags]
    if pinned_missing or missing_latest_target:
        return {"status": "index_tags_missing",
                "pinned_version": identity["version"], "head": identity["commit"],
                "advice": [],
                "missing_tags": [{"package_version": entry["package_version"],
                                  "tag": entry["tag"]} for entry in missing_tags],
                "message": "one or more release tags are not present locally; no advice given",
                "authenticity": "not verified (no attestation)"}
    latest = max(verified_entries, key=lambda entry: semver(entry["package_version"]),
                 default=latest_indexed)
    verified_releases = [{"package_version": entry["package_version"],
                          "manifest_sha256": entry["manifest_sha256"],
                          "protocol_sha256": entry["protocol_sha256"],
                          "commit": entry["commit"], "tag": entry["tag"],
                          "advisory": entry["advisory"],
                          "summary": entry["summary"], "reason": entry["reason"],
                          **({"affects": entry["affects"]} if "affects" in entry else {}),
                          **({"fixed_in": entry["fixed_in"]} if "fixed_in" in entry else {})}
                         for entry in verified_entries]
    advice = [{"package_version": entry["package_version"], "advisory": entry["advisory"],
               "summary": entry["summary"], "reason": entry["reason"]}
              for entry in verified_entries
              if semver(entry["package_version"]) > semver(identity["version"])]
    active_advisories = []
    local_version = semver(identity["version"])
    for entry in verified_entries:
        if entry["advisory"] not in {"recommended", "required"}:
            continue
        parsed, _, _ = parse_affects(entry["affects"])
        if range_matches(parsed, local_version):
            active_advisories.append({"package_version": entry["package_version"],
                                      "advisory": entry["advisory"],
                                      "summary": entry["summary"], "reason": entry["reason"],
                                      "fixed_in": entry.get("fixed_in")})
    return {"status": "ok", "pinned_version": identity["version"],
            "latest_version": latest["package_version"], "head": identity["commit"],
            "advice": _annotate_release_entries(advice, verified_releases,
                                                identity["protocol_sha256"]),
            "active_advisories": _annotate_release_entries(
                active_advisories, verified_releases, identity["protocol_sha256"]),
            "verified_releases": verified_releases,
            "missing_tags": [{"package_version": entry["package_version"],
                              "tag": entry["tag"]} for entry in missing_tags],
            "authenticity": "not verified (no attestation)"}


def _upgrade_error(message, code, field, hint, details=None):
    raise ProtocolError(message, code=code, field=field, hint=hint, details=details)


def _upgrade_channel_path(value):
    require(isinstance(value, str) and value, "invalid upgrade channel path",
            code="upgrade_channel_invalid", field="channel",
            hint="Pass an initialized protocol-compatible channel path.")
    raw = Path(value)
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    # Refuse parent traversal instead of normalizing it across a possible symlink.
    if ".." in raw.parts:
        _upgrade_error("upgrade channel path is invalid", "upgrade_channel_invalid", "channel",
                       "Pass a normalized path without parent traversal.")
    cursor = Path(raw.anchor)
    for part in raw.parts[1:]:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            _upgrade_error("upgrade channel path contains a symlink", "upgrade_channel_symlink",
                           "channel", "Use a path whose components are not symlinks.")
    return raw


def _parse_upgrade_participants(values, participants, reviewers):
    supplied = {}
    require(len(values) <= 32, "too many upgrade participant statuses",
            code="upgrade_participant_malformed", field="participant",
            hint="Pass at most one status for each current participant.")
    current = set(participants)
    for value in values:
        if (not isinstance(value, str) or len(value) > 80 or "=" not in value):
            _upgrade_error("upgrade participant status is malformed",
                           "upgrade_participant_malformed", "participant",
                           "Use HANDLE=will_upgrade or HANDLE=stays_behind.")
        handle, status = value.split("=", 1)
        if HANDLE.fullmatch(handle) is None:
            _upgrade_error("upgrade participant status is malformed",
                           "upgrade_participant_malformed", "participant",
                           "Use a current lowercase handle and a documented status.")
        if not status:
            _upgrade_error("upgrade participant status is missing",
                           "upgrade_participant_malformed", "participant",
                           "Use HANDLE=will_upgrade or HANDLE=stays_behind.")
        if handle in supplied:
            _upgrade_error("duplicate upgrade participant status",
                           "upgrade_participant_duplicate", "participant",
                           "Give each current participant exactly one status.")
        if handle not in current and handle in reviewers:
            _upgrade_error("required reviewer has not joined the predecessor channel",
                           "upgrade_participant_missing", "participant",
                           "Have the configured reviewer join the old channel, then regenerate the plan.")
        if handle not in current:
            _upgrade_error("unknown upgrade participant",
                           "upgrade_participant_unknown", "participant",
                           "Use only handles listed by the selected channel.")
        if status not in {"will_upgrade", "stays_behind"}:
            _upgrade_error("invalid upgrade participant status",
                           "upgrade_participant_status_invalid", "participant",
                           "Use will_upgrade or stays_behind.")
        if handle in reviewers and status == "stays_behind":
            _upgrade_error("reviewers cannot stay behind in a channel migration",
                           "upgrade_reviewer_stays_behind", "participant",
                           "Every configured reviewer must be marked will_upgrade.")
        supplied[handle] = status
    return supplied


def upgrade_plan(target_version=None, channel_path=None, participant_values=(), directory=None):
    """Return a read-only plan from already verified local release metadata."""
    repo = Path(directory or Path(__file__).resolve().parent).resolve()
    status = release_status(repo)
    if status.get("status") == "index_tags_missing":
        _upgrade_error("local release tags are incomplete; Gate 1 is needed",
                       "upgrade_plan_gate1_required", "target",
                       "After the owner's separate Gate 1 approval, refresh release refs with the bounded helper, then retry.")
    if status.get("status") != "ok":
        _upgrade_error("verified release information is unavailable",
                       "upgrade_plan_unavailable", "target",
                       "Check the local release-index checkout and retry when release-status is ok.",
                       {"status": status.get("status")})
    identity = package_identity(repo)
    pinned = semver(identity["version"], "package_version")
    releases = status.get("verified_releases", [])
    by_version = {entry["package_version"]: entry for entry in releases}
    if target_version is None:
        newer = [entry for entry in releases
                 if semver(entry["package_version"]) > pinned]
        if not newer:
            _upgrade_error("no newer verified release is available", "upgrade_target_not_newer",
                           "target", "Choose a newer version after fetching an authorized release index.")
        target = max(newer, key=lambda entry: semver(entry["package_version"]))
        target_version = target["package_version"]
    else:
        try:
            target_tuple = semver(target_version, "target")
        except ProtocolError:
            _upgrade_error("invalid upgrade target version", "invalid_upgrade_target", "target",
                           "Use a three-part package version such as 1.2.3 without leading zeroes.")
        if target_tuple <= pinned:
            _upgrade_error("upgrade target is not newer than the pinned package",
                           "upgrade_target_not_newer", "target",
                           "Choose a package version newer than the pinned version.")
        target = by_version.get(target_version)
        if target is None:
            if any(item.get("package_version") == target_version
                   for item in status.get("missing_tags", [])):
                _upgrade_error("target release tag is not available locally; Gate 1 is needed",
                               "upgrade_plan_gate1_required", "target",
                               "After the owner's separate Gate 1 approval, refresh release refs with the bounded helper, then retry.")
            _upgrade_error("upgrade target is not locally verified", "upgrade_target_unverified",
                           "target", "Choose a version listed in verified_releases.")

    transition = _protocol_transition_fields(target.get("protocol_sha256"),
                                             identity.get("protocol_sha256"))
    active = status.get("active_advisories", [])
    severity = {"none": 0, "optional": 1, "recommended": 2, "required": 3}
    selected = max(active, key=lambda entry: (
        severity.get(entry.get("advisory"), 0),
        semver(entry["package_version"])), default=None)
    if selected:
        applicable = {"advisory": selected["advisory"], "reason": selected["reason"]}
        fixed_in = selected.get("fixed_in")
        applicable["fixed_in"] = fixed_in
    else:
        applicable = {"advisory": "none", "reason": "No applicable advisory.", "fixed_in": None}
        fixed_in = None
    owner_fixed = fixed_in or "none"
    protocol_line = ("unchanged, so this channel keeps working" if transition["protocol_changed"] is False
                     else "changed, so a new channel is needed" if transition["protocol_changed"] is True
                     else "unknown; channel compatibility has not been verified")
    owner_notice = (
        f"ai-comm release notice. Pinned: {identity['version']} at {identity['commit']}. "
        f"Available: {target['package_version']}. Advisory: {applicable['advisory']}, "
        f"which applies to this version: {applicable['reason']}; fixed in {owner_fixed}. "
        f"Protocol: {protocol_line}. Steps: use the tag already fetched at Gate 1, verify it is annotated "
        f"and its message names manifest {target['manifest_sha256']}, check out the tag, run "
        f"python3 scripts/check_tag.py {target['tag']}, scripts/check_release_manifest.py and "
        f"the test suite, update the project's activation note, announce on the channel. "
        "Authenticity: not verified (no attestation). Apply? yes/no")
    repin_steps = [
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
         f"refs/tags/{target['tag']}"] ,
        ["git", "-C", str(repo), "checkout", "--detach", f"refs/tags/{target['tag']}^{{commit}}"],
        ["python3", str(repo / "scripts" / "check_tag.py"), target["tag"]],
        ["python3", str(repo / "scripts" / "check_release_manifest.py")],
        ["python3", "-m", "unittest", "discover", "-s", str(repo / "tests")],
    ]
    result = {
        "from": identity["version"], "to": target["package_version"],
        "commit": target["commit"], "tag": target["tag"],
        "manifest_sha256": target["manifest_sha256"],
        "protocol_sha256": target["protocol_sha256"],
        **transition,
        "applicable_advisory": applicable,
        "fixed_in": fixed_in,
        "authenticity": "not verified (no attestation)",
        "repin_steps": repin_steps,
        "owner_notice": owner_notice,
    }
    if channel_path is None:
        require(not participant_values, "participant statuses require --channel",
                code="upgrade_participant_requires_channel", field="participant",
                hint="Supply --channel with --participant, or omit participant statuses.")
        return result

    safe_path = _upgrade_channel_path(channel_path)
    if not safe_path.is_dir() or not ((safe_path / "HEAD").is_file() or
                                      (safe_path / ".git").exists()):
        _upgrade_error("upgrade channel path is not an initialized channel",
                       "upgrade_channel_invalid", "channel",
                       "Pass an existing initialized bare channel repository.")
    bare = work_git(safe_path, "rev-parse", "--is-bare-repository", ok=(0, 128)).strip()
    if bare != b"true":
        _upgrade_error("upgrade channel path is not a bare repository",
                       "upgrade_channel_not_bare", "channel",
                       "Pass the initialized bare channel repository.")
    try:
        channel = Channel(safe_path)
        channel_state = channel.snapshot()
    except ProtocolError as exc:
        code = exc.details.get("code")
        if code == "channel_must_be_a_dedicated_bare_git_repository":
            _upgrade_error("upgrade channel path is not a bare repository",
                           "upgrade_channel_not_bare", "channel",
                           "Pass the initialized bare channel repository.")
        if code == "channel_symlink_refused":
            _upgrade_error("upgrade channel path contains a symlink", "upgrade_channel_symlink",
                           "channel", "Use a path whose components are not symlinks.")
        _upgrade_error("upgrade channel path is not a compatible ai-comm channel",
                       "upgrade_channel_invalid", "channel",
                       "Pass a valid channel using this package's protocol.")

    if transition["channel_action"] == "new_channel_required":
        config = channel_state.config
        current_handles = sorted(channel_state.current)
        reviewers = list(config["reviewers"])
        participants_unjoined = sorted(set(reviewers) - set(current_handles))
        supplied = _parse_upgrade_participants(participant_values, current_handles, reviewers)
        hint = str(channel.path.with_name(channel.path.name + "-upgraded"))
        handoff_fields = {
            "kind": "ai-comm-handoff",
            "from_protocol_sha256": config["protocol_sha256"],
            "to_protocol_sha256": target["protocol_sha256"],
            "to_package_version": target["package_version"],
            "task": config["task"],
            "reviewers": reviewers,
            "new_channel_hint": hint,
            "reason": "The target release uses a different verified protocol digest.",
        }
        required_handles = sorted(set(current_handles) | set(participants_unjoined))
        outstanding = [handle for handle in required_handles if handle not in supplied]
        if outstanding:
            migration = {"complete": False, "handoff_fields": handoff_fields,
                         "participants_outstanding": outstanding,
                         "participants_unjoined": participants_unjoined,
                         "reviewer_join_required": bool(participants_unjoined),
                         "choices": ["will_upgrade", "stays_behind (non-reviewers only)"]}
        else:
            participants = [{
                "handle": handle,
                "status": supplied[handle],
                "reason": ("Marked will_upgrade in this plan." if supplied[handle] == "will_upgrade"
                           else "Marked stays_behind in this plan."),
            } for handle in current_handles]
            handoff = dict(handoff_fields, participants=participants)
            try:
                validate_handoff(handoff, target_protocol_digest=target["protocol_sha256"])
            except ProtocolError as exc:
                if exc.details.get("code") == "handoff_reviewer_cannot_stay_behind":
                    _upgrade_error("reviewers cannot stay behind in a channel migration",
                                   "upgrade_reviewer_stays_behind", "participant",
                                   "Every configured reviewer must be marked will_upgrade.")
                raise
            handoff_digest = sha(canonical(handoff))
            new_channel = hint
            migration = {
                "complete": True,
                "participants_unjoined": [],
                "reviewer_join_required": False,
                "handoff": handoff,
                "handoff_sha256": handoff_digest,
                "steps": [
                    {"kind": "manual", "text": f"PROPOSE artifact {handoff_digest} in the predecessor channel."},
                    {"kind": "manual", "text": f"Each configured reviewer ACCEPTs artifact {handoff_digest}."},
                    {"kind": "manual", "text": "A configured reviewer DECIDEs the accepted proposal."},
                    {"kind": "manual", "text": f"After owner authorization, re-pin to {target['tag']}."},
                    {"kind": "command", "argv": ["python3", str(Path(__file__).resolve()),
                     "--channel", new_channel, "init", "--task", config["task"],
                     "--reviewers", *reviewers, "--predecessor", str(channel.path),
                     "--handoff", handoff_digest]},
                ],
            }
        result["migration"] = migration
    elif participant_values:
        _upgrade_error("participant statuses are only used for protocol migration",
                       "upgrade_participant_not_required", "participant",
                       "Omit --participant when the target protocol is unchanged.")
    return result


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    require(not path.is_symlink(), "runtime symlink refused", code="runtime_symlink_refused")
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_snapshot_blobs(repo):
    head = git(repo, "rev-parse", "--verify", REF).decode().strip()
    require(not git(repo, "log", "--format=", "--name-only", "--diff-filter=MDRT", head).strip(),
            "channel history modified or removed a durable record", code="channel_history_modified_or_removed_a_durable_record")
    entries = git(repo, "ls-tree", "-r", "-z", head).split(b"\0")
    tree = {}
    for entry in entries:
        if not entry:
            continue
        prefix, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = prefix.decode().split()
        require(mode == "100644" and kind == "blob", "unexpected channel entry type", code="unexpected_channel_entry_type")
        path = raw_path.decode("ascii")
        require(path == "channel.json" or re.fullmatch(
            r"messages/[0-9]{8}-[a-z][a-z0-9-]{0,31}\.json|"
            r"sessions/[a-z][a-z0-9-]{0,31}/[0-9]{8}\.json|"
            r"artifacts/[0-9a-f]{64}", path), "unexpected channel entry path", code="unexpected_channel_entry_path")
        tree[path] = oid
    require("channel.json" in tree, "not an ai-comm channel", code="not_an_ai_comm_channel")
    request = ("\n".join(tree[p] for p in tree) + "\n").encode()
    sizes = git(repo, "cat-file", "--batch-check", data=request).splitlines()
    require(len(sizes) == len(tree), "invalid Git size response", code="invalid_git_size_response")
    total = 0
    for path, line in zip(tree, sizes):
        parts = line.split()
        require(len(parts) == 3 and parts[1] == b"blob", "invalid Git size response", code="invalid_git_size_response")
        length = int(parts[2])
        bound = MAX_ARTIFACT if path.startswith("artifacts/") else MAX_MESSAGE
        require(length <= bound, "stored object exceeds limit", code="stored_object_exceeds_limit")
        total += length
    require(total <= 64 * 1024 * 1024,
            "channel snapshot exceeds 64 MiB limit; archive explicitly",
            code="channel_snapshot_exceeds_64_mib_limit_archive_explicitly")
    names = list(tree)
    raw = git(repo, "cat-file", "--batch", data=("\n".join(tree[p] for p in names) + "\n").encode())
    blobs, offset = {}, 0
    for path in names:
        end = raw.find(b"\n", offset)
        require(end >= offset, "truncated Git object response", code="truncated_git_object_response")
        fields = raw[offset:end].split()
        require(len(fields) == 3 and fields[1] == b"blob", "invalid Git object response", code="invalid_git_object_response")
        size = int(fields[2])
        require(size <= MAX_ARTIFACT, "stored object exceeds limit", code="stored_object_exceeds_limit")
        offset = end + 1
        blobs[path] = raw[offset:offset + size]
        require(len(blobs[path]) == size and raw[offset + size:offset + size + 1] == b"\n",
                "truncated Git object", code="truncated_git_object")
        offset += size + 1
    return head, blobs


class Channel:
    def __init__(self, path):
        raw = Path(path).absolute()
        require(not raw.is_symlink(), "channel symlink refused", code="channel_symlink_refused")
        self.path = raw.resolve()
        require(self.path.is_dir(), "channel does not exist; initialize it explicitly", code="channel_does_not_exist_initialize_it_explicitly")
        require(git(self.path, "rev-parse", "--is-bare-repository").strip() == b"true",
                "channel must be a dedicated bare Git repository", code="channel_must_be_a_dedicated_bare_git_repository")
        self.runtime = self.path / "ai-comm-runtime"
        require(not self.runtime.is_symlink(), "runtime symlink refused", code="runtime_symlink_refused")

    @classmethod
    def init(cls, path, task, reviewers, require_confidence=(), require_sources=(),
             predecessor=None, handoff=None):
        text_field(task, "task", 2000, True)
        require(2 <= len(reviewers) <= 16 and len(set(reviewers)) == len(reviewers),
                "at least two distinct reviewers required", code="at_least_two_distinct_reviewers_required")
        for handle in reviewers:
            pattern(handle, HANDLE, "reviewer")
        require(set(require_confidence) <= CONF_ACTS, "invalid confidence policy", code="invalid_confidence_policy")
        require(set(require_sources) <= ACTS, "invalid source policy", code="invalid_source_policy")
        require((predecessor is None) == (handoff is None),
                "predecessor and handoff must be supplied together",
                code="predecessor_arguments_must_be_paired", field="channel")
        protocol_digest = sha(Path(__file__).with_name("PROTOCOL.md").read_bytes())
        predecessor_info = (validate_predecessor(predecessor, handoff, task, reviewers,
                                                 protocol_digest)
                            if predecessor is not None else None)
        target = Path(path).absolute()
        require(not target.exists() and not target.is_symlink(),
                "init refuses an existing path", code="init_refuses_an_existing_path")
        target.mkdir(mode=0o700, parents=False)
        proc = subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(target)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=git_env(), timeout=30)
        require(proc.returncode == 0, "channel initialization failed", code="channel_initialization_failed")
        channel = cls(target)
        channel.runtime.mkdir(mode=0o700, exist_ok=True)
        policy = {"require_confidence": list(require_confidence),
                  "require_sources": list(require_sources),
                  "protocol_version": PROTOCOL_VERSION}
        changes = {"channel.json": canonical({"kind": "ai-comm-channel", "v": 2,
                  "task": task, "reviewers": reviewers, "policy": policy,
                  "protocol_sha256": protocol_digest})}
        if predecessor_info is not None:
            policy["predecessor"] = predecessor_info["record"]
            changes["channel.json"] = canonical({"kind": "ai-comm-channel", "v": 2,
                  "task": task, "reviewers": reviewers, "policy": policy,
                  "protocol_sha256": protocol_digest})
            changes[f"artifacts/{handoff}"] = predecessor_info["handoff_bytes"]
        # A new owner-only bare repository has no concurrent writers yet.
        head = channel.publish(None, changes, "initialize channel", "system")
        return {"channel": str(channel.path), "head": head, "v": 2}

    @contextlib.contextmanager
    def lock(self):
        # Validate the immutable contract before creating runtime state. The
        # caller re-reads under the lock before acting, so this preflight only
        # prevents a rejected mismatch from leaving a runtime directory behind.
        self.snapshot()
        require(not self.runtime.is_symlink(), "runtime symlink refused", code="runtime_symlink_refused")
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.runtime / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "channel busy; retry with the same key", code="channel_busy_retry_with_the_same_key")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def publish(self, parent, changes, title, author):
        """Commit only supplied blobs, then atomically advance the delivery ref."""
        if parent:
            for path in changes:
                require(not git(self.path, "ls-tree", parent, "--", path).strip(),
                        "durable records are append-only", code="durable_records_are_append_only")
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.runtime) as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index"),
                   "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": author + "@ai-comm.invalid",
                   "GIT_COMMITTER_NAME": "ai-comm", "GIT_COMMITTER_EMAIL": "ai-comm@local.invalid"}
            if parent:
                git(self.path, "read-tree", parent, extra=env)
            else:
                git(self.path, "read-tree", "--empty", extra=env)
            for path, data in changes.items():
                blob = git(self.path, "hash-object", "-w", "--stdin", data=data, extra=env).decode().strip()
                git(self.path, "update-index", "--add", "--cacheinfo", "100644", blob, path, extra=env)
            tree = git(self.path, "write-tree", extra=env).decode().strip()
            args = ["-c", "commit.gpgsign=false", "commit-tree", tree]
            if parent:
                args += ["-p", parent]
            commit = git(self.path, *args, data=(title + "\n").encode(), extra=env).decode().strip()
            self._before_publish(commit)  # failure-injection seam; no external hooks
            expected = parent or "0" * len(commit)
            git(self.path, "update-ref", REF, commit, expected)
            return commit

    def _before_publish(self, commit):
        pass

    def snapshot(self):
        head, blobs = read_snapshot_blobs(self.path)
        return Snapshot(head, blobs)

    def _record_poll(self, handle, session, command):
        """Best-effort process heartbeat; never writes to Git or receipts."""
        warning = {"code": "heartbeat_not_recorded", "reason": "unavailable"}
        try:
            pattern(handle, HANDLE, "handle")
            require(command in {"inbox", "ack", "wait", "send", "state"},
                    "invalid heartbeat command", code="invalid_heartbeat_command")
            initial = self.snapshot()
            identity = initial.identity(handle, session)
            sample = poll_now()
            require(not self.runtime.is_symlink(), "runtime symlink refused",
                    code="runtime_symlink_refused")
            self.runtime.mkdir(mode=0o700, exist_ok=True)
            poll_root = self.runtime / "poll"
            if poll_root.is_symlink():
                return dict(warning, reason="target_symlink")
            try:
                poll_root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            dirfd = os.open(poll_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with self.lock():
                    current = self.snapshot().identity(handle, session)
                    name = handle + ".json"
                    old = None
                    try:
                        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                        if stat.S_ISLNK(st.st_mode):
                            return dict(warning, reason="target_symlink")
                        if not stat.S_ISREG(st.st_mode):
                            return dict(warning, reason="target_not_regular")
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
                        try:
                            data = os.read(fd, 1025)
                        finally:
                            os.close(fd)
                        if len(data) <= 1024:
                            try:
                                value = parse(data, 1024)
                                if (set(value) == {"v", "handle", "generation", "last_poll_utc", "command", "tool_version"}
                                    and type(value["v"]) is int and value["v"] == 1
                                    and value["handle"] == handle
                                    and type(value["generation"]) is int and value["generation"] >= 1
                                    and value["command"] in {"inbox", "ack", "wait", "send", "state"}
                                    and isinstance(value["tool_version"], str)
                                    and SEMVER.fullmatch(value["tool_version"])):
                                    old = (value, poll_time(value["last_poll_utc"]))
                            except (ProtocolError, ValueError, UnicodeError, TypeError, KeyError):
                                pass
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        if exc.errno == errno.ELOOP:
                            return dict(warning, reason="target_symlink")
                    if old and old[0]["generation"] == current["generation"]:
                        sampled_at = poll_time(sample)
                        observed_now = poll_observed_now()
                        if old[1] > sampled_at:
                            future = old[1] > observed_now + timedelta(seconds=5)
                            fresh_sample = sampled_at >= observed_now - timedelta(seconds=5)
                            if not (future and fresh_sample):
                                return None
                    record = {"v": 1, "handle": handle, "generation": current["generation"],
                              "last_poll_utc": sample, "command": command, "tool_version": VERSION}
                    temp = ".poll-" + secrets.token_hex(12)
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=dirfd)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(canonical(record))
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
                        os.fsync(dirfd)
                    finally:
                        try:
                            os.unlink(temp, dir_fd=dirfd)
                        except FileNotFoundError:
                            pass
            finally:
                os.close(dirfd)
            return None
        except Exception as exc:
            reason = "unavailable"
            if isinstance(exc, PermissionError):
                reason = "permission_denied"
            elif isinstance(exc, ProtocolError):
                code = exc.details.get("code")
                if code == "runtime_symlink_refused":
                    reason = "target_symlink"
                elif code == "channel_busy_retry_with_the_same_key":
                    reason = "lock_busy"
                elif code in {"unregistered_or_stale_session", "handle_already_active_explicit_takeover_required"}:
                    reason = "identity_invalid"
            return dict(warning, reason=reason)

    def _poll_status(self, record, stale_after, as_of):
        unknown = {"last_poll_utc": None, "age_seconds": None, "command": None,
                   "running_version": None, "status": "unknown", "reason": "malformed"}
        try:
            poll_root = self.runtime / "poll"
            if poll_root.is_symlink():
                return dict(unknown, reason="symlink")
            dirfd = os.open(poll_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                name = record["handle"] + ".json"
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    return dict(unknown, reason="symlink")
                if not stat.S_ISREG(st.st_mode):
                    return unknown
                if st.st_size > 1024:
                    return dict(unknown, reason="oversized")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
                try:
                    raw = os.read(fd, 1025)
                finally:
                    os.close(fd)
            finally:
                os.close(dirfd)
            if len(raw) > 1024:
                return dict(unknown, reason="oversized")
            value = parse(raw, 1024)
            if (set(value) != {"v", "handle", "generation", "last_poll_utc", "command", "tool_version"}
                or type(value["v"]) is not int or value["v"] != 1
                or value["handle"] != record["handle"]
                or type(value["generation"]) is not int or value["generation"] < 1
                or value["command"] not in {"inbox", "ack", "wait", "send", "state"}
                or not isinstance(value["tool_version"], str) or not SEMVER.fullmatch(value["tool_version"])):
                return unknown
            if value["generation"] != record["generation"]:
                return dict(unknown, reason="generation_mismatch")
            stamp = poll_time(value["last_poll_utc"])
            age = (as_of - stamp).total_seconds()
            if age < -5:
                return dict(unknown, reason="future_timestamp")
            age = max(0, age)
            return {"last_poll_utc": value["last_poll_utc"], "age_seconds": age,
                    "command": value["command"], "running_version": value["tool_version"],
                    "status": "active" if age <= stale_after else "stale", "reason": None}
        except FileNotFoundError:
            return {"last_poll_utc": None, "age_seconds": None, "command": None,
                    "running_version": None, "status": "never", "reason": None}
        except PermissionError:
            return dict(unknown, reason="unreadable")
        except OSError as exc:
            return dict(unknown, reason="symlink" if exc.errno == errno.ELOOP else "unreadable")
        except (ProtocolError, ValueError, UnicodeError, TypeError, KeyError):
            return unknown

    def join(self, handle, platform, session, takeover=False):
        pattern(handle, HANDLE, "handle")
        pattern(platform, HANDLE, "platform")
        text_field(session, "non-secret session reference", 160)
        with self.lock():
            state = self.snapshot()
            current = state.current.get(handle)
            if current and current["platform"] == platform and current["session"] == session:
                require(current["tool_version"] == VERSION,
                        "package version changed; use a fresh session reference and explicit takeover",
                        code="tool_version_changed_requires_fresh_session", field="session",
                        hint="Register the upgraded tool with a fresh conversation reference and authorized takeover.")
                return {"joined": current, "head": state.head, "duplicate": True}
            require(not current or takeover, "handle already active; explicit takeover required", code="handle_already_active_explicit_takeover_required")
            require(not any(record["handle"] == handle and record["session"] == session
                            for record in state.sessions.values()),
                    "takeover requires a never-used session reference for this handle", code="takeover_requires_a_never_used_session_reference_for_this_handle")
            generation = current["generation"] + 1 if current else 1
            record = {"handle": handle, "platform": platform, "session": session,
                      "generation": generation, "ts": now(), "tool_version": VERSION}
            path = f"sessions/{handle}/{generation:08d}.json"
            head = self.publish(state.head, {path: canonical(record)},
                                f"join {handle} generation {generation}", handle)
            return {"joined": record, "head": head, "duplicate": False}

    def send(self, handle, session, key, draft, dry_run=False):
        pattern(key, KEY, "retry key")
        require(len(canonical(draft)) <= MAX_DRAFT, "draft exceeds size limit", code="draft_exceeds_size_limit")
        with self.lock():
            state = self.snapshot()
            current = state.identity(handle, session)
            digest = sha(canonical(draft))
            previous = state.retries.get((handle, current["generation"], key))
            if previous:
                require(previous["draft_sha256"] == digest, "retry key reused with changed draft", code="retry_key_reused_with_changed_draft")
                if dry_run:
                    return {"dry_run": True, "published": False, "duplicate": True,
                            "message": {"id": previous["id"], "seq": previous["seq"],
                                        "ts": previous["ts"], "provisional": False},
                            "head": state.head, "warnings": []}
                return {"message": previous, "head": state.head, "duplicate": True}
            state.validate_draft(draft, handle)
            seq = len(state.messages) + 1
            integer(seq, "sequence", 1)
            message = dict(draft, v=2, id=f"{seq:08d}-{handle}", seq=seq, ts=now(),
                           session=session, generation=current["generation"],
                           key=key, draft_sha256=digest)
            message["from"] = handle
            act = message["act"]
            target = state.by_id.get(message.get("reply_to"))
            if act in {"PING", "PONG"}:
                message["nonce"] = secrets.token_hex(16)
            if act == "PONG" or (act == "ACK" and target and target["act"] in {"PING", "PONG"}):
                message["echo"] = target["nonce"]
            state.add_message(message)  # validates the complete assigned envelope too
            data = canonical(message)
            require(len(data) <= MAX_MESSAGE, "assigned envelope exceeds size limit", code="assigned_envelope_exceeds_size_limit")
            warnings = ([f"body is {len(message.get('body', ''))} characters (soft limit 2000; Unicode code points, as counted by Python len): publish the detail with 'artifact --file PATH' and put the returned digest in this message's 'artifact' field"]
                        if len(message.get("body", "")) > 2000 else [])
            if dry_run:
                return {"dry_run": True, "published": False, "duplicate": False,
                        "message": {"id": message["id"], "seq": message["seq"],
                                    "ts": message["ts"], "nonce": message.get("nonce"),
                                    "echo": message.get("echo"), "provisional": True,
                                    "provisional_fields": ["seq", "id", "ts", "nonce", "echo"]},
                        "head": state.head, "warnings": warnings}
            head = self.publish(state.head, {f"messages/{message['id']}.json": data},
                                f"{message['id']} {act}: {message['summary']}", handle)
            result = {"message": message, "head": head, "duplicate": False,
                      "warnings": warnings}
        poll_warning = self._record_poll(handle, session, "send")
        if poll_warning:
            result["warnings"].append(poll_warning)
        return result

    def put_artifact(self, handle, session, data):
        require(len(data) <= MAX_ARTIFACT, "artifact exceeds 1 MiB", code="artifact_exceeds_1_mib")
        try:
            text_field(data.decode("utf-8"), "artifact", MAX_ARTIFACT, True)
        except UnicodeError as exc:
            raise ProtocolError("artifact must be UTF-8 text", code="artifact_must_be_utf_8_text") from exc
        digest = sha(data)
        with self.lock():
            state = self.snapshot()
            state.identity(handle, session)
            if digest in state.artifacts:
                return {"artifact": digest, "head": state.head, "duplicate": True}
            head = self.publish(state.head, {f"artifacts/{digest}": data},
                                f"artifact {digest}", handle)
            return {"artifact": digest, "head": head, "duplicate": False}

    def receipt(self, state, handle, session):
        current = state.identity(handle, session)
        path = self.runtime / (handle + ".json")
        require(not path.is_symlink(), "receipt symlink refused", code="receipt_symlink_refused")
        if path.exists():
            record = parse(read_input(path, 4096), 4096)
            require(set(record) == {"v", "generation", "read", "seen"} and record["v"] == 2,
                    "corrupt receipt", code="corrupt_receipt")
            for field in ("generation", "read", "seen"):
                integer(record[field], "receipt field")
            require(1 <= record["generation"] <= current["generation"] and
                    0 <= record["read"] <= record["seen"] <= len(state.messages), "corrupt receipt bounds", code="corrupt_receipt_bounds")
            if record["generation"] != current["generation"]:
                record = dict(record, generation=current["generation"], seen=record["read"])
        else:
            record = {"v": 2, "generation": current["generation"], "read": 0, "seen": 0}
        return path, record

    def inbox(self, handle, session, limit=10, _heartbeat=True):
        integer(limit, "limit", 1, 100)
        with self.lock():
            state = self.snapshot()
            path, receipt = self.receipt(state, handle, session)
            messages, through = self._page(state, handle, receipt["read"], limit)
            receipt["seen"] = max(receipt["seen"], through)
            atomic_json(path, receipt)
            result = {"head": state.head, "read_through": receipt["read"], "through": through,
                      "messages": messages, "more": through < len(state.messages)}
        if _heartbeat:
            warning = self._record_poll(handle, session, "inbox")
            if warning:
                result["warnings"] = [warning]
        return result

    @staticmethod
    def _page(state, handle, after, limit):
        messages, through, size = [], after, 0
        for message in state.messages[after:]:
            if handle in message["to"]:
                length = len(canonical(message))
                if len(messages) >= limit or size + length > 256 * 1024:
                    break
                messages.append(state.message_view(message))
                size += length
            through = message["seq"]
        return messages, through

    def notifications(self, handle, session, new_since, limit=10, _heartbeat=True):
        """Caller-owned alert cursor; never authorize or advance a receipt."""
        integer(new_since, "notification sequence")
        integer(limit, "limit", 1, 100)
        with self.lock():
            state = self.snapshot()
            _, receipt = self.receipt(state, handle, session)
            require(new_since <= len(state.messages), "notification cursor exceeds channel history", code="notification_cursor_exceeds_channel_history")
            after = max(receipt["read"], new_since)
            messages, through = self._page(state, handle, after, limit)
            messages = [state.message_view(message) for message in messages]
            result = {"head": state.head, "mode": "notifications", "read_through": receipt["read"],
                      "notification_through": through, "messages": messages,
                      "more": through < len(state.messages)}
        if _heartbeat:
            warning = self._record_poll(handle, session, "wait")
            if warning:
                result["warnings"] = [warning]
        return result

    def ack(self, handle, session, through):
        integer(through, "receipt sequence")
        with self.lock():
            state = self.snapshot()
            path, receipt = self.receipt(state, handle, session)
            require(receipt["read"] <= through <= receipt["seen"],
                    "ack must not regress or pass the scanned inbox", code="ack_out_of_range",
                    field="ack", hint="Choose through between read_through and scanned_through.",
                    details={"read_through": receipt["read"], "scanned_through": receipt["seen"]})
            receipt["read"] = through
            atomic_json(path, receipt)
            result = {"read_through": through, "head": state.head}
        warning = self._record_poll(handle, session, "ack")
        if warning:
            result["warnings"] = [warning]
        return result

    def wait(self, handle, session, timeout=30, limit=10, new_since=None):
        require(type(timeout) in (int, float) and 0 <= timeout <= 60, "timeout must be 0–60 seconds", code="timeout_must_be_0_60_seconds")
        deadline = time.monotonic() + timeout
        while True:
            result = (self.inbox(handle, session, limit, _heartbeat=False) if new_since is None else
                      self.notifications(handle, session, new_since, limit, _heartbeat=False))
            if result["messages"] or time.monotonic() >= deadline:
                result = dict(result, timed_out=not bool(result["messages"]))
                warning = self._record_poll(handle, session, "wait")
                if warning:
                    result["warnings"] = [warning]
                return result
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))


class Snapshot:
    def __init__(self, head, blobs, *, allow_legacy=False):
        self.head = head
        self.config = parse(blobs["channel.json"])
        cfg = self.config
        require(set(cfg) == {"kind", "v", "task", "reviewers", "policy", "protocol_sha256"}
                and cfg["kind"] == "ai-comm-channel" and type(cfg["v"]) is int and cfg["v"] == 2,
                "unsupported or malformed channel", code="unsupported_or_malformed_channel")
        text_field(cfg["task"], "task", 2000, True)
        pattern(cfg["protocol_sha256"], HEX, "protocol digest")
        local_protocol_digest = sha(Path(__file__).with_name("PROTOCOL.md").read_bytes())
        if not allow_legacy and cfg["protocol_sha256"] != local_protocol_digest:
            code, last_package = classify_protocol_mismatch(
                cfg["protocol_sha256"],
                cfg.get("policy", {}).get("protocol_version") if isinstance(cfg.get("policy"), dict) else None,
                local_protocol_digest)
            if code == "channel_contract_older_than_package":
                message = "channel uses an older contract; use its last compatible package"
                hint = f"The last indexed implementation is package v{last_package}; keep the pinned revision for this channel."
            elif code == "channel_contract_newer_than_package":
                message = "channel claims a newer contract; select a package that supports it"
                hint = "The declared protocol version is not evidence; verify the release and manifest before upgrading."
            else:
                message = "channel contract is unknown or inconsistent; no write was made"
                hint = "Use only a package whose known protocol digest matches the channel, or ask the owner to verify it."
            raise ProtocolError(message, code=code, field="channel", hint=hint)
        reviewers = cfg["reviewers"]
        require(isinstance(reviewers, list) and 2 <= len(reviewers) <= 16,
                "invalid reviewer configuration", code="invalid_reviewer_configuration")
        for handle in reviewers:
            pattern(handle, HANDLE, "reviewer")
        require(len(set(reviewers)) == len(reviewers), "reviewers must be distinct", code="reviewers_must_be_distinct")
        policy = cfg["policy"]
        base_policy = {"require_confidence", "require_sources"}
        expected_policy = (base_policy if allow_legacy else
                           base_policy | {"protocol_version"})
        require(isinstance(policy, dict) and
                (set(policy) == expected_policy or
                 (not allow_legacy and set(policy) == expected_policy | {"predecessor"})),
                "invalid policy", code="invalid_policy")
        if not allow_legacy:
            require(policy.get("protocol_version") == PROTOCOL_VERSION and
                    protocol_version_table(local_protocol_digest) is not None,
                    "channel protocol version or package table is inconsistent",
                    code="channel_contract_unknown", field="channel",
                    hint="Use a package with a valid one-to-one protocol version and digest table.")
        for field, allowed in (("require_confidence", CONF_ACTS), ("require_sources", ACTS)):
            require(isinstance(policy[field], list) and all(isinstance(a, str) and a in allowed
                    for a in policy[field]), "invalid policy acts", code="invalid_policy_acts")
        self.sessions, self.current, self.artifacts = {}, {}, {}
        for path, data in sorted(blobs.items()):
            if path.startswith("sessions/"):
                record = parse(data)
                session_fields = {"handle", "platform", "session", "generation", "ts"}
                required_session_fields = session_fields if allow_legacy else session_fields | {"tool_version"}
                require(set(record) == required_session_fields,
                        "invalid session record", code="invalid_session_record")
                handle = pattern(record["handle"], HANDLE, "handle")
                pattern(record["platform"], HANDLE, "platform")
                text_field(record["session"], "session reference", 160)
                generation = integer(record["generation"], "generation", 1)
                timestamp(record["ts"])
                if not allow_legacy:
                    semver(record["tool_version"], "tool_version")
                require(path == f"sessions/{handle}/{generation:08d}.json", "session path mismatch", code="session_path_mismatch")
                previous = self.current.get(handle)
                require(generation == (previous["generation"] + 1 if previous else 1), "session history gap", code="session_history_gap")
                require(not any(old["handle"] == handle and old["session"] == record["session"]
                                for old in self.sessions.values()), "reused historical session reference", code="reused_historical_session_reference")
                self.sessions[(handle, generation)] = record
                self.current[handle] = record
            elif path.startswith("artifacts/"):
                digest = path.split("/")[1]
                require(sha(data) == digest, "artifact hash mismatch", code="artifact_hash_mismatch")
                try:
                    text_field(data.decode("utf-8"), "stored artifact", MAX_ARTIFACT, True)
                except UnicodeError as exc:
                    raise ProtocolError("stored artifact must be UTF-8 text", code="stored_artifact_must_be_utf_8_text") from exc
                self.artifacts[digest] = data
        if not allow_legacy and "predecessor" in policy:
            validate_predecessor_record(policy["predecessor"], self.artifacts,
                                        cfg["protocol_sha256"], cfg["task"], reviewers)
        self.messages, self.by_id, self.retries, self.sources, self.questions = [], {}, {}, {}, {}
        for path, data in sorted(blobs.items()):
            if path.startswith("messages/"):
                message = parse(data)
                require(path == f"messages/{message.get('id')}.json", "message path mismatch", code="message_path_mismatch")
                require(canonical(message) == data, "noncanonical stored message", code="noncanonical_stored_message")
                self.add_message(message)
        try:
            self._local_identity = package_identity(Path(__file__).resolve().parent)
        except Exception:
            self._local_identity = {"version": None, "commit": None, "source": "unknown",
                                    "manifest_sha256": None, "protocol_sha256": None,
                                    "reason": "local_identity_unavailable"}
        try:
            status = release_status(Path(__file__).resolve().parent)
            self._release_index_status = status
            self._verified_releases = {entry["package_version"]: entry
                                       for entry in status.get("verified_releases", [])}
        except Exception:
            self._release_index_status = {"status": "index_invalid",
                                          "message": "local release status unavailable"}
            self._verified_releases = {}

    def peer_advisory(self, record):
        """A total, offline annotation; never infer safety from unavailable release data."""
        status = self._release_index_status.get("status")
        if status != "ok":
            return {"advisory": "unknown", "reason": str(status or "release index unavailable")}
        try:
            version = semver(record.get("tool_version"), "tool_version")
            matches = []
            for entry in self._verified_releases.values():
                if entry.get("advisory") not in {"recommended", "required"} or "affects" not in entry:
                    continue
                parsed, _, _ = parse_affects(entry["affects"])
                if range_matches(parsed, version):
                    matches.append(entry)
            if not matches:
                return {"advisory": "none",
                        "reason": "no applicable advisory in the locally verified release index"}
            selected = max(matches, key=lambda entry: (
                entry["advisory"] == "required", semver(entry["package_version"])))
            value = {"advisory": selected["advisory"],
                     "package_version": selected["package_version"],
                     "reason": selected["reason"][:240]}
            if "fixed_in" in selected:
                value["fixed_in"] = selected["fixed_in"]
            if len(matches) > 1:
                value["additional_applicable_advisories"] = len(matches) - 1
            return value
        except Exception:
            return {"advisory": "unknown", "reason": "peer advisory evaluation failed safely"}

    def identity(self, handle, session):
        pattern(handle, HANDLE, "handle")
        text_field(session, "session reference", 160)
        record = self.current.get(handle)
        require(record is not None and record["session"] == session, "unregistered or stale session", code="unregistered_or_stale_session")
        return record

    def release_notice(self, message):
        if message.get("act") != "FACT" or message.get("topic") != "upgrade":
            return None
        if "artifact" not in message:
            return {"status": "legacy_note", "advisory": "legacy_note",
                    "reason": "historical upgrade note has no release record; no advisory inferred",
                    **_protocol_transition_fields(None, None)}
        try:
            return self._release_notice(message)
        except Exception:
            return {"status": "unverified", "advisory": "malformed",
                    "reason": "release notice is invalid; no advisory can be cleared",
                    **_protocol_transition_fields(None, None)}

    def _release_notice(self, message):
        data = self.artifacts.get(message.get("artifact"))
        if data is None:
            return {"status": "unverified", "advisory": "malformed",
                    "reason": "release record artifact is unavailable",
                    **_protocol_transition_fields(None, None)}
        try:
            record = parse(data, MAX_ARTIFACT)
            require(canonical(record) == data, "release record must be canonical JSON", code="release_record_must_be_canonical_json")
            validate_release_record(record)
        except (ProtocolError, UnicodeError, ValueError):
            return {"status": "unverified", "advisory": "malformed",
                    "reason": "release record is invalid; no advisory can be cleared",
                    **_protocol_transition_fields(None, None)}
        version = semver(record["package_version"])
        local_version = semver(self._local_identity["version"])
        known = self._verified_releases.get(record["package_version"])
        if version == local_version:
            status = ("same" if self._local_identity["manifest_sha256"] and
                      record["manifest_sha256"] == self._local_identity["manifest_sha256"] and
                      record["protocol_sha256"] == self._local_identity["protocol_sha256"]
                      else ("inconsistent" if self._local_identity["manifest_sha256"] else "unverified"))
        elif known and (record["manifest_sha256"] != known["manifest_sha256"] or
                        record["protocol_sha256"] != known["protocol_sha256"]):
            status = "inconsistent"
        elif known and record["protocol_sha256"] != self._local_identity["protocol_sha256"]:
            status = "incompatible_release"
        elif known and version > local_version and record["protocol_sha256"] == self._local_identity["protocol_sha256"]:
            status = "update_available"
        elif version < local_version:
            status = "older_release_announced"
        else:
            status = "unverified"
        advisory = record["advisory"]
        applies = False
        if advisory in {"required", "recommended"}:
            if "affects" not in record:
                advisory = "malformed"
            else:
                try:
                    parsed, _, _ = parse_affects(record["affects"])
                    applies = range_matches(parsed, local_version)
                except ProtocolError:
                    advisory = "malformed"
        if status == "inconsistent":
            advisory = "suppressed_inconsistent"
            applies = False
        if status == "inconsistent":
            advisory = "suppressed_inconsistent"
            applies = False
        trusted_release = (self._release_index_status.get("status") == "ok" and known and
                           record["commit"] == known.get("commit") and
                           record["manifest_sha256"] == known.get("manifest_sha256") and
                           record["protocol_sha256"] == known.get("protocol_sha256"))
        transition = _protocol_transition_fields(
            known.get("protocol_sha256") if trusted_release else None,
            self._local_identity.get("protocol_sha256"))
        return {"status": status, "package_version": record["package_version"],
                "advisory": advisory, "advisory_applies": applies,
                "summary": message["summary"], "reason": record["reason"],
                "different_pin": version != local_version and status in
                {"update_available", "older_release_announced"},
                **transition}

    def message_view(self, message):
        notice = self.release_notice(message)
        return dict(message, release_status=notice) if notice else message

    def validate_draft(self, draft, author, historical=False):
        require(isinstance(draft, dict) and set(draft) <= DRAFT_KEYS, "unknown draft field", code="unknown_draft_field")
        require({"act", "to", "topic", "summary"} <= set(draft), "missing draft field", code="missing_draft_field")
        act = draft["act"]
        require(isinstance(act, str) and act in ACTS, "unknown act", code="unknown_act",
                field="act", allowed=sorted(ACTS), hint="Choose one of the documented communication acts.")
        recipients = draft["to"]
        require(isinstance(recipients, list) and 1 <= len(recipients) <= 32, "invalid recipients", code="invalid_recipients")
        for handle in recipients:
            pattern(handle, HANDLE, "recipient")
        unregistered = sum(1 for handle in recipients if handle not in self.current)
        require(unregistered == 0, "to contains unregistered handles", code="unregistered_recipients",
                field="to", got=unregistered,
                hint="Register the intended recipients first; the count is shown without exposing their names.")
        require(len(set(recipients)) == len(recipients), "duplicate recipient", code="duplicate_recipient")
        pattern(draft["topic"], IDENT, "topic")
        text_field(draft["summary"], "summary", 300)
        if "body" in draft:
            text_field(draft["body"], "body", MAX_DRAFT, True)
        if "confidence" in draft:
            require(act in CONF_ACTS, "confidence is not allowed for this act",
                    code="confidence_not_allowed", field="confidence",
                    allowed=sorted(CONF_ACTS), got=act,
                    hint="Remove confidence; it is allowed only on FACT, CORRECT, and DECIDE.")
            require(isinstance(draft["confidence"], str) and draft["confidence"] in CONFIDENCE,
                    "invalid confidence", code="invalid_confidence", field="confidence",
                    allowed=sorted(CONFIDENCE), hint="Choose verified, likely, or unverified.")
        if act in self.config["policy"]["require_confidence"]:
            require("confidence" in draft, "confidence required by channel policy", code="confidence_required_by_channel_policy")
        target = None
        if "reply_to" in draft:
            pattern(draft["reply_to"], MID, "reply target")
            target = self.by_id.get(draft["reply_to"])
            require(target is not None, "reply target must be an earlier committed message", code="reply_target_must_be_an_earlier_committed_message")
        if act in {"CORRECT", "PONG", "ACK", "ACCEPT", "REJECT", "DECIDE"}:
            require(target is not None, "this act requires a reply target", code="this_act_requires_a_reply_target")
        if act == "PONG" or (act == "ACK" and target["act"] in {"PING", "PONG"}):
            require(target["act"] in ({"PING"} if act == "PONG" else {"PING", "PONG"})
                    and author in target["to"] and target["from"] in recipients
                    and author != target["from"], "invalid control reply relationship", code="invalid_control_reply_relationship")
        if "artifact" in draft:
            pattern(draft["artifact"], HEX, "artifact digest")
            require(draft["artifact"] in self.artifacts, "artifact must be published first", code="artifact_must_be_published_first")
        if act == "FACT" and draft["topic"] == "upgrade" and not historical:
            require("artifact" in draft, "upgrade notice requires a release record",
                    code="upgrade_requires_release_record", field="artifact",
                    hint="Publish the canonical release record artifact and reference its digest.")
            raw_record = self.artifacts[draft["artifact"]]
            try:
                release_record = parse(raw_record, MAX_ARTIFACT)
                require(canonical(release_record) == raw_record, "release record must be canonical JSON",
                        code="release_record_not_canonical", field="artifact",
                        hint="Serialize the release record using canonical JSON and a final newline.")
                validate_release_record(release_record)
                summary = draft["summary"].lower()
                require(("v" + release_record["package_version"]).lower() in summary and
                        release_record["commit"] in summary and release_record["advisory"] in summary,
                        "upgrade summary must identify version, commit, and advisory",
                        code="upgrade_summary_incomplete", field="summary",
                        hint="Include the v-prefixed package version, full commit id, and advisory level in summary.")
            except ProtocolError:
                raise
        if act in {"PROPOSE", "ACCEPT", "REJECT", "DECIDE"}:
            require("artifact" in draft, "this act requires an artifact", code="this_act_requires_an_artifact")
        if act in {"ACCEPT", "REJECT", "DECIDE"}:
            require(target["act"] == "PROPOSE" and target.get("artifact") == draft["artifact"],
                    "agreement must reference the exact proposal and artifact", code="agreement_must_reference_the_exact_proposal_and_artifact")
            require(draft["topic"] == target["topic"], "agreement topic differs from proposal", code="agreement_topic_differs_from_proposal")
            require(author in self.config["reviewers"], "only configured reviewers may record agreement", code="only_configured_reviewers_may_record_agreement")
            require(author == target["from"] or author in target["to"], "reviewer not addressed by proposal", code="reviewer_not_addressed_by_proposal")
            require(not any(m["act"] == "DECIDE" and m.get("reply_to") == target["id"]
                            for m in self.messages), "proposal already decided; propose a new revision", code="proposal_already_decided_propose_a_new_revision")
            if act == "DECIDE":
                stances = {}
                for message in self.messages:
                    if message["act"] in {"ACCEPT", "REJECT"} and message.get("reply_to") == target["id"]:
                        stances[message["from"]] = message["act"]
                require(all(stances.get(r) == "ACCEPT" for r in self.config["reviewers"]),
                        "all required reviewers must ACCEPT this exact proposal", code="all_required_reviewers_must_accept_this_exact_proposal")
        additions = draft.get("sources", [])
        require(isinstance(additions, list) and len(additions) <= 64, "invalid source declarations", code="invalid_source_declarations")
        sources = dict(self.sources)
        for source in additions:
            require(isinstance(source, dict) and set(source) == {"id", "kind", "ref", "detail"},
                    "invalid source declaration", code="invalid_source_declaration")
            sid = pattern(source["id"], IDENT, "source id")
            pattern(source["kind"], IDENT, "source kind")
            text_field(source["ref"], "source reference", 4000)
            text_field(source["detail"], "source detail", 8000, True)
            require(sid not in sources or sources[sid] == source, "source id is immutable", code="source_id_is_immutable")
            sources[sid] = source
        refs = draft.get("refs", [])
        require(isinstance(refs, list) and len(refs) <= 64, "invalid source references", code="invalid_source_references")
        for sid in refs:
            pattern(sid, IDENT, "source id")
            require(sid in sources, "unknown source reference", code="unknown_source_reference")
        if act in self.config["policy"]["require_sources"]:
            require(bool(refs or additions), "source evidence required by channel policy", code="source_evidence_required_by_channel_policy")
        deltas = draft.get("questions", [])
        require(isinstance(deltas, list) and len(deltas) <= 64, "invalid question deltas", code="invalid_question_deltas")
        questions = dict(self.questions)
        changed_questions = set()
        for delta in deltas:
            require(isinstance(delta, dict) and isinstance(delta.get("op"), str), "invalid question delta", code="invalid_question_delta")
            qid = pattern(delta.get("id"), IDENT, "question id")
            require(qid not in changed_questions, "one delta per question per message", code="one_delta_per_question_per_message")
            changed_questions.add(qid)
            if delta["op"] == "open":
                require(set(delta) == {"op", "id", "text"}, "invalid open question delta",
                        code="invalid_open_question_delta", field="questions",
                        hint="An open delta contains exactly op, id, and text.")
                require(qid not in questions, "question id already used", code="question_id_already_used",
                        field="questions", hint="Choose a question id never used in this channel.")
                text_field(delta["text"], "question", 2000, True)
                questions[qid] = {"id": qid, "text": delta["text"], "state": "open"}
            else:
                require(delta["op"] == "close" and set(delta) == {"op", "id"}, "unknown question operation", code="unknown_question_operation")
                require(qid in questions and questions[qid]["state"] == "open", "question is not open", code="question_is_not_open")
                questions[qid] = dict(questions[qid], state="closed")
        return sources, questions

    def add_message(self, message):
        required = {"v", "id", "seq", "ts", "from", "session", "generation", "key", "draft_sha256"}
        require(required <= set(message) <= (DRAFT_KEYS | META_KEYS), "invalid envelope fields", code="invalid_envelope_fields")
        require(type(message["v"]) is int and message["v"] == 2, "unsupported message version", code="unsupported_message_version")
        seq = integer(message["seq"], "sequence", 1)
        require(seq == len(self.messages) + 1, "message sequence gap or collision", code="message_sequence_gap_or_collision")
        author = pattern(message["from"], HANDLE, "sender")
        require(message["id"] == f"{seq:08d}-{author}", "message id mismatch", code="message_id_mismatch")
        generation = integer(message["generation"], "generation", 1)
        session = self.sessions.get((author, generation))
        require(session is not None and session["session"] == message["session"], "invalid historical session", code="invalid_historical_session")
        timestamp(message["ts"])
        pattern(message["key"], KEY, "retry key")
        pattern(message["draft_sha256"], HEX, "draft digest")
        draft = {k: v for k, v in message.items() if k in DRAFT_KEYS}
        require(len(canonical(draft)) <= MAX_DRAFT, "stored draft exceeds limit", code="stored_draft_exceeds_limit")
        require(sha(canonical(draft)) == message["draft_sha256"], "draft digest mismatch", code="draft_digest_mismatch")
        sources, questions = self.validate_draft(draft, author, historical=True)
        predecessor = self.config["policy"].get("predecessor")
        if seq == 1 and predecessor is not None:
            declaration = next((source for source in draft.get("sources", [])
                                if source.get("id") == "predecessor-ledger"), None)
            require(declaration is not None and declaration["kind"] == "ledger" and
                    declaration["ref"] == predecessor["channel_head"],
                    "first message must cite the predecessor ledger",
                    code="predecessor_source_required", field="sources",
                    hint="Declare predecessor-ledger with kind ledger and ref equal to the linked channel head.")
        act = message["act"]
        if act in {"PING", "PONG"}:
            pattern(message.get("nonce"), NONCE, "control nonce")
        else:
            require("nonce" not in message, "unexpected control nonce", code="unexpected_control_nonce")
        target = self.by_id.get(message.get("reply_to"))
        if act == "PONG" or (act == "ACK" and target["act"] in {"PING", "PONG"}):
            require(message.get("echo") == target["nonce"], "control nonce does not match", code="control_nonce_does_not_match")
            if act == "PONG":
                require(message["nonce"] != target["nonce"], "PONG nonce must be fresh", code="pong_nonce_must_be_fresh")
        else:
            require("echo" not in message, "unexpected control echo", code="unexpected_control_echo")
        retry = (author, generation, message["key"])
        require(retry not in self.retries, "duplicate retry key in history", code="duplicate_retry_key_in_history")
        for qid, value in questions.items():
            old = self.questions.get(qid)
            if old is None:
                value = dict(value, opened_by=message["id"])
            elif old["state"] != value["state"]:
                value = dict(value, closed_by=message["id"])
            questions[qid] = value
        self.sources, self.questions = sources, questions
        self.messages.append(message)
        self.by_id[message["id"]] = message
        self.retries[retry] = message

    def state(self, budget=8192, actor=None, session=None):
        local = {key: self._local_identity.get(key) for key in
                 ("version", "commit", "protocol_sha256", "source")}
        if self._local_identity.get("source") == "git":
            local["dirty"] = bool(self._local_identity.get("dirty"))
        if self._local_identity.get("source") == "unknown":
            local["reason"] = self._local_identity.get("reason", "local_identity_unavailable")
        result = {"v": 2, "head": self.head, "task": self.config["task"],
                  "local_package": local,
                  "participants": [dict(record, tool_version_basis="join-time",
                                        advisory=self.peer_advisory(record))
                                  for record in self.current.values()],
                  "open_questions": [{"id": q["id"], "opened_by": q["opened_by"]}
                                     for q in self.questions.values() if q["state"] == "open"],
                  "decisions": [{"id": m["id"], "artifact": m["artifact"], "proposal": m["reply_to"]}
                                for m in self.messages if m["act"] == "DECIDE"],
                  "recent": [{"id": m["id"], "act": m["act"], "summary": m["summary"]}
                             for m in self.messages[-10:]],
                  "release_notices": [notice for m in self.messages
                                      if (notice := self.release_notice(m)) is not None],
                  "omitted": {"participants": 0, "open_questions": 0, "decisions": 0,
                              "recent": 0, "release_notices": 0},
                  "inspect": "registry sessions|questions|sources; log --after N; show ID"}
        if actor is not None or session is not None:
            require(actor is not None and session is not None,
                    "state identity requires both actor and session", code="state_identity_incomplete",
                    field="session", hint="Pass both --as and --session, or neither.")
            own = self.identity(actor, session)
            running = local.get("version")
            if isinstance(running, str) and own["tool_version"] != running:
                result["version_note"] = {"joined_with": own["tool_version"], "running": running}
        while len(canonical(result)) > budget:
            notices = result["release_notices"]
            disposable = [i for i, notice in enumerate(notices)
                          if not (notice.get("advisory") in {"required", "recommended"} and
                                  notice.get("advisory_applies") is True)]
            if disposable:
                notices.pop(disposable[0])
                result["omitted"]["release_notices"] += 1
                continue
            # Preserve every applicable required/recommended notice. Compact detail first.
            shortened = False
            for notice in notices:
                for field, limit in (("reason", 80), ("summary", 80)):
                    value = notice.get(field)
                    if isinstance(value, str) and len(value) > limit:
                        notice[field] = value[:limit - 1] + "…"
                        shortened = True
            if shortened:
                continue
            candidates = [k for k in result["omitted"] if k != "release_notices" and result[k]]
            require(bool(candidates), "state budget too small", code="state_budget_too_small")
            largest = max(candidates, key=lambda k: len(canonical(result[k])))
            result[largest].pop(0 if largest in {"recent", "decisions", "release_notices"} else -1)
            result["omitted"][largest] += 1
        return result

    def render(self, since=0):
        integer(since, "since")
        lines = ["# Channel history", "", f"Committed snapshot: `{self.head}`", ""]
        words = {"PING": "Ping", "PONG": "Pong and ping acknowledgement", "ACK": "Acknowledgement",
                 "ASK": "Question", "FACT": "Fact or observation", "CORRECT": "Correction",
                 "PROPOSE": "Proposal", "REVIEW": "Review", "ACCEPT": "Acceptance",
                 "REJECT": "Rejection", "DECIDE": "Decision"}
        # HTML-escape all text so content cannot become executable markup or links.
        def safe(value):
            return html.escape(str(value), quote=True).replace("`", "&#96;")
        source_first_seen = {}
        for message in self.messages:
            if message["seq"] <= since:
                continue
            lines += [f"## {safe(message['id'])} — {words[message['act']]}", "",
                      f"{safe(message['from'])} → {safe(', '.join(message['to']))} · {safe(message['ts'])}", "",
                      "<pre>" + safe(message["summary"]) + "</pre>", ""]
            if "body" in message:
                lines += ["<pre>" + safe(message["body"]) + "</pre>", ""]
            for field in ("reply_to", "confidence", "artifact", "nonce", "echo"):
                if field in message:
                    lines += [f"{field}: `{safe(message[field])}`", ""]
            refs = list(dict.fromkeys(message.get("refs", []) + [s["id"] for s in message.get("sources", [])]))
            for sid in refs:
                if sid in source_first_seen:
                    citation = f"Source {sid}: see {source_first_seen[sid]} above."
                else:
                    source = self.sources[sid]
                    citation = f"Source {sid} ({source['kind']}): {source['ref']}\n{source['detail']}"
                    source_first_seen[sid] = message["id"]
                lines += ["<pre>" + safe(citation) + "</pre>", ""]
            for delta in message.get("questions", []):
                question = self.questions[delta["id"]]
                lines += ["<pre>" + safe(f"Question {delta['id']}: {delta['op']} — {question['text']}") + "</pre>", ""]
        return "\n".join(lines) + "\n"


def validate_predecessor(path, digest, task, reviewers, target_protocol_digest):
    raw_path = Path(path).absolute()
    cursor = Path(raw_path.anchor)
    for component in raw_path.parts[1:]:
        cursor = cursor / component
        require(not cursor.is_symlink(), "predecessor path contains a symlink",
                code="predecessor_symlink_refused", field="channel")
    require(raw_path.exists(), "predecessor path does not exist",
            code="predecessor_not_found", field="channel")
    require(raw_path.is_dir(), "predecessor must be an existing bare channel",
            code="predecessor_not_bare", field="channel")
    repo = raw_path.resolve()
    try:
        bare = git(repo, "rev-parse", "--is-bare-repository").strip()
    except ProtocolError as exc:
        raise ProtocolError("predecessor is not a valid bare channel",
                            code="predecessor_not_bare", field="channel") from exc
    require(bare == b"true",
            "predecessor must be a bare channel", code="predecessor_not_bare", field="channel")
    require(isinstance(digest, str) and HEX.fullmatch(digest),
            "invalid predecessor handoff digest",
            code="predecessor_handoff_digest_invalid", field="artifact")
    try:
        head, blobs = read_snapshot_blobs(repo)
    except ProtocolError as exc:
        raise ProtocolError("predecessor ledger is malformed",
                            code="predecessor_invalid_channel", field="channel") from exc
    try:
        old = Snapshot(head, blobs, allow_legacy=True)
    except ProtocolError as exc:
        raise predecessor_snapshot_error(exc) from exc
    handoff_bytes = old.artifacts.get(digest)
    require(handoff_bytes is not None, "handoff artifact is missing from predecessor",
            code="predecessor_handoff_missing", field="artifact")
    try:
        handoff = parse(handoff_bytes, MAX_ARTIFACT)
    except ProtocolError as exc:
        raise ProtocolError("predecessor handoff is invalid", code="predecessor_handoff_invalid",
                            field="artifact") from exc
    require(canonical(handoff) == handoff_bytes, "predecessor handoff is not canonical",
            code="predecessor_handoff_invalid", field="artifact")
    try:
        validate_handoff(handoff, target_protocol_digest=target_protocol_digest)
    except ProtocolError as exc:
        raise predecessor_handoff_error(exc) from exc
    require(semver(handoff["to_package_version"]) <= semver(VERSION),
            "package is older than handoff requirement", code="predecessor_package_too_old",
            field="channel")
    require(handoff["from_protocol_sha256"] == old.config["protocol_sha256"],
            "handoff source digest does not match predecessor",
            code="predecessor_source_protocol_mismatch", field="channel")
    require(handoff["task"] == task and set(handoff["reviewers"]) == set(old.config["reviewers"]) and
            set(reviewers) == set(old.config["reviewers"]),
            "task or reviewer set differs from handoff",
            code="predecessor_policy_mismatch", field="channel")
    decisions = [message for message in old.messages
                 if message["act"] == "DECIDE" and message.get("artifact") == digest]
    require(bool(decisions), "predecessor has no decision for the handoff",
            code="predecessor_decision_invalid", field="channel")
    decision = decisions[-1]
    require(old.messages[-1]["id"] == decision["id"],
            "handoff DECIDE must be the final ledger message",
            code="predecessor_decision_not_final", field="channel")
    try:
        parent = git(repo, "rev-parse", "--verify", f"{head}^", ok=(0, 128)).decode().strip()
    except ProtocolError as exc:
        raise ProtocolError("predecessor DECIDE commit cannot be verified",
                            code="predecessor_decision_commit_invalid", field="channel") from exc
    require(bool(parent), "predecessor decision commit has no parent",
            code="predecessor_decision_has_no_parent", field="channel")
    try:
        changes = git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", parent, head).splitlines()
    except ProtocolError as exc:
        raise ProtocolError("predecessor head cannot be verified",
                            code="predecessor_head_unreadable", field="channel") from exc
    require(changes == [f"A\tmessages/{decision['id']}.json".encode()],
            "predecessor head must be the commit that added DECIDE",
            code="predecessor_head_not_decision", field="channel")
    expected_handles = set(old.current)
    supplied_handles = {participant["handle"] for participant in handoff["participants"]}
    require(supplied_handles == expected_handles,
            "handoff participants differ from predecessor", code="predecessor_participants_mismatch",
            field="artifact")
    return {"record": {"channel_head": head,
                        "protocol_sha256": old.config["protocol_sha256"],
                        "handoff_artifact": digest,
                        "decide_message": decision["id"]},
            "handoff_bytes": handoff_bytes}


def emit(value):
    sys.stdout.buffer.write(canonical(value))
    sys.stdout.buffer.flush()


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ProtocolError("invalid command-line arguments", code="invalid_cli_arguments",
                            field="input", hint="Check the command name, option names, and value formats.")


def parser():
    result = SafeArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=VERSION)
    result.add_argument("--channel", help="explicit local channel directory")
    sub = result.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init")
    p.add_argument("--task", required=True)
    p.add_argument("--reviewers", nargs="+", required=True)
    p.add_argument("--require-confidence", nargs="*", default=[])
    p.add_argument("--require-sources", nargs="*", default=[])
    p.add_argument("--predecessor", help="read-only path to a frozen predecessor channel")
    p.add_argument("--handoff", help="SHA-256 of the accepted migration handoff artifact")
    p = sub.add_parser("join")
    p.add_argument("--as", dest="actor", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--session", required=True, help="non-secret conversation reference")
    p.add_argument("--takeover", action="store_true")
    command_help = {
        "artifact": "publish a file as an immutable artifact and print its SHA-256; "
                    "read it back with show-artifact DIGEST",
    }
    for command in ("send", "inbox", "ack", "wait", "artifact"):
        p = sub.add_parser(command, help=command_help.get(command),
                           description=command_help.get(command))
        p.add_argument("--as", dest="actor", required=True)
        p.add_argument("--session", required=True)
        if command == "send":
            p.add_argument("--key", required=True, help="reuse for retries of this identical draft")
            p.add_argument("--draft", required=True, help="JSON file, or - for stdin")
            p.add_argument("--dry-run", action="store_true",
                           help="validate against the current snapshot without publishing or reserving the key")
        elif command in {"inbox", "wait"}:
            p.add_argument("--limit", type=int, default=10)
            if command == "wait":
                p.add_argument("--timeout", type=float, default=30)
                p.add_argument("--new-since", type=int,
                               help="notification cursor only; does not scan or acknowledge the inbox")
        elif command == "ack":
            p.add_argument("--through", type=int, required=True)
        elif command == "artifact":
            p.add_argument("--file", required=True)
    p = sub.add_parser("state")
    p.add_argument("--as", dest="actor")
    p.add_argument("--session")
    p.add_argument("--stale-after", default="300")
    sub.add_parser("validate")
    sub.add_parser("release-status", help="inspect only already-fetched local release refs; never contacts the network")
    p = sub.add_parser("upgrade-plan", help="print a read-only plan from verified local release metadata")
    p.add_argument("--to", dest="target", help="verified newer package version; defaults to latest")
    p.add_argument("--channel", dest="channel", default=argparse.SUPPRESS,
                   help="optional current channel for a protocol migration plan")
    p.add_argument("--participant", action="append", default=[], metavar="HANDLE=STATUS",
                   help="explicit channel participant status (repeat for each handle)")
    p = sub.add_parser("upgrade", help="guided, owner-consented upgrade (runs upgrade_controller.py; see docs/RELEASES.md)",
                       description="Convenience wrapper: runs this package's upgrade_controller.py with the remaining "
                                   "arguments. For recovery always use the frozen controller path that `upgrade status` prints.")
    p.add_argument("controller_args", nargs=argparse.REMAINDER)
    p = sub.add_parser("show", help="print one committed message", description="print one committed message")
    p.add_argument("id")
    p = sub.add_parser("show-artifact", help="print a published artifact's exact bytes",
                       description="print a published artifact's exact bytes; read it before ACCEPT")
    p.add_argument("digest")
    p = sub.add_parser("log")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("registry")
    p.add_argument("kind", choices=["sessions", "questions", "sources"])
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("render")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--out")
    return result


def run_upgrade_controller(controller_args):
    """Exec the package's standalone controller; it never imports this module."""
    controller = Path(__file__).resolve().parent / "upgrade_controller.py"
    require(controller.is_file() and not controller.is_symlink(), "upgrade controller is missing",
            code="upgrade_controller_missing", field="input",
            hint="Use the frozen controller path printed by `upgrade status`.")
    os.execv(sys.executable, [sys.executable, "-I", str(controller), *controller_args])


def main(argv=None):
    try:
        raw = list(sys.argv[1:] if argv is None else argv)
        if raw[:1] == ["upgrade"] and not {"-h", "--help"} & set(raw[1:2]):
            run_upgrade_controller(raw[1:])
        args = parser().parse_args(argv)
        if args.command not in {"release-status", "upgrade-plan"}:
            require(args.channel is not None, "--channel is required for this command",
                    code="channel_required", field="channel",
                    hint="Pass --channel PATH before the command.")
        if args.command == "init":
            emit(Channel.init(args.channel, args.task, args.reviewers,
                              args.require_confidence, args.require_sources,
                              args.predecessor, args.handoff))
            return 0
        if args.command == "release-status":
            emit(release_status())
            return 0
        if args.command == "upgrade-plan":
            emit(upgrade_plan(args.target, args.channel, args.participant))
            return 0
        channel = Channel(args.channel)
        cmd = args.command
        if cmd == "join":
            value = channel.join(args.actor, args.platform, args.session, args.takeover)
        elif cmd == "send":
            value = channel.send(args.actor, args.session, args.key,
                                 parse(read_input(args.draft), MAX_DRAFT), args.dry_run)
            if not args.dry_run:
                value["message"] = {k: v for k, v in value["message"].items()
                                    if k in {"id", "seq", "act", "from", "to", "nonce", "echo"}}
        elif cmd == "inbox":
            value = channel.inbox(args.actor, args.session, args.limit)
        elif cmd == "wait":
            value = channel.wait(args.actor, args.session, args.timeout, args.limit, args.new_since)
        elif cmd == "ack":
            value = channel.ack(args.actor, args.session, args.through)
        elif cmd == "artifact":
            value = channel.put_artifact(args.actor, args.session, read_input(args.file, MAX_ARTIFACT))
        else:
            state = channel.snapshot()
            if cmd == "state":
                require(isinstance(args.stale_after, str) and len(args.stale_after) <= 5 and
                        re.fullmatch(r"[1-9][0-9]*", args.stale_after) is not None,
                        "invalid stale-after threshold", code="invalid_stale_after",
                        field="stale_after",
                        hint="Use ASCII decimal digits for an integer from 1 through 86400.")
                stale_after = integer(int(args.stale_after), "stale-after", 1, 86400)
                heartbeat_warning = None
                if args.actor is not None or args.session is not None:
                    require(args.actor is not None and args.session is not None,
                            "state identity requires both actor and session",
                            code="state_identity_incomplete", field="session",
                            hint="Pass both --as and --session, or neither.")
                    heartbeat_warning = channel._record_poll(args.actor, args.session, "state")
                value = state.state(actor=args.actor, session=args.session)
                as_of = datetime.now(timezone.utc)
                value["liveness"] = {record["handle"]: channel._poll_status(record, stale_after, as_of)
                                     for record in state.current.values()}
                if heartbeat_warning:
                    value["warnings"] = [heartbeat_warning]
            elif cmd == "validate":
                value = {"valid": True, "head": state.head, "messages": len(state.messages),
                         "sessions": len(state.sessions), "artifacts": len(state.artifacts)}
            elif cmd == "show":
                pattern(args.id, MID, "message id")
                require(args.id in state.by_id, "message not found", code="message_not_found")
                value = state.by_id[args.id]
            elif cmd == "show-artifact":
                pattern(args.digest, HEX, "artifact digest")
                require(args.digest in state.artifacts, "artifact not found", code="artifact_not_found")
                sys.stdout.buffer.write(state.artifacts[args.digest])
                return 0
            elif cmd == "log":
                integer(args.after, "after")
                integer(args.limit, "limit", 1, 100)
                page = state.messages[args.after:args.after + args.limit]
                value = {"head": state.head, "messages": [{k: m[k] for k in
                         ("id", "seq", "from", "to", "act", "summary")} for m in page],
                         "next": page[-1]["seq"] if page else args.after,
                         "more": args.after + len(page) < len(state.messages)}
            elif cmd == "registry":
                integer(args.offset, "offset")
                integer(args.limit, "limit", 1, 100)
                mapping = getattr(state, args.kind)
                values = list(mapping.values())
                page = values[args.offset:args.offset + args.limit]
                value = {"head": state.head, "items": page, "next": args.offset + len(page),
                         "more": args.offset + len(page) < len(values)}
            elif cmd == "render":
                rendered = state.render(args.since)
                if args.out:
                    with open(args.out, "x", encoding="utf-8") as output:
                        output.write(rendered)
                    value = {"rendered": True, "head": state.head}
                else:
                    sys.stdout.write(rendered)
                    return 0
        emit(value)
        return 0
    except ProtocolError as exc:
        print(json.dumps(exc.details, ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError,
            TypeError, KeyError, RecursionError):
        print('{"error":"operation failed; no success is implied","code":"operation_failed",'
              '"field":"input","hint":"Check the documented inputs and local permissions; '
              'no message was published."}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
