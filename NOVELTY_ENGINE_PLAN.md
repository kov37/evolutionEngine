# Novelty Engine Plan

## Objective

Create a small, coherent context manager for evolveEngine that helps a local
small/medium model make sustained coding progress. The primary verification
task is SymPy SWE-bench issue `sympy__sympy-13878`; the final result must be
independently verified with the real tests, not inferred from the model's
completion message.

The experimental project starts from commit `105b565` and lives in the
separate `novelty-context-engine` worktree. The original evolveEngine checkout
is the historical reference and must remain untouched.

## Design principle

Keep only mechanisms that improve verified progress, context efficiency,
recovery, or reproducibility. Existing governors, sidecars, meta-tools, and
duplicated state systems are deletion candidates. The target is one clear
control loop, one state model, one evidence/novelty archive, and one
verification path.

## Two-model architecture

The larger Ollama model is the primary coding actor. The small 4B Ollama model
is a first-class context worker and should be used concurrently when useful.
It must not edit code or silently declare success.

Candidate 4B responsibilities:

- extract facts from tool results;
- maintain compact structured working state;
- detect duplicate reads and stagnation;
- retrieve relevant prior evidence;
- classify test and patch failures;
- rank the next useful action;
- score trajectory novelty;
- trigger compaction or recovery when needed.

The 4B worker should emit strict, compact structured records rather than
verbose prose. Example:

```json
{
  "phase": "mutate",
  "new_facts": [],
  "relevant_facts": [],
  "duplicate_action": false,
  "stagnating": false,
  "recommended_action": "patch_file",
  "confidence": 0.82
}
```

The 4B worker's output is advisory and mechanically bounded. The engine must
not let it create an attention-heavy stream of competing instructions.

## Iterative implementation passes

### Pass 0: establish the baseline

- Run the unmodified baseline against SymPy #13878 in fresh workspaces.
- Use multiple seeds and record the exact Ollama model, settings, hardware,
  git SHA, runtime, and context size.
- Preserve raw trajectories, diffs, test output, and failure causes.

### Pass 1: minimal evidence state

- Replace scattered state mechanisms with an append-only evidence ledger.
- Track observations, hypotheses, mutations, tests, failures, and decisions.
- Build a compact restartable state snapshot.
- Add deterministic duplicate-action and context-pressure measurements.

### Pass 2: progress control

- Use explicit phases: orient, localize, hypothesize, mutate, verify, repair.
- Select the next action from evidence instead of allowing unlimited analysis.
- Trigger a concrete edit when the evidence threshold is met.
- Add safe checkpoints and rollback for failed mutations.
- Require mutation plus independent validation before completion.

### Pass 3: concurrent 4B context worker

Test these 4B modes independently:

1. synchronous processing after every tool result;
2. asynchronous event consumption while the larger model works;
3. batched processing every few turns;
4. event-triggered processing after large outputs, failures, repeated reads,
   or context pressure;
5. a minimal hybrid using local heuristics plus selective 4B calls.

### Pass 4: novelty archive

Archive distinct trajectories and strategies rather than random code changes.
Behavior descriptors may include exploration breadth, mutation timing, files
and functions touched, test-driven versus source-driven work, hypotheses tried,
verification depth, and recovery behavior.

Retrieve useful prior evidence while preserving exploration of underrepresented
strategies. Novelty must never displace direct task evidence or verification.

### Pass 5: verification-driven recovery

- Feed exact test failures back into the state ledger.
- Distinguish syntax failures, wrong localization, incorrect formulas,
  environment failures, and tool-call failures.
- Retry with a changed strategy, not an identical prompt.
- Preserve the best independently verified workspace state.

### Pass 6: tuning and ablation

Compare each change against the previous commit and run ablations:

- no 4B worker;
- 4B summarization only;
- 4B progress control only;
- 4B retrieval only;
- 4B novelty scoring only;
- concurrent hybrid;
- full engine.

No component is retained solely because it sounds useful.

## Metrics

### Primary outcome metrics

- independently verified SWE-bench resolution;
- number of passing `FAIL_TO_PASS` tests;
- preservation of `PASS_TO_PASS` tests;
- time and iterations to resolution;
- resolution rate across repeated seeds.

### Progress metrics

- iterations to first successful mutation;
- iterations to first improved targeted test result;
- successful versus failed mutation rate;
- test-score improvement over time;
- unique files and functions meaningfully inspected;
- recovery rate after failed edits;
- premature or false completion rate.

### Context metrics

- duplicate-tool-call rate;
- analysis-only turns;
- stagnation episodes and duration;
- context tokens retained per useful action;
- compaction frequency and retrieval accuracy;
- 4B latency, token use, and overhead;
- larger-model wall-clock time and throughput.

### Novelty metrics

- archive coverage;
- number of distinct strategies discovered;
- novel-trajectory rate;
- best quality per behavioral niche;
- quality-diversity score;
- percentage of archived evidence later reused successfully.

## Overnight execution requirements

- isolate every run in a fresh workspace;
- bound iterations, wall-clock time, context size, and retries;
- write JSONL trajectories and periodic status snapshots;
- preserve diffs and test output automatically;
- retry transient Ollama failures without infinite loops;
- support resuming interrupted runs;
- execute multiple seeds sequentially or concurrently when hardware allows;
- compare every run against the baseline automatically;
- never delete existing user artifacts during cleanup.

## Promotion gates

A change is promoted only when it:

- passes the engine's deterministic tests;
- does not increase false completion or uncontrolled context growth;
- measurably reduces analysis paralysis or duplicate actions;
- matches or beats baseline performance across repeated seeds;
- demonstrates independently verified progress on SymPy #13878;
- eventually produces a correct independently verified solution.

The final claim of success is based on the official-style independent test
verification, not on self-reported model state.

## Research references

- [SWE-bench FAQ and resolution metrics](https://www.swebench.com/SWE-bench/faq/)
- [SymPy #13878 task specification](https://www.tbench.ai/registry/swebench-verified/head/sympy__sympy-13878)
- [Quality Diversity: A New Frontier for Evolutionary Computation](https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2016.00040/full)
- [SWE-bench Verified background](https://openai.com/index/introducing-swe-bench-verified/)

