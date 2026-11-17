# B-side Test Generation Protocol (Draft)

**Status:** Method validation closed on 2026-11-14; full-cohort development is approved, but pre-evaluation preparation remains incomplete. See the [validation phase exit review](../reports/validation_phase_exit_20261114.md). The [team protocol](experiment_protocol.md) remains authoritative for shared decisions.
**Scope:** Mutation analysis and generated-test preparation for RQ1. The team protocol is authoritative for checkpoint construction, legal test access, and regression adjudication.

## 1. Research question and comparison

RQ1 asks whether, under the same test-generation budget, Mutation-Guided Tests detect more confirmed regressions introduced by later checkpoints than Original Tests and Vanilla LLM Tests.

For each eligible starting checkpoint `C_i`, compare these frozen suites:

- **Original:** the complete legal suite at the source checkpoint's `base_tests_path` in the sanitized package. Do not create an additional visible/held-out split. Upstream `hidden`, future checkpoints, and the isolated `file_backup/errors` tests are outside the experiment inputs.
- **Vanilla:** Original plus validated tests generated from the allowed `C_i` inputs.
- **Mutation-Guided:** Original plus validated tests generated from the same allowed `C_i` inputs and surviving-mutant diffs from `C_i`.

The exact base-test contents are frozen by A's package and shared unchanged by all methods. Do not create an additional visible/held-out split. Hidden tests and all future-checkpoint information are excluded from generator inputs.

## 2. B-side input and output contract

B consumes A's immutable checkpoint package and reads its `workspace_path`, `workspace_revision`, `specification_path`, `base_tests_path`, `base_tests_sha256`, `test_command`, and `runtime_environment_id` fields. These names and representations now match the package exporter. Paths are relative to the package root. Run `../scripts/validate_checkpoint_package.py` before generation; it verifies package paths, source and test hashes, and the absence of hidden, excluded, and future test files from the generator input tree.

Recursive package validation, isolation, gap-brief construction, and prompt gathering must
support Windows extended-length paths. A previous `Path.rglob` traversal silently omitted
deep C1 fixtures and failed to find some future files; the affected utilities now use
extended-path traversal, and the package audit checks all 196 packages before cohort runs.
For each generator input manifest, compare the supplied base-test file/hash set against
the complete sanitized package tree. A subset is a preparation error, even if the test
command itself can collect successfully.

The Release archive itself is not safe to pass directly to a generator. Use only the sanitized tree named by `data/checkpoints/ISOLATION_MANIFEST.json`; hidden, future, and deferred error test data are stored in sibling isolated roots.

B returns a frozen test-suite package for each generated method, with the suite path, source checkpoint, model and budget identifiers, validation record, and hashes for generated test files. The common base suite remains identifiable and is not silently modified by generated tests.

JSON Schemas for mutant records, generation runs, and frozen suites are in [`../schemas/`](../schemas/). The checkpoint package and suite package need A+B agreement at Gate 1/2 before being treated as a stable interface.

Prompt drafts are in [`prompts/`](../prompts/). Both generators use the same structured candidate-output schema; only Guided receives surviving-mutant records and assigns target mutant IDs. Prompt wording and output shape are drafts until jointly ratified.

## 3. Mutation analysis

1. Record the clean source revision/digest, runtime environment, base-test command, and mutmut configuration before running analysis.
2. Run the complete legal base suite under coverage. Preserve the command, raw output, machine-readable coverage report, and source digest.
3. Run mutmut against the same checkpoint and base suite. Preserve raw tool output and every mutant result; do not silently drop candidates.
4. For each reported mutant, record its ID, operator, source location, diff, raw tool outcome, normalized status, and supporting logs.
   Extract diffs in configurable batches (`--diff-chunk-size`, default 32) to bound container memory use. If a batch fails with an out-of-memory error, halve that batch and retry the same mutants down to one at a time. Each completed batch is cached in the runtime directory, so an interrupted export can resume without re-extracting earlier diffs. The selected starting batch size is written to the export summary and does not change mutant selection or classification.
5. Verify on a representative mutant that the test process imports the mutated source. Record the mutated source path or digest observed by the subprocess. If tests launch the program in a child process, make sure mutmut's stats phase receives trampoline hits from that child and associates them with the pytest test ID; otherwise mutmut may mark every mutant `not checked`. A test that accidentally imports the original checkout invalidates the mutation result.
6. Preserve every raw mutant record and directly classify each mutant by applying it to staged C1 source and running stable passing C1 test IDs. Mutations that cannot be applied or whose run is incomplete are errors/inconclusive and must not be called survivors. A test-level error on a selected stable test can count as killed only when raw mutmut also reports `killed` and the output has no known resource/harness error marker; retain the errored test IDs and rationale in the direct record. For `execution_server/C1`, the readiness fixture's final `connection refused` text can be stale after an initial request receives a non-200 response; when the only errored selected test is `TestCore.test_initial_stats`, treat it as a kill only if raw mutmut reports `killed` and the traceback contains the fixture's `Server did not start` message. Keep this exception narrow and preserve its rationale. On resume, validate and recover worker shards independently against task/checkpoint, source and baseline hashes, algorithm/protocol version, stable test IDs, and mutant IDs/statuses; one incomplete sibling shard must not discard a valid shard. Archive shard error/timeout attempts and parent-run inconclusive attempts, retry unresolved mutant IDs on resume, and retain the attempt-history JSONL with the normalized bundle. A direct `error` or `timeout` remains an inconclusive outcome in the current bundle; do not count it as killed or survived. Bundle `complete` means every expected mutant has exactly one hash-matched record, while `all_attempts_resolved` separately indicates whether any execution errors/timeouts remain. A finished mutmut run that exits nonzero may still be reused when its hash-matched result listing is nonempty; preserve its raw statuses and send the mutant inventory to direct classification. `not checked` is never a survivor or a complete raw mutation score. If mutmut reports `no tests` and the subprocess-hit log has no matching target-function hit, record direct status `not_tested` and normalized status `inconclusive`; do not call it a survivor or claim the exact mutated line is unreachable. Other mutants without a function-specific hit use the full stable suite, and their target-function reachability remains unknown. Direct survivors remain `needs_review` because mutmut does not prove semantic non-equivalence. Specification review and the Guided target-kill check prevent unsupported assertions from being retained, while unfiltered hints may consume generation budget.

If direct classification yields zero surviving mutants, record a valid zero-call Guided run with zero generated candidates, skip candidate correctness review, and pass the unchanged shared base suite through the normal baseline validator and freezer. This is a completed empty Guided suite, not a generation failure; its generation record must retain the mutation bundle hash and explicitly say why no model call was made. The generation-run schema permits zero eligible mutants and zero CLI attempts for this case.

### Normalized mutant statuses

| Status | Operational meaning |
|---|---|
| `killed` | The base suite fails on the mutant, with failure attributable to the changed behavior. |
| `survived` | The mutant executes and the base suite passes. Eligible survivors may be shown to the Guided generator. |
| `invalid` | The mutant cannot be meaningfully evaluated because the changed program is syntactically invalid, cannot import, or otherwise cannot reach test execution. Preserve the exact failure evidence. |
| `unreachable` | Coverage or execution tracing shows the mutated location was not reached by the base suite. This is a triage label, not a synonym for a surviving mutant. |
| `suspected_equivalent` | Review suggests no behavior change under the applicable specification, but equivalence has not been proved. Preserve the review rationale. |
| `inconclusive` | Timeout, harness failure, ambiguous result, or an explicit `not_tested` result prevents a reliable killed/survived decision. |

Store the tool's raw status separately from the normalized status. If an outcome combines conditions (for example, an uncovered line inside a function that mutmut did not execute), keep the evidence and mark it inconclusive instead of inferring a kill/survival result.

The direct-classifier JSONL uses `not_tested` when mutmut says `no tests` and there is no
matching subprocess hit. The merged mutant record maps that state to `inconclusive`; it is
excluded from the survivor pool and the mutation-score denominator, but retained in counts.
This avoids treating missing test relevance as either a true survivor or proof that the
exact changed line is unreachable.

Direct outcomes `error` and `timeout` are also preserved in the raw classification record
and normalized to `inconclusive` in the merged bundle. They are excluded from the survivor
pool and mutation-score denominator; the bundle audit still requires exactly one status
record and a hash-matched diff for every mutant. A bundle can therefore be complete as an
inventory while still reporting inconclusive mutant outcomes; those counts remain explicit
and are not presented as kills or survivors.

### Survivor quality triage and hint selection

Treat mutation execution and semantic usefulness as separate decisions. A direct `survived`
status means only that the stable C1 base tests passed on the applied mutant. It does not
establish that the mutant is non-equivalent, specified, or useful as a test-generation hint.
Keep every raw mutant and every direct status in the archive; triage only controls which
survivors are exposed to the Guided generator.

Classify every direct survivor exactly once against the same C1 specification and source:

| Triage status | Required rationale |
|---|---|
| `eligible` | Cite a specific C1 requirement, describe the externally observable behavior changed, give a deterministic triggering input, and state the correct expected result/oracle. The behavior must be testable through a supported public interface. |
| `deferred` | The behavior may be specified, but a required fixture, controlled input, or defensible oracle is not yet available. Name what is missing. |
| `suspected_equivalent` | Explain why the diff appears unable to change specified external behavior. This is a suspicion, not a proof; retain it in the archive. |
| `out_of_scope` | Explain why the change has no supported C1 requirement or only changes incidental/internal behavior. |

Do not use a function-level subprocess hit as proof that the exact mutated line ran. Use it
only as supporting evidence or as a tie-breaker. Do not label a survivor unreachable from a
function hit alone.

Before sending eligible mutants, group them into behavior families. Mutants belong to one
family only when they concern the same C1 requirement and substantially the same triggering
input and observable difference. Preserve all family members in the raw records. When a
family is represented in the generation prompt, prefer one representative with a concrete
trigger and clear oracle; record the other family members as not selected because of
redundancy. This avoids spending a generation call on several near-duplicate diffs without
changing the archived mutant pool or its direct classifications.

In each triage decision, `behavior_family_id` identifies that family,
`selected_for_generation` records whether the survivor is included in the prompt, and
`selection_rationale` explains any eligible survivor that is omitted. Formal triage files
must set the selection flag explicitly for every eligible record. Legacy v1 triage files
without the field continue to select all `eligible` survivors. The input manifest records
semantic eligibility separately from the selected hint IDs, and test freezing rechecks the
selected IDs against the hash-matched triage file.

If the prompt still exceeds a practical size limit, freeze a per-unit hint cap and a
deterministic ranking rule before the formal cohort. Rank only `eligible` representatives:
explicit specification anchor first, then concrete trigger and oracle, then independent
behavior family; use the stable mutant ID as the final tie-breaker. Record selected and
omitted IDs and reasons. Do not tune this selection using later checkpoints, regression
events, or generation outcomes. The same cap and ranking rule must apply to every task.

Keep the Vanilla and Guided generation budgets paired: same model/version, call ceiling,
output-token ceiling, candidate-file ceiling, retry policy, and validation repetitions.
Record the Guided hint count and prompt/input usage separately; mutation-analysis runtime is
also reported separately from the LLM generation budget. The mutant shortlist may guide the
content of Guided tests, but it must not grant Guided extra calls or candidate slots.

The checkpoint runner pins `max_stack_depth = 8` and `mutate_only_covered_lines = false`. This keeps mutmut's normal test-relevance depth while retaining mutants in specification-relevant code that the current base suite does not cover. Do not lower the stack-depth limit or switch to covered-lines-only mutation in the formal cohort: mutmut documents that lower depth is faster but leaves more survivors, and covered-lines-only mutation would omit untested behavior gaps ([mutmut 3.8.0 documentation](https://pypi.org/project/mutmut/3.8.0/)).

### Subprocess-aware mutmut runner

For the checkpoint harness, `scripts/run_checkpoint_mutmut.py` runs mutmut in a fresh
staging directory and installs two adapters. `sitecustomize.py` records trampoline hits
made in benchmark child processes. Each process writes to its own JSONL file under a
thread lock; the pytest plugin validates and aggregates those records before replaying
them into mutmut's stats collector. This avoids interleaved writes on Docker Desktop bind
mounts.

The C1 baseline audit sorts stable test IDs for deterministic reporting, so the runner
reconstructs their original pytest collection order from the first baseline repetition.
It also exports that order to the pytest plugin, which restores it when mutmut runs a
per-mutant test subset (mutmut stores those subsets as unordered sets). For long-lived
standard-library HTTP servers, the plugin passes the active pytest node ID in an internal
request header and `sitecustomize.py` uses it to attribute server-process hits to the
requesting test.

Protocol v4 records a SHA-256 signature over the effective mutmut table, mutmut version,
runtime configuration, Docker image, requirements, source/test hashes, ordered test
selection, asset mounts, bridge hashes, and worker count. Cohort reuse requires the current
protocol, runtime configuration, bridge, and worker count to match. Direct
reclassification also records its algorithm version and the raw mutmut-run signature.
The C1 cohort audit requires the protocol worker count (`max_children=4`) by default,
so older bundles produced with another worker count remain visible but are not counted
as protocol-complete. New runs use `v4` output suffixes so earlier evidence remains
available and cannot be mixed into the corrected cohort.

The task's CLI must be imported as a module (`python -c "import hauling; hauling.main()"`)
so mutmut's trampoline module is the one loaded by the child process. Running
`python hauling.py` makes the module `__main__` and does not reliably activate the
mutated module. The adapter also passes the checkpoint-specific required pytest options
to both stats and ordinary test runs.

Use a targeted mutant only to verify the association mechanism:

```powershell
python scripts/run_checkpoint_mutmut.py `
  --runtime-dir tmp/checkpoint-runtime/eve_route_planner/C1-REPLACE_WITH_STAGING_ID `
  --max-children 4 `
  --mutant hauling.x_read_csv__mutmut_1 `
  --debug
```

For mutation analysis, omit `--mutant` so mutmut evaluates the full eligible set. The
runner saves the raw `mutmut` output, subprocess-hit JSONL, `mutmut results --all true`
status listing, source/configuration hashes, and a run manifest under a fresh ignored
`tmp/` directory. `--all true` is required to retain killed mutants in the status file.
Because the trampoline bridge can mislabel subprocess mutants as survivors, the formal
cohort's final killed/survived status comes from the direct apply-and-test classifier;
retain both raw and direct evidence.
A targeted run is not a complete mutation score: any `not checked` mutants mean the
score is incomplete. Do not report the target-only killed/unchecked counts as an RQ1
result.

## 4. Generator inputs and information boundary

Vanilla and Mutation-Guided use the same model/version, common prompt material, current `C_i` specification, current source snapshot, and complete legal base suite. Mutation-Guided additionally receives only the selected surviving-mutant IDs and diffs produced from that same `C_i`. Before a full cohort generation, prepare an audited triage decision for every direct survivor; `run_guided_c1_cohort.py` requires `--mutant-triage-dir` and passes only `eligible` mutants. The explicit `--allow-untriaged-survivors` override is for exploratory runs only because unfiltered survivors can consume the fixed generation budget on equivalent or specification-irrelevant changes.

Neither generator receives hidden tests, future checkpoints, future failures, regression-event labels, or oracle/adjudication material. Prompts and all supplied files must be retained so this boundary can be audited.

## 5. Budget and fair comparison

Before formal generation, assign a `budget_id` and freeze the budget policy. At minimum, log and compare:

- model provider, exact model/version, decoding settings, and prompt-template version;
- maximum model calls, output-token budget, and candidate-test budget per task/checkpoint/method;
- actual calls and input/output tokens, where provided by the model service;
- candidates emitted, candidates validated, candidates retained, and validation/repair calls;
- generation wall time and mutation-analysis execution time.

Use the same model and predeclared generation limits for Vanilla and Guided. Charge all generation and repair calls to the method that used them. Report mutation-analysis compute separately from the test-generation budget, while retaining it as total pipeline cost. Exact numeric limits and how to handle unavailable token counts must be approved jointly at Gate 1; they must not be tuned after seeing regression results.

## 6. Generated-test validation and freezing

Validate each candidate against the exact source checkpoint used for generation:

1. The test is collected by the declared test runner and has a stable test identifier.
2. It passes on the unmodified `C_i` source and does not rely on hidden or future information.
3. It is consistent with the applicable specification, checked using a trusted correct implementation or an independently documented correctness check. If neither is available, mark the candidate's correctness validation as unavailable; do not call it validated by default.
4. It is repeatable under a predeclared number of runs in the same environment. The run count and flakiness rule are Gate 1 decisions.
5. For Guided candidates, it also fails on the targeted mutant and passes on the original `C_i` source. Record the exact mutant ID and execution evidence.

Retain rejected candidates and rejection reasons. Freeze the accepted suite, test-file hashes, input package hash, prompt/configuration identifiers, and validation log before any later-checkpoint evaluation. Do not alter assertions after observing future checkpoints.

## 7. Required B-side records and preliminary measurements

For every task, trajectory, starting checkpoint, and generation method, retain:

- input checkpoint package and source digest;
- coverage command/report and mutmut configuration/raw results;
- normalized mutant records, including excluded mutants and exclusion evidence;
- exact prompts, model configuration, API-reported usage, candidate files, and generated files;
- candidate-level validation outcomes and frozen suite manifest;
- generation budget and mutation-analysis costs.

RQ1's primary outcome is the count of distinct confirmed candidate-regression events detected by each suite under the team protocol's observed-union analysis boundary; A owns event confirmation and deduplication. Do not describe this count as an absolute regression-detection rate. B may report generation success, valid new-test count, mutation score, and coverage as explanatory measures. These auxiliary measures do not replace RQ1's regression-detection outcome.

## 8. Decisions to ratify at Gate 1

- Exact budget limits and whether candidate count or token count is the binding generation constraint.
- Model/version, decoding settings, and repair-call policy.
- Correctness evidence available for generated tests when no reference implementation is supplied.
- Number of repeat runs and the rule for declaring a test flaky.
- Exact mutmut release/configuration and mapping from its raw outcomes to normalized statuses.
- Checkpoint-package path conventions and fields shared with A.

### B-side pilot settings adopted on 2026-10-18

For the first Vanilla/Guided pilot, B has recorded concrete settings in
[`gate1_decisions_v1.md`](gate1_decisions_v1.md) and
[`generation_budget.json`](../config/generation_budget.json): OpenAI Codex
`gpt-6-luna`, low reasoning effort, at most three launched calls (one generation plus
up to two pre-response process retries), a requested output target of 12,288 total tokens
(4,096 per call), eight candidate files, no answer repairs, and three repeat runs. The
Codex CLI does not expose a hard output-token override, so this token target is checked
post hoc when service usage is available and is not a binding limit. The call and
candidate caps are the binding equal-budget limits; token usage is recorded when the
service reports it and is otherwise left null. These B-side decisions still require A's
agreement before the final RQ1 comparison.

The first Vanilla prompt, input manifest, empty candidate response, and run record are
archived for `eve_route_planner/C1`. The model emitted zero candidates. The unchanged base
suite passed with 1 passed and 1 skipped; the frozen Vanilla suite contains the five
original base-suite files and no generated files. The freeze hash and launch accounting are
recorded in [`gate1_decisions_v1.md`](gate1_decisions_v1.md). This is a valid zero-addition
pilot outcome, not evidence that Vanilla generated regression-detecting tests.

Mutation-Guided uses [`mutation_guided_v1.md`](../prompts/mutation_guided_v1.md) and
[`run_mutation_guided_generation.py`](../scripts/run_mutation_guided_generation.py). The
shared validator now checks that each Guided candidate names eligible same-checkpoint
survivors, passes on the original checkpoint across the configured repeats, and kills each
declared target under the subprocess-aware mutmut runner. Its C1 input preflight verified all
26 survivor diffs against the source hash. Following the user's explicit authorization,
the C1 Guided prompt was sent to GPT-6 Luna. It returned a valid empty `tests` array
(1 call; 26,122 input tokens and 14 output tokens reported), so no candidate required
correctness or target-mutant review. The unchanged base suite passed (1 passed, 1 skipped),
and the Guided suite was frozen with the five base-suite files and no generated files. Its
suite hash matches the Vanilla suite because both contain only the same base tests. Under
the frozen attempt policy, a valid empty response is final; remaining calls are reserved
for process failures before structured output, so this run cannot be retried as generation.
This is a zero-addition pilot result, not evidence that Guided tests detected regressions.

### Exploratory gap-focused generation (2026-10-18)

The empty v1 responses prompted a clause-by-clause audit of the sanitized C1 specification
and legal base suite. The audit is in [`c1_generation_gap_audit.md`](c1_generation_gap_audit.md);
the C1-specific survivor classification is in [`c1_mutant_triage_v1.json`](c1_mutant_triage_v1.json).
The audit identified system-start routing, reverse gate traversal, station/system destination
combinations, Zarzakh return-lock behavior, CLI numeric bounds, and warp-time rounding as
explicitly specified behaviors with little or no base-test coverage.

The intermediate attempts are retained separately. Vanilla v2 produced one validated
Example 2 test (suite SHA-256
`1bc1af9ed38f63c35747cdd6a4f5652a0f8bc181caf85062e16a3cf3fb8771be`); Guided v2 emitted
five candidates but retained none after target and correctness checks. A v3 Vanilla attempt
used the legacy schema because the runner did not yet load the schema from its budget file;
its three responses lacked required gap assessments and valid candidate paths, so they were
preserved but not treated as valid tests. The audit explains these failures and the v4 fixes.

The gap-focused runs used the same `gpt-6-luna` medium configuration, budget ID,
three-call ceiling, eight-candidate ceiling, and three-repeat validation rule for both
methods. Each method actually used one model call. The configured output-token target was
12,288; service usage was 1,243 output tokens for Vanilla and 1,309 for Guided. The
checkpoint source and base-test package hashes matched across the paired runs.

- **Vanilla:** 3 candidates emitted and all 3 retained: same-system system start (C1 Example
  2), Jita system start to the Tama station, and reverse Tama-to-Jita routing. Frozen suite
  SHA-256: `0c12a2b0d198141847528876545beba69fb6bb1b65345179c44bc23120dc20cf`.
- **Mutation-Guided:** 7 of the 26 raw survivors were classified eligible and shown to the
  model; 3 candidates were emitted and 2 retained. Both retained system-start tests killed
  all five declared targets (`hauling.x_main__mutmut_211` through `_215`) and passed the
  original C1 suite three times. A reverse-route test was specification-correct and stable,
  but did not kill its declared adjacency mutants (`154`, `159`), so the Guided validator
  rejected it. Frozen suite SHA-256:
  `da64de84fbad274eba5ef41789a34c95bc7f8bc1486bb35a7b7607acb0c3db18`.
- The common unchanged base suite passed with 1 passed and 1 skipped in both runs. The
  skipped test is an empty upstream hidden-case parametrization in the sanitized package;
  hidden/future inputs were not supplied to either generator.

This corrects the earlier zero-addition pilot for `eve_route_planner/C1`; it remains an
**exploratory generation result**, not the formal 36-task RQ1 comparison. The bundles and frozen suites are under
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/vanilla_gap_v4b_gpt6luna_medium_20261018/`
and
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutation_guided_gap_v4_gpt6luna_medium_20261018/`.

The generation scripts now read the output schema from the budget configuration and record
its hash in the input manifest. The Guided runner can accept a triage file, verify that it
classifies every raw survivor exactly once against the matching task/checkpoint/trajectory
and raw-record hash, and send only `eligible` records. An earlier full-cohort prototype
sent all direct survivors without semantic triage and marked them `needs_review`; treat
those runs as historical exploratory artifacts. The current formal runner requires a
hash-matched triage file for every selected task and sends only `eligible` survivors.
Retained Guided candidates must still pass C1, satisfy an independent specification review,
and directly kill a named survivor.

For the reverse-gate survivors, an engineering-only probe now creates a tiny SDE with one
directed jump row and checks both route directions. Unchanged C1 passes; temporary copies
with the exact survivor `154` and `159` diffs each fail as expected. This confirms the test
fixture separates the two mutations, but it is not a mutmut validation or an LLM-generated
test. The Docker named-pipe access issue described in the original pilot record was resolved
on 2026-10-19. See the fixture under
[`../fixtures/eve-route-planner-synthetic-sde-v1/`](../fixtures/eve-route-planner-synthetic-sde-v1/)
and its probe record under
`../artifacts/results/rq1_full_36x1/eve_route_planner/C1/synthetic_sde_probe_v1/`.

## Gate 2 integration status

The concrete exporter and its sample packages use `base_tests_path` and
`base_tests_sha256`, with a string `test_command`; the checkpoint-package schema now
matches those fields. A standard-library package audit is provided at
`../scripts/validate_checkpoint_package.py`.

The package audit and B-tool container smoke are complete. Docker Desktop's Linux
engine is running, and the package-declared Python 3.12 image (CPython 3.12.15, uv
0.13.1) runs the pinned mutmut/coverage/pytest toolchain. The B-side runtime adapter
now derives the `eve_route_planner/C1` command, including the `--entrypoint` and
`--checkpoint` options required by its pytest conftest. The real base suite still needs
the pinned SDE resource at `base_tests/assets/sde_dir`; the local workspace now has the
locked `scb-problems` checkout. A real C1 run produced 74% line coverage and a complete
mutmut result (605 mutants: 579 killed, 26 survived; 95.70%). The result files are in
`artifacts/results/rq1_full_36x1/eve_route_planner/C1/mutmut_full_20261018/`. The
recovered run did not preserve its original runner manifest/log; this is disclosed in
that result bundle. The RQ1 regression comparison for that one pilot remains separate from
the full-cohort report.

## Formal 36-task cohort status (updated 2026-10-19)

- C1 repeated base-suite audit: complete for 36/36 tasks; 7 suites pass fully and 29 contain stable pre-existing failures.
- Vanilla generation and freezing: complete for 36/36 tasks under GPT-6 Luna medium and the paired three-call/eight-candidate/12,288-output-token budget.
- Direct mutant classification: complete and hash-matched for `cfgpipe`, `code_search`, and `eve_industry`; the remaining task bundles are running or queued. The zero-survivor path now emits an auditable empty Guided result and freezes the shared base suite without a model call.
- Mutation-Guided generation: frozen for `eve_industry` (zero candidates), `cfgpipe` (two candidates rejected because neither killed a named survivor), and `code_search` (one candidate retained after it killed two named survivors); remaining tasks await direct mutant bundles.
- Later-checkpoint evaluation, adjudication, and the full RQ1 report are not complete. The evaluator requires all 108 suites (36 tasks × 3 methods) to be frozen before it reads any target checkpoint package.
