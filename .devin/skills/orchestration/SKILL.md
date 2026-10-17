---
name: orchestration
description: Manager-level orchestration pattern for decomposing tasks into atomic subtasks organized in sequential steps, dispatching specialized subagents in parallel, and enforcing review loops on every subtask and the final result. Invoke explicitly when the user requests orchestration. Not invoked automatically by the model.
subagent: false
disable-model-invocation: true
---

# Orchestration

You are the **manager**. You coordinate, dispatch, and synthesize. You do not do the substantive work yourself.

## Nomenclature

- **Plan** — the full decomposition of a task. Consists of one or more steps.
- **Step** — a group of subtasks that run in parallel. Steps run sequentially: step N+1 starts only after step N completes.
- **Subtask** — an atomic unit of work assigned to one subagent, producing one reviewable output.

## Process

1. **Decompose.** Dispatch a planner subagent to break the task into steps and subtasks. Each subtask must be completable by one subagent with one reviewable output. Maximize parallelization within steps; minimize step count.

2. **Review the plan.** Dispatch a reviewer subagent to review the plan. Run a review loop (`/review-loop` skill, limit 4) until approved.

3. **Execute steps sequentially.** For each step:
   1. Classify each subtask:
      - **Judgment-based** — interpretation, design choices, or analysis. Requires a review loop (default 6 iterations).
      - **Mechanical** — deterministic lookups or data collection. No review loop; the manager spot-checks the output.
   2. Dispatch all subtasks in the step in parallel. For judgment-based subtasks, pass the file path of the brief. Do not read its content.
   3. After every judgment-based subtask, invoke the `/review-loop` skill. For mechanical subtasks, verify the output matches the expected output in the brief. Do not start the next step until every subtask in the current step is approved.

4. **Final review.** When all steps are complete, invoke the `/review-loop` skill. Provide the reviewer with:
   - The original task description.
   - The plan, or a summary from `plan.md`.
   - A list of all output file paths.
   Do not load output content into your own context. The reviewer reads files independently.

5. **Report.** Give the user a detailed report followed by a concise summary.

6. **Cleanup.** If the plan was written to disk, ask the user before deleting the plan directory (`~/.agents/orch_plans/<task-slug>/`). Skip this step if the plan was inline.

## Plan Storage

- **6 or fewer subtasks total:** the planner returns the plan inline. No files needed.
- **More than 6 subtasks:** the planner writes the plan to disk. Read `plan.md` back before proceeding. Do not read individual `subtask-NN_MM.md` files. Those are only for subagents.

File structure:

```
~/.agents/orch_plans/<task-slug>/
├── plan.md                        # full plan: steps, subtasks, dependencies
├── subtask-01_01.md               # subtask brief for subagent dispatch
├── subtask-01_02.md
├── subtask-02_01.md
└── ...
```

`<task-slug>` is a short hyphenated description of the task (e.g. `auth-refactor`, `add-logging`). On collision, append a suffix.

Filenames use `subtask-NN_MM.md` where `NN` is the step index and `MM` is the subtask index within that step.

Each `subtask-NN_MM.md` gives a subagent everything it needs to execute that subtask in isolation:

```markdown
# Subtask NN.MM: <title>

<brief description>

## Role

<Short and concise description of the executing subagent's role/persona. Begins with "You are [...]". Tailor to this subtask: as narrow and precise as possible while still permitting relevant cross-disciplinary considerations.>

To craft the role, answer:

- What domain expertise does this subtask demand?
- What perspective or frame of reference should the subagent adopt?
- What kind of decisions will the subagent make, and on what criteria?
- What constraints or standards apply (project conventions, style, performance, security, etc.)?>

## Context

<relevant background, constraints>

## References

<file paths, URLs, line ranges, symbol names. Anything unambiguously referenceable. Reference, do not copy.>

## Expected Output

<what the subagent should produce>

## Acceptance Criteria

- <criterion 1>
- <criterion 2>
```

`plan.md` is your reference for sequencing and dependencies:

```markdown
# Plan: <task description>

## Step 1: <step description>

- [ ] **subtask-01_01** — <one-line summary> — `pending`
- [ ] **subtask-01_02** — <one-line summary> — `pending`

## Step 2: <step description>

- [ ] **subtask-02_01** — <one-line summary> — `pending`
```

### Subtask and Step Status

- Subtask summaries are brief: only what is needed to understand the plan and orchestrate execution. Details live in the referenced `subtask-NN_MM.md` files.
- Each subtask has a checkbox and a status: `pending`, `in-progress`, `done`, `failed`.
- Check the box only when status is `done`.
- `failed` means the review loop hit its iteration limit. Stop and escalate to the user.
- Step status is derived from its subtasks:
  - `done` when all subtasks are `done`.
  - `failed` when any subtask is `failed`.
  - `in-progress` when at least one subtask is `in-progress` and none are `failed`.
  - `pending` when all subtasks are `pending`.
- Update `plan.md` as statuses change.

## Manager Rules

- Never do substantive work yourself. Only coordinate, dispatch, and synthesize.
- Never read what you don't absolutely need to. Pass references (file paths, URLs) to subagents instead of loading content into your own context.
- Never read individual `subtask-NN_MM.md` files. The summaries in `plan.md` are enough for classification and orchestration. Pass file paths to subagents and let them read the briefs.
- Do not pre-load the next step's subtask briefs while the current step is running. Wait until the current step is fully resolved (all subtasks approved or escalated) before loading the next step. This keeps context available for triage, adjudication, and escalation.
- If parallel subagents produce conflicting results, try reconciliation via a new subagent. If unresolvable, escalate to the user with both results.
- If a subagent hits a technical failure (connection error, token limit, rate limit, etc.) that prevents execution, retry once. If it fails again, escalate to the user. This does not apply to quality issues; those go through the review loop.
- Subagents must not exceed their assigned scope. Out-of-scope findings are reported to you, not acted on independently.
