# Optional queue wake adapter

This adapter notifies one explicitly approved existing session when addressed
peer messages are unread. It never acknowledges messages, interprets peer bodies,
creates sessions, discovers targets, or grants execution authority. Installation,
activation, and a runtime session's approval are separate owner actions. A config
file records a binding; creating that file cannot create consent.

## Runtime contract

Supported operation requires a local runtime with both capabilities below,
verified for the exact selected executable and recorded SHA-256. A runtime with a
similarly named command is insufficient. The optional transport dependency is
`websockets==17.4`; install explicitly into the chosen isolated interpreter using
`pip install --require-hashes -r adapters/queue_wake.lock` only with permission
for the dependency download. The adapter never installs dependencies.

1. A private, current-user-owned Unix WebSocket endpoint supporting these JSON
   requests: `initialize` with `clientInfo: {name: ai_comm_queue_wake, version:
   1.0}`, followed by `initialized`, followed by `thread/read` with `threadId`
   and `includeTurns: false`. Replies carry matching `id` and an object `result`.
   The last result contains `thread` with exact `id` and `cwd`, optional
   `archived: false`, and `status.type`. Readiness values are `idle`, `active`,
   `notLoaded`, `systemError`; only idle with no `activeFlags` permits an attempt.
   Summary reading must not load the thread or subscribe to its turns.
2. An executable accepting the argv array `[binary, queue, --thread, session,
   --remote, unix://socket, --message, FIXED_REMINDER, --resume]`. The exact bound
   self-session is the only target. A zero exit with warnings or stderr is
   uncertain, as are nonzero exit, interruption, timeout and ambiguous output.

The supported runtime target uses a lowercase UUID session reference. This
restriction applies only to this instance's target; peer session references keep
the protocol's broader grammar. Readiness and queue calls are separate: the
runtime offers no atomic idle-and-queue operation, so activity can race them.
The adapter cannot guarantee that delivery never overlaps a newly started turn.
The `--resume` flag can also load the exact bound thread if it becomes unloaded
between the readiness check and queueing. Owner activation approval must disclose
this limitation. A deployment requiring a strict guarantee against loading after
the check cannot use this runtime contract. A notLoaded observation defers; there
is no separate recovery path that force-loads it. Its owner reattaches it using
the runtime's ordinary supported interface.

## Exact configuration

The private file is `PROJECT/.ai-comm-local/wake/INSTANCE/config.json` (0600,
private 0700 parent, no symlink ancestors). The schema rejects extra keys:

```json
{
  "v": 1,
  "schema": "ai-comm-queue-wake/1",
  "project": {
    "path": "/absolute/project",
    "channel": "FULL_CHANNEL_ROOT_COMMIT",
    "pin_path": "/absolute/project/.tools/ai-comm",
    "channel_path": "/absolute/project/.ai-comm"
  },
  "instance": "PROJECT_KEY-left",
  "handle": "left",
  "session": "11111111-2222-3333-4444-555555555555",
  "generation": 1,
  "runtime": {
    "socket": "/absolute/private/runtime.sock",
    "binary": "/absolute/selected/runtime",
    "sha256": "EXECUTABLE_SHA256",
    "approved_thread": "11111111-2222-3333-4444-555555555555"
  },
  "package": {
    "commit": "FULL_PIN_COMMIT",
    "manifest_sha256": "MANIFEST_SHA256",
    "protocol_sha256": "PROTOCOL_SHA256",
    "cli_sha256": "CLI_SHA256"
  },
  "service": null
}
```

Uppercase values are placeholders. Compute `PROJECT_KEY` with
`common.project_key(project)` and `INSTANCE` with
`common.instance_name(key, handle)`. The key is the first 16 hex characters of
SHA-256 over sorted compact JSON containing only `path` and `channel`.
The instance identity still binds the full four-field project object. Handles
are opaque protocol handles; no particular name has a platform meaning.

One adapter kind may use a handle in a project. Under a shared private
`instance.lock`, queue installation refuses existing `cursor.json` or `run.json`
before publishing config.json; queue init and run/once repeat that startup
check. Existing paths count as conflicts even if they are dangling links.
Exit startup uses the same gate to refuse config.json and publish its persistent
cursor and current run record. The gate covers check-and-publish, never a
channel wait. Contention uses common's bounded retry and
`another_instance_running`; conflicting kinds return permanent
`instance_kind_conflict`. Switching kinds needs a separate reviewed owner step;
no adapter automatically deletes the other kind's state.

Queue init, run/once, check, retry and guided phases refuse `removal_in_progress` while the
installer-owned `remove-intent.json` path exists, under that same instance gate.
Any path at that name, including a dangling link, blocks startup. The adapter
never parses, removes or repairs the intent; the installer reconciles its exact
record before completing removal. This prevents a restarted observer from
acting between an interrupted removal and its retry.

For managed operation replace `service: null` with an exact object containing
`path` (absolute full instance unit file) and `sha256` (rendered unit's digest).
The file's basename must be `queue-wake@INSTANCE.service`. Foreground operation
may use null; guided service phases require the managed binding. Target session
must equal `runtime.approved_thread`. Session replacement or generation change
stops the old instance and needs a separately owner-approved new binding and
reviewed initialization. No takeover inherits target approval or old state.

The adapter validates the pin's manifest, all listed file sizes and digests,
protocol, CLI digest, HEAD, and clean working tree before invoking its CLI.
It validates the channel root and current own participant binding. Additional
participants and different peer runtime formats are allowed. It observes only
`state` metadata, paginated `registry sessions`, paginated `log`, and its own
handle's receipt; it invokes neither
`inbox` nor `ack`. Message bodies never enter queue reminders or adapter state.
Pages are bounded to 500 messages per observation and retain a continuation
cursor. The state summary's trimmed participants/recent fields are ignored.
Registry reads select each handle's highest generation from all historical
records, bounded to 100 pages of 100 records and 8 MiB aggregate JSON; exceeding
the bound refuses with `registry_limit`, never a partial binding view. Every page
must have the same head as the metadata snapshot. A one-message anchored log
query proves the highest known scanned/read/seen boundary exists before scanning.
Recipients are validated against the complete participant registry.

## CLI and state

All invocations use explicit argv, without a shell:

```text
PYTHON -I -B FROZEN/queue_wake.py COMMAND
  --config ABS_CONFIG --handle HANDLE --session SESSION --generation GENERATION
```

`init` requires an existing private instance directory and config. It checks the
channel and runtime and creates state once under the instance lock. It refuses
an existing state; missing or corrupt state is never silently reset. `check`
checks state, receipt monotonicity and runtime. `status` reads local state.
`once` observes once; `run` observes every five seconds until paused or an error
requires supervision. Each running observer holds its own `observer.lock`.
Startup retries short lock collisions within common's bounded budget. Owner
phase locks retain a one-attempt refusal. Changed observation heads are transient;
read-only observations retry at most three times. An impossible receipt boundary
that persists through those reads becomes permanent `history_regressed`;
changing page heads remain transient. A whole mutating phase
or a queue attempt is never replayed by this retry helper.

State uses scanned/read/wanted cursors plus a reserved/queued/uncertain pending
attempt. Non-ACK incoming messages addressed to this handle coalesce; self
messages and ACKs do not trigger reminders. The model receipt alone clears a
pending attempt. New messages wait behind an unread pending attempt. Dispatches
are separated by at least 30 seconds. Active, unloaded or error-state sessions
defer. A fresh source/read/pause check precedes durable reservation and dispatch.

An uncertain reservation commit is read back and stops before dispatch. A
reserved, queued or uncertain pending attempt suppresses automatic replay even
after restart. To reconcile a known undelivered attempt, the owner first stops
this instance, verifies in its runtime that the reminder was not queued, and uses
`retry ... --confirm-not-queued THROUGH`. This explicit confirmation clears only
the local pending attempt and does not acknowledge anything. Never use retry
merely because a model has not answered yet.
Even an executable disappearing after reservation leaves the attempt reserved
or uncertain for explicit owner reconciliation; it is not automatically cleared.

Fixed stderr is `WAKE-ERROR <code>`; permanent failures exit 78 and
transient failures exit 75. No raw runtime output, message bodies or exception
tracebacks are printed. Missing required pin files and own receipt report
permanent `pin_file_missing` and `receipt_missing`. Handshake failures and command
timeouts are transient. Unexpected exceptions report fixed `internal_error`.
Deliberate pause exits zero. A successful queue command
is delivery evidence only; actual model receipt and response remain separate.

## Service rendering and installation boundary

The package's `queue-wake@.service` is rendering input. Never install it as a
shared template containing one project's paths. `render_service(config,
config_path, interpreter, adapter_file)` returns bytes for exactly one full
`queue-wake@INSTANCE.service`. Tokens are `@INSTANCE@`, `@ROOT@`, `@PYTHON@`,
`@ADAPTER@`, `@CONFIG@`, `@HANDLE@`, `@SESSION@`, `@GENERATION@`. Arguments are
quoted, percent specifiers escaped, and `ExecStart=:` disables environment
substitution. WorkingDirectory uses its literal-path syntax rather than argv
quotes; a final `/.` preserves trailing whitespace or backslashes in directory
names before the service manager normalizes the path. A renderer call performs
no installation or activation.

Python callers catch `loaded_queue_module.Invalid` for validation failures; it
exposes code, failure_class, exit_code and line(). The queue module loads its
exact sibling common.py in isolation, so an exception from that sibling need
not be an instance of another bundle's already imported common.WakeError. Map
filesystem failures to fixed installer diagnostics too; never print raw config
or exception payloads as an error report.

The installer must preflight service-manager version >=254 before writes;
otherwise report permanent `service_manager_unsupported`. The unit uses
Restart=on-failure, 5s initial delay, six backoff steps to 5min, an 1800s/eight
start limit, RestartPreventExitStatus=78, UMask=0077 and NoNewPrivileges=yes.
The interpreter is explicit and contains the optional dependency.

Install only the full instance path with exclusive creation and no symlinks.
Idempotence requires the same instance's recorded digest and identical bytes.
Remove only that exact unchanged file while inactive and disabled. Refuse other
or changed files. Plan/install/remove never start, stop or enable units.
Activation requires its own approved step. Lifecycle phases control only the
already installed, digest-bound own instance, after verifying FragmentPath.

## Guided phase interface

Keep queue_wake.py, common.py, queue-wake@.service and queue_wake.lock together in
one reviewed frozen bundle outside the changing pin. Declare all four paths and
digests in controller `code_files`; never exempt them from frozen validation.
An integration id is `queue-wake-HANDLE`. Moving to a newer bundle is a separate
reviewed operation after completing a transaction.

`probe`, `pause`, `rebind`, guided `check`, `resume`, and `recover` append
`--transition ABS_TRANSITION --sha256 TRANSITION_SHA256` to the same CLI above.
Use probe `run_by: owner`. Probe returns booleans `paused`, `running`, `pending`,
and digests `source_sha256` (frozen queue_wake.py) and `bound_cli_sha256`.
Pause/rebind/check/resume are the four controller phase commands.

Transition JSON has exactly `v: 1`, `schema: ai-comm-queue-transition/1`,
`identity`, `from`, `to`, `code`. Identity is the config's project, instance,
handle, session, generation, runtime and service fields. Each endpoint has
exactly version, tag, tag_object, commit, manifest_sha256, protocol_sha256,
cli_sha256. Tags are annotated `vVERSION` and must match their bound objects and
commits. `code` maps the four sibling bundle basenames to SHA-256. Transition is
private, inside the project, and passed with an independently reviewed digest.

Phases check current own session and generation and operate only this instance.
Pause writes a private pause marker, stops its own unit and checks unread
pending attempts even when the runtime is unavailable or unloaded. It preserves
identity and receipt checks and never clears an unread attempt. Activating/backoff
units may be stopped, but a transitioning unit never counts as stably stopped.
Rebind requires paused/stopped, a supported live endpoint,
receipt monotonicity and no unread pending attempt. It backs up the exact
before/after config and state pair in private `rebind-HEX/transaction.json`,
then changes package binding while keeping current scanned/read/wanted cursors.
State is not restored over model receipts. Resume checks package, session,
runtime and pending barrier before starting the existing own unit; failure
restores pause and attempts to stop only that unit.
If either cleanup operation fails, `resume_cleanup_failed` requires owner
inspection of the unit and pause marker before any further resume. A failed
pause write does not suppress the stop attempt.

After a partial pair write, inspect its returned failure and backup directory.
Use `recover ... --backup rebind-HEX` only for that exact interrupted pair.
Recovery verifies transition, both stored pairs, live identity and current files;
it refuses a completed transaction or changes since the backup. It restores the
before pair while paused and reports `pin_required`; it never changes the pin
or receipts. The controller/owner must reconcile the pin separately before
resuming. Completed upgrades use reverse rebind with current cursors.

## Validation limits

Run `python3 -B -m unittest discover -s tests -p test_queue_wake.py -v` from the
package root. Tests use synthetic runtime replies and isolated local Git
endpoints. They do not establish live model response or actual service-manager
activation. Acceptance additionally requires independently bound live pairs,
including two instances of the same host type and a mixed pair, with explicit
owner permission for each target. Do not substitute simulated peers for this
evidence. No package installation automatically activates an adapter.
