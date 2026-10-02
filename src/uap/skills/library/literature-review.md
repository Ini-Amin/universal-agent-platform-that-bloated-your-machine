---
name: literature-review
description: Produce a scoped, evidence-backed academic literature review
domain: research
required_capabilities: web.search, document.read
version: 1
---

# Academic Literature Review

## When to use
The task asks for a survey of prior work, a state-of-the-art summary, or an
evidence base for a claim.

## Method
1. **Fix the scope first.** Write one sentence stating the question, the
   population/domain, and the time window. Everything else filters against it.
2. **Define inclusion/exclusion criteria** before searching, so selection is not
   driven by which papers are easy to find.
3. **Search systematically.** Combine keyword variants and synonyms, and record
   the exact query and source for every retrieval round (reproducibility).
4. **Screen in two passes.** Title/abstract first, then full text. Log why each
   excluded paper was dropped.
5. **Extract into a matrix.** One row per included paper: claim, method, sample,
   dataset, key result, limitation.
6. **Synthesize, do not summarize.** Group findings by theme or by agreement /
   disagreement, then state where evidence is strong and where it is thin.
7. **Report limitations** of the review itself: coverage, language bias, and
   any reliance on secondary sources.

## Checks
- Every claim in the synthesis traces to at least one included paper.
- Contradictory findings are surfaced, not averaged away.
- Search queries are recorded so the review can be reproduced.

## Pitfalls
- Citing abstracts as if the full method was read.
- Confusing correlation reported in a study with causal evidence.
- Over-weighting highly cited but dated work.
- Letting the synthesis drift into a list of one-paragraph summaries.
