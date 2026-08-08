# Recursive LLM Coding: A First-Principles Reading Guide

This guide is for learning how LLM coding agents become iterative, self-improving, and eventually recursive. The central idea is simple: do not begin with grand theories of recursive self-improvement. Begin with the smallest working agent loop, then add feedback, evaluation, mutation, selection, memory, and trustworthy verification one layer at a time.

## The mental model

A recursive coding system can be decomposed into seven parts:

1. **Artifact** — the code, prompt, tool, policy, or agent scaffold being changed.
2. **Mutation operator** — the mechanism that proposes a candidate change.
3. **Environment** — the repository, shell, tests, tools, and task context.
4. **Evaluator** — the mechanism that decides whether the candidate is better.
5. **Selection rule** — the policy that accepts, rejects, or archives candidates.
6. **Memory** — the state inherited across attempts or generations.
7. **Trust boundary** — the components the evolving system cannot modify or spoof.

The most important lesson is that recursive improvement is not merely “an LLM editing its own code.” It is a search process whose behavior is dominated by its evaluator, selection rule, retained state, and trust boundary.

## The shortest serious reading list

If you read only five things, read these in order:

1. [mini-SWE-agent](https://github.com/swe-agent/mini-swe-agent) — the smallest practical coding-agent loop.
2. [ReAct](https://arxiv.org/abs/2210.03629) — the reasoning/action/observation pattern behind tool-using agents.
3. [SWE-agent](https://arxiv.org/abs/2405.15793) — why the agent-computer interface changes coding performance.
4. [STOP: Self-Taught Optimizer](https://arxiv.org/abs/2310.02304) — an early direct treatment of self-improving scaffolds.
5. [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954) — archive-based, open-ended evolution of coding agents.

That sequence gives you the practical loop, the interaction model, the environment design problem, recursive mutation, and evolutionary selection.

## Stage 1 — Understand the basic agent loop

### 1. mini-SWE-agent

**Why it matters:** It exposes a coding agent without hiding the essential loop behind a large framework.

**Read for:** How a task becomes a prompt, how actions are executed, what observations return to the model, how history accumulates, and what causes the run to stop.

While reading, trace one complete trajectory by hand:

```text
task → model → action → environment → observation → updated history → model
```

Then identify the corresponding pieces in `harness.py` and `agent.py`.

### 2. ReAct: Synergizing Reasoning and Acting in Language Models

**Why it matters:** ReAct provides the conceptual vocabulary for interleaving reasoning, external actions, and observations.

**Read for:** The difference between internal reasoning, executable actions, and external evidence. Pay particular attention to how a wrong observation can redirect all later reasoning.

| ReAct concept | Coding-agent equivalent |
|---|---|
| Thought | Diagnosis or plan |
| Action | Shell command, file edit, search, or test |
| Observation | Tool output, file contents, compiler error, or test result |
| Trajectory | The complete attempt to solve the task |

## Stage 2 — Understand feedback and memory

### 3. [Self-Refine: Iterative Refinement with Self-Feedback](https://arxiv.org/abs/2303.17651)

**Why it matters:** It separates generation, feedback, and revision into explicit stages.

**Read for:** What information the critique contains, whether the critic is independent of the generator, and whether repeated refinement truly adds evidence or merely amplifies the model’s existing beliefs.

### 4. [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)

**Why it matters:** It shows how textual feedback can persist between attempts without changing model weights.

**Read for:** Episodic memory, credit assignment, and the difference between remembering a failure and learning a generally useful strategy.

Key distinction:

- **Within-attempt refinement:** revise the current candidate.
- **Across-attempt memory:** retain information for a later attempt.
- **Across-generation heredity:** retain code or policy changes in descendants.

These are different mechanisms and should be measured separately.

## Stage 3 — Understand coding environments and evaluation

### 5. [SWE-bench](https://arxiv.org/abs/2310.06770)

**Why it matters:** It makes the coding task concrete: issue text, a real repository, executable tests, and a patch-level outcome.

**Read for:** Task construction, repository state, test-based grading, contamination risk, and why a passing visible test suite is not identical to general correctness.

### 6. [SWE-agent](https://arxiv.org/abs/2405.15793)

**Why it matters:** It demonstrates that the interface exposed to an agent is part of the algorithm, not incidental plumbing.

**Read for:** Tool design, observation formatting, action constraints, error recovery, and how interface choices alter the search space. Also inspect the [official repository](https://github.com/swe-agent/swe-agent).

## Stage 4 — Study recursive agent design

### 7. [STOP: Self-Taught Optimizer](https://arxiv.org/abs/2310.02304)

**Why it matters:** STOP directly studies a scaffold that improves the program responsible for producing improvements.

**Read for:**

1. What program is editable?
2. What remains fixed?
3. How are candidate rewrites produced?
4. Which evaluation determines survival?

The fixed pieces matter as much as the editable ones. They define the experiment’s trust anchor.

### 8. [Automated Design of Agentic Systems (ADAS)](https://arxiv.org/abs/2408.08435)

**Why it matters:** ADAS treats agent architectures as objects that can be generated and evaluated automatically.

**Read for:** The meta-agent, the representation of agent programs, the search loop, and transfer across tasks. Compare the paper with the [official implementation](https://github.com/ShengranHu/ADAS).

### 9. [A Self-Improving Coding Agent](https://arxiv.org/abs/2504.15228)

**Why it matters:** It moves self-improvement into a coding-agent setting where the agent changes its own implementation.

**Read for:** Editable scope, benchmark feedback, accepted-change persistence, and protection against regression. See the [official implementation](https://github.com/MaximeRobeyns/self_improving_coding_agent).

### 10. [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)

**Why it matters:** DGM combines self-modifying coding agents with an archive of prior variants rather than relying on a single lineage.

**Read for:** Parent selection, archive maintenance, diversity, evaluation, heredity, and the boundary between empirical improvement and theoretical guarantees. The [Sakana AI project explanation](https://sakana.ai/dgm/) is a useful companion.

## Stage 5 — Learn the evolutionary-computation vocabulary

### 11. [Introduction to Evolutionary Computing — Eiben and Smith](https://link.springer.com/book/10.1007/978-3-662-44874-8)

**Why it matters:** Recursive coding systems often reinvent evolutionary algorithms with different vocabulary.

**Read for:** Genotype versus phenotype, mutation, selection pressure, elitism, population diversity, fitness landscapes, premature convergence, and noisy fitness.

Map the terms into an LLM coding system:

| Evolutionary term | Recursive coding analogue |
|---|---|
| Genome | Editable source, prompt, configuration, or tool policy |
| Mutation | LLM-generated patch |
| Phenotype | Behavior produced by running the candidate |
| Fitness | Evaluator score or acceptance decision |
| Selection | Choosing a parent or accepting a child |
| Heredity | Persisting accepted changes |
| Population | Candidate archive or parallel lineages |

### 12. [Quality-Diversity Optimization](https://quality-diversity.github.io/)

**Why it matters:** A single “best so far” lineage can become trapped. Quality-diversity methods preserve multiple capable but behaviorally different candidates.

**Read for:** Archives, behavior descriptors, novelty, local competition, and when diversity is useful rather than decorative.

### 13. [FunSearch](https://deepmind.google/blog/funsearch-making-new-discoveries-in-mathematical-sciences-using-large-language-models/)

**Why it matters:** It is a clear example of LLM-generated programs being filtered by an external evaluator and accumulated through evolutionary search.

**Read for:** Program representation, evaluator design, island models, and the division of labor between generative priors and exact execution.

### 14. [AlphaEvolve](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)

**Why it matters:** It extends the generate-evaluate-evolve pattern toward larger algorithmic systems and multiple evaluators.

**Read for:** Automated evaluation, evolutionary population management, and the properties a domain must have before this approach is useful.

## Stage 6 — Understand the theoretical origin

### 15. [Gödel Machines: Self-Referential Universal Problem Solvers](https://arxiv.org/abs/cs/0309048)

**Why it matters:** This is the theoretical origin of provably beneficial self-rewriting systems.

**Read for:** The difference between a formally proved rewrite and the benchmark-based acceptance used by contemporary systems.

| Theoretical Gödel machine | Contemporary recursive agent |
|---|---|
| Proves improvement | Measures improvement |
| Formal utility | Benchmark score |
| Proof search | LLM-generated mutations |
| Globally justified rewrite | Empirically selected candidate |
| Theoretical guarantee | Experimental evidence |

`evolutionEngine` is best described as an empirical self-modifying agent-search system, not a Gödel machine in the strict proof-based sense.

## Stage 7 — Understand verification failure

In a recursive system, the evaluator becomes part of the effective specification. A system that can optimize strongly against a weak evaluator will eventually expose the evaluator’s omissions.

### 16. [Defining and Characterizing Reward Gaming](https://openreview.net/forum?id=yb3HOXO3lX2)

**Why it matters:** It supplies vocabulary for cases in which an optimizer performs well on a proxy while violating the intended objective.

**Read for:** Reward gaming, misspecification, proxy objectives, and why apparently reasonable metrics remain exploitable.

### 17. [SWE-ReX: Sandboxed Code Execution for AI Agents](https://github.com/SWE-agent/SWE-ReX)

**Why it matters:** It is a practical reference for separating coding agents from the environments in which their code executes.

**Read for:** Execution isolation, reproducibility, environment lifecycle, parallel evaluation, and containment assumptions.

Verification vocabulary to master:

- Goodhart’s law and proxy optimization
- Reward hacking and specification gaming
- Test overfitting and benchmark contamination
- Self-report versus external evidence
- Training-evaluation leakage
- Mutable evaluators and immutable trust anchors
- Time-of-check/time-of-use failures
- Capability, environment, and secret leakage
- Compute and complexity gaming

## Five-week study plan

| Time | Theme | Deliverable |
|---|---|---|
| Week 1 | Basic agent loops | Read mini-SWE-agent and ReAct. Trace one trajectory manually and annotate the corresponding components in `harness.py`. |
| Week 2 | Feedback and environments | Read Self-Refine, Reflexion, SWE-bench, and SWE-agent. Compare feedback, memory, tools, and stopping behavior. |
| Week 3 | Recursive agent design | Read STOP, ADAS, and A Self-Improving Coding Agent. Build a matrix of mutation, evaluation, selection, memory, and trust. |
| Week 4 | Evolution and archives | Read DGM, FunSearch, AlphaEvolve, and introductory evolutionary-computation material. Reassess hill climbing and archive search. |
| Week 5 | Verification and experimentation | Study reward gaming and sandboxing. Define data splits, compute-matched controls, observability, and ablations. |

## Reading worksheet

Answer the same questions for every system:

1. What artifact is being improved?
2. What generates mutations or candidate changes?
3. What is the fitness or utility function?
4. Who controls the evaluator?
5. What state persists between attempts or generations?
6. How are parents or prior candidates selected?
7. What prevents regression?
8. What prevents evaluator gaming?
9. How is generalization measured?
10. What baseline receives equal compute?

If a paper does not answer one of these questions, record it as an open assumption. Do not silently fill the gap with your preferred design.

## Comparison matrix template

| Dimension | System A | System B |
|---|---|---|
| Editable artifact |  |  |
| Mutation operator |  |  |
| Evaluator |  |  |
| Selection |  |  |
| Memory/archive |  |  |
| Feedback |  |  |
| Trust boundary |  |  |
| Generalization |  |  |
| Compute budget |  |  |
| Failure mode |  |  |

## Apply the readings to evolutionEngine

As you read, map every concept back into the repository. The useful output is a set of testable hypotheses about the current system.

| Concept | Current location |
|---|---|
| Agent loop | `harness.py` and `agent.py` |
| Tool interface | `kernel/`, `registry.py`, and `dispatch.py` |
| Mutation operator | `evolve/self_edit_step.py` plus the fixed 35B model |
| Candidate genome | Editable orchestration files in the worktree |
| Fitness evaluation | `evolve/evolve_verifier.py` and `evolve_tasks.py` |
| Selection | Hill-climb and archive rules in `evolve_runner.py` |
| Heredity | Accepted Git commits |
| Behavioral memory | Feedback text and inherited orchestration code |
| Population memory | Archive of accepted parent SHAs |
| Trust boundary | External verifier, fixed kernel, and isolated execution policy |

Questions to carry into implementation:

- Are we evolving a general tool-building harness or attacking one hard problem family?
- What information should survive a failed generation?
- Should neutral mutations survive, and under what secondary criteria?
- How will we distinguish general improvement from benchmark specialization?
- What receives a held-out evaluation that never enters the feedback loop?
- How will we measure improvement per token, model call, and minute?
- Which accepted changes remain beneficial under ablation and repeated seeds?
- Where does recursive modification stop, and what remains the immutable trust anchor?

## Bottom line

Start with mini-SWE-agent, not abstract recursive-self-improvement theory. Then follow the direct lineage:

```text
mini-SWE-agent → ReAct → SWE-agent → STOP → ADAS
→ A Self-Improving Coding Agent → Darwin Gödel Machine
```

At every step, study the evaluator as carefully as the generator. A stronger mutation engine paired with a weak verifier does not reliably produce a better agent; it produces a better optimizer of the verifier’s blind spots.
