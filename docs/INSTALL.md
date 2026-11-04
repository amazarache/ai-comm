# Installing ai-comm for one user

ai-comm is Python and Git; there is nothing to compile. It needs a POSIX system (Linux or macOS), Python 3.10 or later
and Git 2.25 or later. It never needs root or `sudo`.

There are two places ai-comm can live:

- **A project's pin**, `<project>/.tools/ai-comm`: the exact release a project's channel uses
  (`install.sh --project`, see [RELEASES.md](RELEASES.md)).
- **The per-user cache**, `$XDG_DATA_HOME/ai-comm` (default `~/.local/share/ai-comm`): verified releases for this
  user, with `current` and `previous` links (`install.sh --user`). Changing the cache never changes a project's pin,
  channel, declaration, listener or guidance.

## One-line install

Each release (from 1.8.0) publishes `install-ai-comm.sh`, a small bootstrap that installs **that** release into the
per-user cache. It downloads the release archive and `SHA256SUMS` over HTTPS only (every redirect too, with bounded
time), checks the archive's own `SHA256SUMS` record and every archive member, then runs the release's
`install.sh --user install`. That installer re-verifies the tag, the release index and the manifest, and runs the full
test suite, before publishing anything. By default it installs into the cache only; `--register SKILLS_DIR`
(repeatable) and `--launcher BIN_DIR` add the skill link and the `ai-comm` command. It never needs root and touches no
project. If another release is already current, the new one is installed but not activated, and the bootstrap prints
the `activate` command. If the version is already in the cache, it refuses without changing anything.

Replace `X` with the version you chose.

**Direct (fastest):**

```sh
curl -fsSL --proto =https --proto-redir =https --tlsv1.2 \
  https://github.com/amazarache/ai-comm/releases/download/vX/install-ai-comm.sh | sh
```

Add options after `sh -s --`, for example `| sh -s -- --register ~/.agents/skills --launcher ~/.local/bin`. A
convenience URL, `https://github.com/amazarache/ai-comm/releases/latest/download/install-ai-comm.sh`, always serves
the newest release's bootstrap, which pins its own version; use it only when you deliberately want the newest.

**Verify first (recommended):** the bootstrap runs only if the whole downloaded file matches the digest in the table
below. Run it in any POSIX shell; the parentheses give it its own subshell, so the temporary file is removed when the
command ends and your shell's traps are untouched:

```sh
( f=$(mktemp) || exit 1; trap 'rm -f "$f"' EXIT; trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM
  curl -fsSL --proto =https --proto-redir =https --tlsv1.2 --connect-timeout 20 --max-time 120 -o "$f" \
    https://github.com/amazarache/ai-comm/releases/download/vX/install-ai-comm.sh \
  && echo "<sha256>  $f" | sha256sum -c - && sh "$f" )
```

On macOS use `shasum -a 256 -c -` instead of `sha256sum -c -`. To read the bootstrap first, replace `sh "$f"` with
`cat "$f"`, or download it to a file of your own with `-o install-ai-comm.sh`.

| Release | File | SHA-256 |
| --- | --- | --- |
| `v1.9.0` | `install-ai-comm.sh` | `208cbc4d1e6a331bb2d0ecad5c65e5d6e7f6161776d93eeb43faf9aaa85106cf` |

The table is generated before each release is tagged (`scripts/render_bootstrap.py`), and the release workflow
refuses to publish a bootstrap whose digest differs from it.

**What this trusts.** Piped into `sh`, the bootstrap itself runs before you have read it, fetched from GitHub over
HTTPS. `SHA256SUMS` comes from the same release as the archive, so it proves the download is intact, not who
published it. A tag cannot move (the repository's rules forbid it), but a release asset can still be replaced; the
digest in the tagged repository's table above is what detects a changed bootstrap. The direct pipe only guarantees
that a download cut inside the bootstrap's function body does not run a partial body; the verify-first command
rejects any corrupted or truncated download before it runs. ai-comm releases are not signed yet; signatures with an
independently published key are the planned next step.

## First install on a new machine (manual steps)

The steps verify everything they download before anything from it runs. They pin one version; there is no "latest"
and no silent fallback. Replace `X` with the version you chose, for example `1.6.0`.

Download the archive and `SHA256SUMS` of release `vX` (the GitHub command-line tool works too:
`gh release download vX -R amazarache/ai-comm -p 'ai-comm-X.tar.gz' -p SHA256SUMS`), then check exactly the archive's
own record. `SHA256SUMS` also lists `install-ai-comm.sh` from 1.8.0 on, which this route does not download, so check
the one selected line rather than the whole file:

```sh
work=$(mktemp -d) && chmod 700 "$work" && cd "$work"
curl -fsSL --proto =https --proto-redir =https --tlsv1.2 \
  -O https://github.com/amazarache/ai-comm/releases/download/vX/ai-comm-X.tar.gz \
  -O https://github.com/amazarache/ai-comm/releases/download/vX/SHA256SUMS
grep -c 'ai-comm-X\.tar\.gz' SHA256SUMS                   # must print 1; stop otherwise
grep -x '[0-9a-f]\{64\}  ai-comm-X\.tar\.gz' SHA256SUMS | sha256sum -c -   # must print "ai-comm-X.tar.gz: OK"
```

On macOS use `shasum -a 256 -c -` in place of `sha256sum -c -`.

Extract it. A release archive contains only regular files and directories, so the command first checks every
member and refuses the whole archive, before anything is written, if any name is absolute, contains a `..`
component or appears twice, or if any member is a symbolic link, hard link, device or other special file. It then
extracts with Python's `data` filter as a second layer:

```sh
mkdir pkg && python3 -I -c 'import sys, tarfile
assert hasattr(tarfile, "data_filter"), "this Python lacks safe tar extraction; use a newer patch release"
with tarfile.open(sys.argv[1]) as archive:
    members, names = archive.getmembers(), set()
    for m in members:
        parts = m.name.split("/")
        if (not m.name or m.name.startswith("/") or ".." in parts or m.name in names
                or not (m.isreg() or m.isdir())):
            sys.exit("refusing the archive: unexpected member " + repr(m.name))
        names.add(m.name)
    archive.extractall(sys.argv[2], members=members, filter="data")' "ai-comm-X.tar.gz" pkg
```

Then install into the per-user cache. `--fetch-permitted` is your consent to fetch exactly tag `vX` and the release
index from the package's own release origin; `--use-git-credential-helper` lets that fetch use your configured Git
login for the private repository:

```sh
sh pkg/install.sh --user install --version X --fetch-permitted --use-git-credential-helper
```

The installer checks that the archive you verified, the fetched annotated tag and the release index name the same
manifest; then it runs the tag and manifest checks and the full test suite in a private staging directory before
publishing the release into the cache. You can delete the download directory afterwards.

**What this proves and what it does not.** The download comes over authenticated HTTPS, and the archive, tag and
index agree with each other. That is consistency, not independent proof of who published the release: ai-comm
releases are not signed yet. The one-line install above trusts exactly the same things, plus the bootstrap script
itself; the verify-first variant checks that script against the tagged digest before running it. A later
signed-release design will add an independently trusted key.

## Managing the cache

All commands print JSON and stop with a code on any problem.

| Command | Effect |
| --- | --- |
| `install.sh --user status` | offline report: current and previous release, installed releases, trusted bindings |
| `install.sh --user check` | offline: is a newer verified release known? |
| `install.sh --user check --fetch-permitted` | with your consent, refresh only the release index and `v*` tags, and record trusted bindings |
| `install.sh --user install --version X --fetch-permitted --no-activate` | add a release without switching to it |
| `install.sh --user activate X` | switch `current` to an installed release; the old one becomes `previous` |
| `install.sh --user rollback` | switch back to `previous` |
| `install.sh --user recover` | finish or undo an operation that was interrupted |
| `install.sh --user register --skills-dir DIR` | link `DIR/ai-comm` to the cache's entry skill (below) |
| `install.sh --user unregister [--skills-dir DIR]` | remove those links, only where they are unchanged |
| `install.sh --user launcher --bin-dir DIR` | write the optional `ai-comm` command (below) |
| `install.sh --user launcher --remove` | remove it, only if unchanged |
| `install.sh --user offer status`, `offer claim`, `offer answer` | the cache's own update offer (below) |
| `install.sh --user uninstall --confirm` | remove the cache, its links and launcher; anything unexpected or changed is kept and reported |

`check` reports `verdict`: `update_available`, `up_to_date`, or `unknown`. It says `up_to_date` only when the release
metadata is fresh (refreshed within seven days, not dated in the future, last refresh attempt successful); otherwise
`metadata` says `missing`, `stale` or `failed`, and the verdict is `unknown`. The cache follows the release origin of
its first install; a different origin stops with `origin_changed`.

While an operation is unfinished (for example after an interruption), every command that changes something stops with
`operation_pending` until `recover` has run. Recovery finishes an operation only from the exact state it left behind;
anything it cannot prove is the cache's own is kept and reported, never deleted.

## Checking a project's pin

A project uses its own pinned copy of ai-comm in `.tools/ai-comm`. Before anything runs from it, check it with the
cache's code, never with the pin's own:

```sh
REL=$(cd "${XDG_DATA_HOME:-$HOME/.local/share}/ai-comm/current" && pwd -P)
python3 -I "$REL/scripts/verify_pin.py" --project /path/to/project
```

The result is `verified` (exactly a release the cache knows, with its upgrade `class`), `unknown` (no trusted record
for its commit), `modified` (a trusted commit whose files differ; `what` and `path` say where) or `absent`. The check
reads Git objects without a work tree and re-hashes them, and hashes every released file in the working tree, so Git
index flags such as `assume-unchanged` cannot hide a change and no repository hook or helper runs. The cache knows a
release once it installed it, or after a consented `check --fetch-permitted`, which records every indexed release.
Untracked files in the pin (for example `__pycache__`) are outside this check. Hash agreement is consistency, not proof
of who published a release.

## The entry skill

`install.sh --user register --skills-dir DIR` makes the cache's entry skill (`entry/ai-comm/SKILL.md`) visible to agent
hosts that read skill directories. Name a directory your hosts read; `~/.agents/skills`, from the open Agent Skills
convention, is one example. Nothing is registered unless you name a directory, and the directory must already exist.

Registration checks the skill names hosts would see, not just file names: if any other entry in `DIR`, or in a
directory registered earlier, already declares `name: ai-comm`, or `DIR/ai-comm` holds something else, it reports the
conflict and changes nothing. ai-comm cannot know which other directories a host scans or how it ranks duplicates; if
you register in more than one directory, confirm in each host that exactly one `ai-comm` skill appears.

The entry skill verifies the project's pin first and then hands over to the pin's own instructions.

## The `ai-comm` command (optional)

`install.sh --user launcher --bin-dir DIR` writes `DIR/ai-comm`, a short shell script. Run from inside a project, it
resolves the cache's `current` release once, checks that release, verifies the project's pin with it, and only then
runs the pin's own `ai_comm.py` with your arguments. An unverified pin is never run; the command prints the report
instead. It never replaces an existing `DIR/ai-comm`.

## Cache update offers

The cache's offer is separate from each project's own offer. One session at a time is the notifier: `offer claim
--session REF` returns `claimed` to exactly one session (a claim lapses after 24 hours or when a newer release is
offered) and `claimed_by_other` to the rest. That session asks you once and records your answer with `offer answer
--session REF --version V --answer A`, where `A` is `yes`, `fetch_only`, `not_now` (24 hours, then 48 hours, then 7
days), `skip` or `never`. `fetch_only` permits installing that release without activating it; `yes` also permits
activating it. Neither permits anything else, and no project's pin is changed.

## Declaring a project's listeners

Guided upgrades of a project's pin read `.ai-comm-local/upgrade/project.json`. For a verified pin, the cache can write
it, so this works even for pins that predate the command:

```sh
python3 -I "$REL/scripts/declare_project.py" init --project DIR
python3 -I "$REL/scripts/declare_project.py" install --project DIR --file F --sha256 H --previous P
```

`init` writes the unintegrated starting file and never overwrites one. `install` replaces the declaration with the
exact reviewed bytes of `F`: their digest must be `H`, they must validate against the real project, no guided upgrade
may be in progress, and the live file must still have digest `P` (or be absent, `--previous absent`). Nothing the
declaration names is run.
