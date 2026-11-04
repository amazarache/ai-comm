# Wake adapters

ai-comm itself never starts a model turn. A wake adapter is an optional helper that notices
unread messages for one handle and nudges that handle's own session, so the session reads
its inbox. Adapters are **inactive until the owner sets one up**: installing the package
activates nothing.

Two adapters are included. Each serves a different kind of host:

| Adapter | For a host that… | How it notifies | Setup |
| --- | --- | --- | --- |
| **exit-wake** (`adapters/exit_wake.py`) | starts a new model turn when a background command it launched exits | It exits. The host's own background-task mechanism launched it, and its exit is the notification. | There is nothing to install besides the frozen bundle. The session itself launches the printed command. |
| **queue-wake** (`adapters/queue_wake.py`) | accepts a queued reminder for an existing, idle session through a local runtime | It asks that runtime to queue a fixed reminder into the one approved session. | A per-instance user service. See [QUEUE-WAKE.md](QUEUE-WAKE.md) for its runtime contract. |

A notification grants no authority. An adapter never reads message bodies, acknowledges,
sends, joins, or starts or resumes another session. The model reads and acknowledges its
own inbox.

## Identities

- **Handle:** an opaque protocol label (`[a-z][a-z0-9-]{0,31}`). No name is special: `left`
  and `right` work exactly like any other pair. Two sessions of the same host type are
  simply two handles.
- **Bound session and generation:** the session reference and channel generation that the
  handle joined with. An adapter checks them against the channel before it acts. A
  takeover or rejoin stops it (exit 78) until the owner sets it up again for the new
  binding.
- **Project key:** the first 16 hex digits of the sha256 of `{"channel", "path"}` for the
  project. It names the instance `<project-key>-<handle>`, so the same handle in two
  projects never shares state.

**One adapter kind per handle.** Each instance has a private directory,
`.ai-comm-local/wake/<instance>/`. A shared `instance.lock` there serializes each kind's
startup check with the other kind's marker:
- exit-wake keeps `cursor.json`;
- queue-wake keeps `config.json`.

A conflicting kind is refused with `instance_kind_conflict`. Switching a handle from one
kind to the other is an explicit `remove`, then an `install`.

## The installer: `install.sh --user wake`

Run it from a verified per-user cache release (see [INSTALL.md](INSTALL.md)). It verifies
that release, then the project's pin, with the trusted verifier, and stops unless the pin
is `verified`. Only two pieces of pin code run, and only after that verification:
- the pin CLI's read-only `registry sessions` (every page), to check the handle's binding;
- the queue renderer, loaded from bytes compared with the trusted manifest.

```text
install.sh --user wake plan    --project DIR --handle H --kind exit|queue --session=S --generation G [kind options]
install.sh --user wake install …same… --plan-sha256 D --confirm
install.sh --user wake remove  …same… --confirm
```

**Kind options:**
- exit: optionally `--peer P`, which enables the peer-overdue alarm;
- queue: `--python PY --socket SOCK --binary BIN --binary-sha256 D --thread T`. `T` must equal
  the session.

**`plan`** is read-only. It writes no file, creates no directory, reads no runtime socket
and starts nothing. It prints:
- the instance, the full project identity, and the frozen bundle path and digest;
- for exit, the exact launch argv;
- for queue, the exact config, unit path and unit digest;
- `plan_sha256`;
- `steps`, the owner's next commands as argv arrays. They are bound to this release's own
  path, never to the cache's `current` link.

For queue, `plan` also refuses a service manager older than systemd 254
(`service_manager_unsupported`).

**`install`** recomputes the plan and requires the same digest, so it writes exactly what
the owner read. It freezes the adapter files from the verified pin into
`.ai-comm-local/upgrade/adapters/<sha256>/` (0700, files 0400). An existing bundle is
reused only if it is byte-identical and still private:
- every directory on its path is owned by you, with no group or other permissions;
- every file is yours and mode 0400.

Otherwise `install` stops with `bundle_unsafe` and changes no permissions. The unit
directory's existing ancestors may not be symlinks or writable by another account, and
the directory itself must be yours and writable by no other account (`unit_dir_unsafe`).
Group write on it is accepted only for your own private group, when the account databases
positively show that nobody else is in it: 0775 is the normal default with user-private
groups. That judgement rests on what `pwd` and `grp` can enumerate on this machine. If a
directory service does not enumerate its accounts here, another account could exist unseen,
so keep the directory 0755. `wake` never changes its mode.
- **For exit,** that is all it does.
- **For queue,** it checks every destination first. Then, under `instance.lock`, it writes
  `config.json` (the ownership record), then the unit file
  `queue-wake@<instance>.service`, published exclusively.

`install` never runs the queue `init`, never reloads, enables or starts a unit, and never
reads the runtime. Those are the owner's printed steps:
1. queue `init`, which is the first live runtime read;
2. `systemctl --user daemon-reload`;
3. `systemctl --user enable --now queue-wake@<instance>.service`.

For exit, the session launches the printed argv with its host's background mechanism.

**`remove`** deletes only this instance's unchanged data, its config and its recorded
unit. It removes what is **installed**, not a fresh plan, so it also works after a guided
upgrade has rebound the package:
- **queue:** it checks the installed config, which must match the requested project,
  handle, session, generation and runtime. It does not require that binding to still be
  the handle's current one, so a stale instance can be removed after a takeover.
- **exit:** an exit instance keeps no record of its session, so `remove` still requires
  the handle's current binding. It never deletes a lock file, a bundle, a receipt, the channel or the pin, and it
never stops or disables a unit.
- **exit:** the run lock must be free. Under `phase.lock`, then `lock`, then
  `instance.lock`, it deletes `cursor.json`, `run.json`, `pause`, `pause-ack` and
  `peer-alarm.json`.
- **queue:** the conditions:
  - the unit must be inactive and disabled. `systemctl --user show` must answer with
    exactly the four requested properties, and an empty, partial, duplicated or unknown
    answer refuses (`service_state_invalid`);
  - `state.json` must be valid, with no pending attempt (otherwise `pending_attempt`);
  - there must be no `rebind-*` recovery directory (otherwise `recovery_data_present`).

  Under `guided.lock`, then `observer.lock`, then `instance.lock`, it first writes
  `remove-intent.json`, recording the exact config, state and unit digests. Then it
  deletes the unit, `paused.json`, `state.json`, `config.json`, and the intent last.
  While the intent exists, both adapters refuse to start (`removal_in_progress`). An
  interrupted removal is completed by rerunning `remove`.

  A rerun refuses `removal_state_changed` if `state.json` changed in between.

Any unexpected entry in the instance directory refuses removal
(`instance_unexpected_files`) and changes nothing.

## Declaring an adapter to the guided upgrade controller

`install.sh --user wake integration …` prints one exact declaration item for the
upgrade controller. It writes nothing: the owner reviews the item and adds it with the
project's declaration tool. Arguments:
- the same identity arguments as `plan`;
- `--bundle-sha256 D`, which selects an **existing** frozen bundle explicitly. A rebind
  keeps an older bundle, so the item always names the bundle the instance actually runs,
  never one computed from the current pin;
- one or more `--pair FROM TO` (directed 40-hex commits). There is no "any version".

Queue also needs `--config` (the instance's own `config.json`) and
`--transition ABS --transition-sha256 D`. The transition is validated by the selected
bundle's own code: its identity, its code and its two endpoints. Each pair must be that
transition's direction or its exact reverse.

The item:
- `id` is `exit-wake-<handle>` or `queue-wake-<handle>`, and `owner` is the handle;
- `probe` is run by the owner;
- `steps` are `pause`, `rebind`, `check` and `resume`;
- `code_files` are the bundle's files, so the controller checks those frozen bytes in
  every phase.

The output is `{"status": "integration", "item": …, "item_sha256": …}`: add `item` itself, and use
`item_sha256` (the sha256 of its canonical JSON) to confirm that it was not changed in transit.

The handle must still be the channel's current binding for that session. The item is
not a release or an activation.

## Owner recovery

**`unit_orphan`** means a unit file exists for this instance but no config records it,
for example after a manual edit.
1. Check that it is not active or enabled:
   `systemctl --user show queue-wake@<instance>.service --property=ActiveState,UnitFileState`.
2. Delete the unit file at the path `plan` prints.
3. Rerun `install`.

**`state_missing`** means a queue instance has a config but no `state.json`, and no removal
is in progress. A missing state file cannot show whether a reminder was already queued,
so `remove` refuses rather than guess.
1. Make sure the unit is stopped and disabled.
2. In the runtime, check that no reminder from this instance is queued or about to be
   delivered, or cancel it there.
3. Delete the instance's `config.json` and its unit file by hand, at the paths `plan`
   prints. Leave the lock files.

**`pending_attempt`** means a reminder may have been delivered and not yet read. Let the
model read its inbox, or follow the queue adapter's `retry --confirm-not-queued` procedure
in [QUEUE-WAKE.md](QUEUE-WAKE.md). Never clear it just because the model has not answered.

## Exit codes and output

The adapters and the installer share three outcomes:

| Exit | Meaning |
| --- | --- |
| 0 | done, woken, expired, an alarm, or a deliberate pause |
| 75 | transient: retry later |
| 78 | permanent: unsafe or mismatched setup, which the owner must resolve |

A queue unit lists 78 in `RestartPreventExitStatus=`, so it is not restarted.

Errors are fixed, content-free codes:
- adapters print `WAKE-ERROR <code>`;
- guided phases print `{"error": CODE}`;
- the installer prints `{"status": "stopped", "code": CODE}`.

No message text, path from a peer, or raw tool output is printed.

## exit-wake in detail

```text
python3 -I BUNDLE/exit_wake.py --project DIR --project-key K --handle H --session=S --generation G
        [--peer P] [--rounds N] [--wait-timeout S]
```

- **Before its first wait,** it recomputes the project key from the channel's root commit,
  checks the handle's binding, refuses a queue instance or a pending removal, keeps
  `cursor.json` and records `run.json`.
- **Each round,** it waits for new messages with the pin's CLI.
- **It prints one line, then exits:**
  - `WAKE new messages for H; notification cursor N`;
  - `WAKE-EXPIRED re-arm; notification cursor N`;
  - `PEER-OVERDUE …`;
  - `WAKE-ERROR <code>`.

  The session re-launches it after reading its inbox.

**Guided upgrades** drive it with `exit_wake.py probe|pause|rebind|check|resume` and the
same identity arguments. Each phase checks the binding before it changes anything. The
frozen bundle runs every phase, and moving to a newer bundle is a separate step after a
transaction completes.

## Validation status (2026-11-04, release 1.9.0)

- **What has been shown:** every behaviour in this guide is covered by isolated tests with fixtures and fakes: a fake
  channel CLI, fake service manager, fake runtime and temporary projects. The guided-upgrade hooks are run through
  the controller's own validator, `code_hashes` and `probe()`.
- **What was also rehearsed, on one machine, before release:** with a real per-user cache, trusted verifier, pinned
  checkout, channel and CLI, and locally made tags:
  - exit-wake's whole path: plan, install, a run, its phases, a live pause, a real takeover, and removal;
  - a full guided upgrade of exit-wake between two versions with different adapter bytes, including an interrupted
    apply, `continue` and rollback, all from one frozen bundle;
  - queue-wake's plan and install, and `systemd-analyze` on its real rendered unit.

  The "sessions" in those rehearsals were scripted handles, not models, and no queue instance ran.
- **What has not been shown:**
  - **No genuine session pair has been run** (mixed, two of one host, two of the other). Live acceptance with real,
    independently bound sessions and owner approval for each target is still outstanding.
  - **The service manager's real restart and backoff behaviour, and activation, are unproven.** The unit file is only
    syntax-checked with `systemd-analyze`.
  - **queue-wake's runtime contract has a known race:** the resume flag can load an unloaded session after the
    readiness check. A deployment that needs a strict "never loads" guarantee cannot use it.
- **Default:** installing the package activates nothing.

## Limits

- Adapters observe and notify only. Delivery evidence is not proof that a model read or
  answered. Live acceptance needs real, independently bound sessions.
- The trust boundary is the user account: the checks catch mistakes and stale bindings,
  not a hostile process running as the same user.
- queue-wake depends on one specific runtime contract. See [QUEUE-WAKE.md](QUEUE-WAKE.md)
  for its requirements and its known limitation: readiness and delivery are separate calls.
