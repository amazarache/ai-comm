# Compact prose versus symbolic claim notation

Two reviewers compared ten existing messages: five factual reports, three
corrections and two decisions. Private source material remains outside this
repository. These are aggregate measurements, not a public reproducible dataset.
Generic failure cases are covered by the unit tests.

An encoder produced compact prose with shared registries, and an alternative
using symbolic claim tuples. Independent decoders received one arm plus its
dictionary without the source conversation. A separate checklist contained 158
items covering numbers, claims, questions, confidence, decisions and corrections.

| UTF-8 minified JSON bytes, ten-message sample | Original | Compact prose | Claim shorthand |
|---|---:|---:|---:|
| Messages | 61,234 | 32,967 | 31,455 |
| Shared registries | included above | 12,228 | 12,228 |
| Format note/dictionary | — | 1,042 | 2,185 |
| **Total** | **61,234** | **46,237** | **45,868** |

Compact prose saved **24.5% including setup and registry costs** in this sample.
Claim shorthand saved only **0.8% more**. The larger 46% message-only saving
excludes necessary shared context and must not be presented as the total saving.
No tokenizer was available: these are byte counts, not token or billing savings.

Compact prose recovered all 158 cross-message checklist items, but was **not
lossless per message**: an original mistaken count was omitted and recovered only
from its later correction. This prompted an explicit no-hindsight-editing rule.
Claim shorthand scored 154/158, with two distortions and two omissions. Examples
of the mechanism were changing “approximately twice” into “more than twice”,
losing a measurement's reference frame, and dropping a decision. Additional
ungraded drift was observed. A finite checklist does not prove general fidelity.

The agreed gate required at least 30% additional byte savings over compact prose
and no graded semantic errors. Claim shorthand failed both and is excluded from
protocol 2.0. Readable ASCII acts such as ASK, REVIEW and ACCEPT remain because
they make intent and state transitions checkable; they are not a secret language.

Most practical savings come from incremental reads, declaring sources once,
question deltas, concise summaries and one automated send operation. The sample
does not establish a production-wide saving, a token saving, or an optimal format.
