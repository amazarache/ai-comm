# Snapshot validation measurements

Measured 2026-09-25 with Python 3.12.3 and Git 2.43.0 on a local POSIX filesystem.
Baseline: v0.1.0 (`06cd871aed0c79ed0eb66c2e6c23d38fddcea5f6`). Candidate:
the v0.1.1 control-character validation optimization. Values are median seconds
from three alternating baseline/candidate reads of the same synthetic channel,
after an initial validation by each package.

| Text | Messages | v0.1.0 | v0.1.1 candidate |
|---|---:|---:|---:|
| ASCII | 100 | 0.02424 | 0.01838 |
| ASCII | 1,000 | 0.18935 | 0.13307 |
| ASCII | 3,000 | 0.73646 | 0.56959 |
| Multilingual | 100 | 0.02273 | 0.02024 |
| Multilingual | 1,000 | 0.18894 | 0.16563 |
| Multilingual | 3,000 | 0.75920 | 0.67525 |

At 3,000 messages this is approximately 23% less validation time for the ASCII
fixture and 11% less for the multilingual fixture. The measurements include Git
history/blob reading and complete validation, but exclude process startup, send
publication, rendering, and downstream task execution. This is not a token-cost
or general throughput claim.

Each fixture has two synthetic participants, one Git commit per ASK message,
a short summary, and either a 285-character ASCII body or a 245-character
multilingual body. It contains no artifacts, source registries, questions or
agreement events. Those features and longer bodies can materially change cost.
Git fast-import constructs the temporary fixture outside the timed operation;
both packages then validate it fully. Warm filesystem results depend on hardware,
load and Git object layout. Real `send` is not bypassed in the correctness tests.

## Reproduce

Use the candidate checkout and a separate trusted checkout pinned to v0.1.0:

```sh
python3 scripts/benchmark_snapshot.py --baseline /absolute/path/to/v0.1.0
```

The script creates and cleans its own temporary channels. It never uses an
existing application channel, modifies the baseline checkout, or contacts a
network service. Output includes all timing samples and medians. Optional
`--messages 100 1000 3000 --repeats 5` changes the workload and repetitions.
`--candidate` can select another explicitly trusted local package checkout.

## Why this change

A preliminary profile of 3,000 ASCII messages attributed 0.766 s of 1.629 s
profiled execution to `text_field` character checks. Profiling itself adds
overhead; the table uses unprofiled timings. The new check uses compiled control
character patterns and retains Unicode category checks for non-ASCII text.
An exhaustive regression compares every code point through U+10FFFF in both
multiline modes with the frozen v0.1.0 predicate, plus type, whitespace and length
boundaries. No character rejection rule is relaxed.

There is no persistent cache, index, or weaker integrity path. The complete
snapshot is still read and checked, with the existing 64 MiB limit. Cost grows
with the full history; large registries and agreement histories can add work
beyond a linear scan. Incremental validation or indexing needs a separate design
and corruption/recovery review if future workloads justify it.
