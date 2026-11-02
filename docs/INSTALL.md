# Installing ai-comm for one user

ai-comm is Python and Git; there is nothing to compile. It needs a POSIX system (Linux or macOS), Python 3.10 or later
and Git 2.25 or later. It never needs root or `sudo`.

There are two places ai-comm can live:

- **A project's pin**, `<project>/.tools/ai-comm`: the exact release a project's channel uses
  (`install.sh --project`, see [RELEASES.md](RELEASES.md)).
- **The per-user cache**, `$XDG_DATA_HOME/ai-comm` (default `~/.local/share/ai-comm`): verified releases for this
  user, with `current` and `previous` links (`install.sh --user`). Changing the cache never changes a project's pin,
  channel, declaration, listener or guidance.

## First install on a new machine

The steps verify everything they download before anything from it runs. They pin one version; there is no "latest"
and no silent fallback. Replace `X` with the version you chose, for example `1.6.0`.

While the repository is private, download with the GitHub command-line tool, which uses your existing login:

```sh
work=$(mktemp -d) && chmod 700 "$work" && cd "$work"
gh release download vX -R amazarache/ai-comm -p 'ai-comm-X.tar.gz' -p SHA256SUMS
sha256sum -c SHA256SUMS        # must print "ai-comm-X.tar.gz: OK"; stop otherwise
```

On macOS use `shasum -a 256 -c SHA256SUMS`. If the repository becomes public, `curl -fLO` with the two explicit asset
URLs of release `vX` replaces the download command; the checks stay the same.

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
releases are not signed yet. Never pipe a downloaded script into a shell; a later signed-release design will add a
reviewed verifier and an independently trusted key before any downloaded code runs.

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
| `install.sh --user uninstall --confirm` | remove the cache; anything unexpected or changed is kept and reported |

`check` never claims a release is current when the release metadata is missing or was not refreshed; it reports when
the index was last refreshed.
