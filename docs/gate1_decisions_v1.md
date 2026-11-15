# Gate 1 Decisions for the First RQ1 Generation Run

These settings are adopted by B for the first pilot at `eve_route_planner/C1`.
They apply unchanged to Vanilla and Mutation-Guided. They should be accepted by A
before interpreting later-checkpoint results as the formal RQ1 outcome.

## Generation model and budget

| Setting | Decision |
| --- | --- |
| Provider / model | OpenAI Codex, `gpt-6-luna` |
| Reasoning effort | `low` |
| Decoding | Codex service default; no temperature override is exposed by the CLI |
| Calls | At most 3 launched calls per method, task, and source checkpoint: one generation call plus up to two retries only if the Codex process fails before a structured response is captured |
| Output | Ask for at most 4,096 output tokens per call and 12,288 total; Codex CLI exposes no hard output-token override, so this is a prompt target checked against service usage when available, not a binding limit |
| Candidates | At most 8 candidate files; exactly one top-level pytest test function per file |
| Repairs | 0; a completed but malformed or invalid response is retained as rejected evidence, not repaired |
| Binding comparison budget | The 3-call ceiling and 8-candidate cap; mutation-analysis cost is recorded separately |

The model invocation runs in an ephemeral, read-only Codex CLI session whose working
directory contains no benchmark files. Its only benchmark input is the archived prompt,
constructed from the sanitized current checkpoint package. No hidden tests, future
checkpoint files, future failures, or mutation results enter the Vanilla prompt. A retry
is allowed only for a process failure before a structured response is captured; it counts
against the three-call ceiling and does not permit repairing a completed answer.

## Candidate validation and freezing

1. Run the unchanged package base suite first; if it fails, stop and freeze nothing.
2. Require valid candidate JSON, safe `test_*.py` paths, valid Python syntax, exactly
   one top-level pytest test function per candidate file, and no hidden/future paths,
   network use, randomness, or wall-clock dependence.
3. Collect each candidate with the declared pytest command and require one stable test ID.
4. Run the complete unchanged base suite plus each candidate three times. Any collection,
   timeout, or execution failure rejects that candidate.
5. Require a written specification-based correctness rationale before retaining a test.
   There is no trusted reference implementation for this task; correctness is checked
   against explicit specification requirements and the candidate's independently
   documented rationale.
6. Freeze retained tests with the exact input hashes, prompt, generation record,
   validation logs, repeat count, and suite hash before any later checkpoint is opened.

## Data and provenance decisions

- The generator may read only the sanitized package's specification, workspace, and
  complete `base_tests` tree.
- `data/hidden_tests`, `data/future_tests`, and `data/excluded_tests` are never prompt
  inputs. `file_backup/errors` remains excluded until A confirms its legal visibility.
- Release snapshots are used as candidate frozen trajectories. A still needs to confirm
  that the supplied run manifest and aggregate records satisfy trajectory provenance.
- This first result is a B-side pilot. It is not the RQ1 regression comparison; A owns
  later-checkpoint execution, candidate-event deduplication, and event adjudication.

## Reuse for Mutation-Guided

Mutation-Guided must use the same model ID, reasoning effort, call/candidate/output
budgets, prompt common material, and validation rules. Its only additional input is the
eligible surviving-mutant records from the same source checkpoint. Charge all calls and
repairs to the method that uses them.

## Vanilla pilot generation status

The isolated prompt and input manifest are archived under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/vanilla_gpt6luna_low_20261018/`.
Four CLI launches were recorded: attempt 1 has unknown request status and is conservatively
counted; attempt 2 failed before model request and is not counted; attempt 3 reached the
service but returned no candidate because the structured-output schema used an unsupported
`uniqueItems` keyword; attempt 4 completed under the user's explicit authorization and
returned a valid empty `tests` array. The three-call budget is exhausted. The result has
zero emitted and retained generated tests. The unchanged C1 base suite passed (1 passed,
1 skipped), and the Vanilla suite was frozen with the five base-suite files and no generated
files. Its suite SHA-256 is
`5280a6e542918a68daf6889b2095965429fdb9a6b64e504019d8b4d1830ced58`. Token usage was not
reported by the CLI, so input/output token counts remain null.

## Mutation-Guided C1 status

The Guided prompt, generator, and shared validator now accept only complete same-checkpoint
survivor records. The C1 mutation summary is complete (605 mutants; 579 killed, 26 survived),
and all 26 surviving diffs match the `hauling.py` SHA-256 in the C1 package. Local preflight
rendered a 39,984-character prompt from the same eight package inputs plus the survivor set.
The user later explicitly authorized sending the C1 specification, source, complete legal
base suite, and the 26 survivor IDs, locations, and diffs to GPT-6 Luna. One Guided call
completed and returned a valid empty `tests` array. It used 26,122 input tokens and 14 output
tokens; one of the three maximum launched calls was counted. The frozen Guided suite contains
the five unchanged base-suite files and no generated files, with SHA-256
`5280a6e542918a68daf6889b2095965429fdb9a6b64e504019d8b4d1830ced58` (the same suite as
Vanilla). The base suite passed (1 passed, 1 skipped). No candidate required correctness or
target-mutant validation. Per the predeclared attempt policy, retries are allowed only when
the process fails before capturing structured output; this valid empty response is final and
must not be regenerated under the same run budget.
