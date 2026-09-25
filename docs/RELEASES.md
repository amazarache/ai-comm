# Offline release awareness

The communication CLI never fetches or contacts a URL. To refresh release
awareness, an operator must explicitly fetch the named refs in the package
checkout and then run `release-status`:

```sh
git -C .tools/ai-comm fetch origin refs/heads/release-index:refs/remotes/origin/release-index
git -C .tools/ai-comm fetch origin 'refs/tags/v*:refs/tags/v*'
python3 .tools/ai-comm/ai_comm.py release-status
```

`release-status` reads only the current checkout's HEAD and manifest, the local
`origin/release-index` ref, and local annotated tags. A missing or invalid index
produces no advice. Every index entry must agree with its annotated tag's target,
manifest digest in the tag message, manifest file, and `PROTOCOL.md` at that tag.
Checks use committed HEAD, so working-tree edits do not affect package identity.
Git 2.44 and newer disables lazy fetching during these reads; on older Git
versions partial clones receive no release advice rather than triggering a
network fetch. This is consistency evidence, not publisher authentication. The private repository has
no provenance attestation; output says `authenticity: not verified (no
attestation)`.

The `release-index` branch contains one authoritative `RELEASES.json` file.
The file is strict JSON with format `ai-comm-releases/1` and a `releases` array.
Each release entry contains `package_version`, `commit`, `tag`,
`manifest_sha256`, `protocol_sha256`, `summary`, `advisory`, and `reason`, with
optional `affects` and `fixed_in` fields as described below. Every index edit
needs exact-artifact review and publication by the designated publisher.

An upgrade announcement is a FACT on topic `upgrade`. Its artifact is canonical
JSON with `kind: ai-comm-release`, package version, commit, tag, manifest and
protocol digests, advisory level, and reason. `recommended` and `required`
records need `affects`; a bounded range also needs a `fixed_in` above that whole
range. An unbounded range has no `fixed_in` and represents an unfixed issue.
Ranges are conjunctions of one or more `>=`, `>`, `<=`, `<`, or `==` comparators
with X.Y.Z decimal versions, no leading zeroes, spaces, or wildcards. Required
advisories name a concrete security, data-loss, or protocol-safety risk.

`state` and `inbox` derive notices from these records. An advisory applies
exactly when the local version matches `affects`; `fixed_in` is displayed as an
upgrade target and never clears the advisory. Older release announcements are
neutral as version signals, while any matching advisory remains visible. Newer
optional notices cannot hide an applicable required advisory. A notice never
installs, fetches, or re-pins a package.

## Automation

`ci.yml` runs on main, candidate branches, and pull requests against Linux and
Python 3.10–3.13. It validates the release manifest and protocol digest, runs the
test suite, and scans tracked source files for credential patterns, private
network addresses, and home paths. CI provides evidence only; it does not ACCEPT
or DECIDE a release. `stability.yml` is manual-only and runs 20 full-suite
repetitions plus 200 focused race-test runs. `release.yml` checks pushed tags,
then creates a GitHub Release with the source archive, SHA256SUMS, and matching
`CHANGELOG.md` section. Manual dispatch verifies an existing tag without
publishing. The workflow force-fetches the annotated tag before checking it.
Only its package job gets `contents:write`; no extra action is used to publish.
It does not publish a provenance attestation or claim publisher authenticity.
