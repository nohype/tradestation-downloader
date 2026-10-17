---
name: review-loop
description: Iterative review pattern for any substantive work artifact. A reviewer subagent assesses the artifact against its original instructions and returns APPROVE or DISAPPROVE with findings. On DISAPPROVE, an implementer subagent fixes all findings, then a new reviewer re-evaluates. Repeat until APPROVE or the iteration limit is reached. Usable in any context (orchestration subtask validation, standalone quality checks, or final deliverable review).
disable-model-invocation: false
---

# Review Loop

An iterative review cycle for any substantive artifact (research, code, analysis, plan, documentation, etc.).

## Process

1. **Dispatch a reviewer subagent.** Provide:
   - The artifact to review.
   - The original instructions (verbatim prompt or document reference).
   - The three review criteria:
     - **Completeness** — all requirements addressed.
     - **Correctness** — no factual or logical errors.
     - **Alignment** — matches instructions exactly, no scope creep, no unintended content.

2. **Reviewer evaluates** the artifact against the three criteria. Returns **APPROVE** or **DISAPPROVE** with specific findings (per issue: what, where, why, fix). Order findings by criticality on a continuous spectrum, highest first. Criticality is set by (a) immediate impact on the subtask and (b) anticipated future cost. Problems that are non-blocking today but will become disproportionately expensive to fix as more work builds on them rank higher than their immediate impact alone would suggest.

3. **On APPROVE:** done. Proceed.

4. **On DISAPPROVE:**
   1. Dispatch an implementer subagent to fix **all** findings. The implementer addresses only the reviewer's findings: no scope expansion, no unrelated changes. Ensure the subtask output as a whole stays coherent, consistent, and follows all applicable rules, guidelines, and instructions.
   2. Dispatch a new reviewer subagent for the next round.
   3. Repeat from step 2 until APPROVE or the iteration limit is reached.

5. **Iteration limit.** Default is 6. The invoker may override this by giving an explicit limit.
   - If all remaining findings are non-critical (style, minor gaps, optimizations: nothing affecting correctness or alignment), proceed and report the outstanding findings.
   - If any critical findings remain, halt and present the user with the artifact, findings, and recommended next steps.

6. **Disputes.** If the implementer disputes a reviewer finding with evidence, the invoker adjudicates. If unresolvable, escalate to the user.
