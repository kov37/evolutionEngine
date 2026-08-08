# Recursive Hard-Problem Experiment

## Research question

It makes sense to study whether a weak system can recursively improve its ability to solve a hard problem. The important question is not simply whether it eventually succeeds. The stronger question is:

> Does preserving and modifying successful problem-solving machinery across attempts outperform spending the same compute on independent attempts?

This framing distinguishes cumulative adaptation from ordinary retries, increased sampling, or additional inference-time compute.

## What the experiment could reveal

A weak baseline repeatedly attacking a difficult problem could provide evidence about:

- Which tools and intermediate representations emerge.
- Whether useful improvements accumulate across generations.
- How strongly performance depends on early mutations and path dependence.
- Whether the system develops reusable problem-solving machinery or overfits to one task.
- Where verification pressure produces genuine capability versus evaluator exploitation.
- Whether improvement reaches a plateau, becomes unstable, or collapses.
- Whether specialization is retained across generations.
- Which parts of the system benefit most from modification: prompts, memory, tools, decomposition, orchestration, or search strategy.

Even an unsuccessful run could expose important capability boundaries and failure dynamics.

## Why one fixed problem is insufficient

Optimizing against one unchanging problem creates several ambiguities:

- The system may memorize or reconstruct the answer.
- It may exploit incidental details of the instance.
- It may exploit weaknesses in the evaluator.
- It may build increasingly elaborate machinery that only works for that one case.
- Apparent improvement may not transfer to related problems.

Success on one fixed instance demonstrates optimization pressure, but it does not necessarily demonstrate improved reasoning or general problem-solving ability.

The experiment should therefore use a **hard problem family**, not only one static instance.

## Recommended experiment structure

```text
Visible training instances
        ↓
System modifies tools, prompts, memory, or orchestration
        ↓
Fixed external verifier
        ↓
Fresh hidden instances from the same problem family
        ↓
Accept only improvements that generalize
```

For example, instead of repeatedly presenting one repository defect, provide a family of repository-repair tasks that share an underlying difficulty but vary in implementation, layout, naming, and failure mode.

The system may retain improvements to its toolset and orchestration between generations. The verifier and hidden instances must remain outside its control.

## The role of a weak baseline

A weak starting point is scientifically useful because it leaves room to observe cumulative improvement. However, it must be capable of occasionally generating partial progress.

If every attempt receives the same binary failure result, the search process has almost no useful gradient. Recursive improvement then becomes random mutation and selection.

A weak baseline benefits from independently measured feedback dimensions such as:

- Number or proportion of correctness tests passed.
- Regressions introduced.
- Edge cases handled.
- Robustness under changed inputs.
- Performance on hidden variants.
- Runtime and resource use.
- Number of model calls and tool calls.
- Scope of unnecessary changes.
- Reproducibility across repeated evaluations.

These signals should be computed by trusted external evaluators, not reported by the candidate system itself.

## What the system should be allowed to modify

A useful recursive experiment could allow modifications to:

- System and task prompts.
- Planning and decomposition strategies.
- Context-management policies.
- Memory formats and retrieval strategies.
- Non-privileged tools.
- Tool-selection policies.
- Retry and reflection strategies.
- Search and branching policies.
- Orchestration code below the trusted verification boundary.

It should not directly modify or approve changes to:

- The active evaluator.
- Hidden tests.
- Promotion policy.
- Acceptance thresholds.
- Audit logs.
- Resource accounting.
- The root trust policy.

The system may propose changes to those trusted components, but an older immutable verifier or a human-controlled process must evaluate and activate them.

## Generational trust rule

The experiment should preserve one invariant:

> Generation N may evaluate Generation N+1, but Generation N+1 must never evaluate or approve itself.

Each accepted generation should be tied to:

- Its exact source digest.
- Its parent generation.
- The verifier version that approved it.
- The evaluation inputs or input-set identifier.
- Per-metric results.
- Compute and wall-clock cost.
- The complete mutation or patch.

This produces an auditable lineage and permits rollback or retrospective analysis.

## Necessary control groups

The recursive system should be compared against compute-matched baselines:

1. **Independent attempts:** the weak baseline starts fresh each time.
2. **History only:** the baseline retains conversation history but cannot modify tools or orchestration.
3. **Tool accumulation:** successful tools persist, but the orchestrator cannot modify itself.
4. **Recursive orchestration:** prompts, tools, strategies, and orchestration may evolve below the verifier boundary.
5. **Stronger static model:** a stronger model receives the same total compute without recursive modification.
6. **Best-of-N sampling:** many independent candidate solutions are generated and the same verifier selects the best.

All conditions should receive comparable token, time, and tool-execution budgets. Without these controls, gains from recursive adaptation can be confused with gains from simply spending more compute.

## Measurements

Track more than final success. Useful measurements include:

- Training-instance score by generation.
- Hidden-instance score by generation.
- Generalization gap.
- Compute spent per accepted improvement.
- Fraction of proposed mutations accepted.
- Rate of regressions.
- Time between meaningful improvements.
- Tool reuse across unrelated instances.
- Diversity among successful lineages.
- Frequency and type of verifier exploitation attempts.
- Complexity growth in prompts, tools, and orchestration.
- Performance after removing individual accumulated components.

Ablation testing is especially important. If removing an evolved component does not reduce performance, that component may be incidental complexity rather than a genuine improvement.

## Single lineage versus population search

A single recursive lineage is inexpensive and easy to interpret, but it can become trapped by early decisions. A population or archive preserves multiple competing approaches and may better expose path dependence.

Useful conditions include:

- **Greedy lineage:** retain only the highest-scoring child.
- **Diverse archive:** retain several behaviorally different successful variants.
- **Periodic restart:** return to the weak baseline while preserving only validated reusable discoveries.
- **Cross-lineage recombination:** combine independently successful tools or strategies, followed by full re-verification.

Comparing these strategies can reveal whether improvement depends on cumulative refinement or on maintaining diversity.

## Preventing overfitting and evaluator gaming

The evaluation design should include:

- Hidden instances that are regenerated or rotated.
- Multiple semantically equivalent formulations of the same task.
- Metamorphic tests that transform inputs while preserving expected relationships.
- Adversarial cases designed to expose shortcuts.
- Separate development and final evaluation suites.
- Periodic evaluation by older verifier versions.
- Mutation testing of the evaluator itself.
- Strict separation between editable artifacts and evaluation infrastructure.

No finite test suite proves universal correctness. The goal is to make exploitation harder than genuine improvement and to detect when measured progress fails to generalize.

## Possible outcomes and interpretations

### General capability improves

If hidden-instance performance rises while the generalization gap remains stable, the system may be accumulating reusable problem-solving machinery.

### Training performance improves but hidden performance does not

This suggests overfitting, evaluator exploitation, or excessive specialization to visible instances.

### Tools improve reliability but not reasoning

The system may become better at search, testing, editing, and error recovery without improving its ability to form difficult abstractions. This is still a meaningful engineering result.

### Memory or decomposition explains most gains

If simpler controls with persistent memory or better decomposition match the recursive system, self-modification may not be the essential mechanism.

### Improvement stalls immediately

The baseline may be below the capability threshold needed to produce useful mutations, or the feedback signal may be too sparse. A shaped curriculum or stronger proposal model may be necessary.

### Performance oscillates or collapses

This would indicate poor retention, inadequate regression testing, excessive mutation scope, or instability from modifying too many interacting components at once.

### Multiple lineages discover different successful machinery

This would provide evidence of meaningful path dependence and support population-based search rather than a single greedy lineage.

## Recommended first experiment

1. Select a problem family with automatically checkable outcomes and several hidden variants.
2. Begin with a baseline that solves some easy instances but fails most hard variants.
3. Allow modification of prompts, strategies, context management, and non-privileged tools.
4. Keep the verifier, hidden tasks, promotion mechanism, and accounting immutable.
5. Use external multi-dimensional scoring rather than a single candidate-reported success flag.
6. Run independent-attempt, history-only, best-of-N, tool-accumulation, and recursive conditions under matched budgets.
7. Store every proposal, score, accepted artifact, source digest, and parent relationship.
8. Evaluate accepted generations on fresh hidden variants.
9. Run ablations to identify which accumulated changes caused improvement.
10. Stop at predetermined compute and wall-clock limits rather than after a desired result appears.

## Conclusion

A weak system recursively attacking a hard problem is a worthwhile research experiment. The most valuable output may not be the final answer; it may be the lineage showing which adaptations were attempted, retained, combined, exploited, or discarded.

The experiment should be described as **cumulative problem-solving adaptation**, not as proof of unrestricted recursive self-improvement. Its scientific value depends on fixed external evaluation, hidden related instances, compute-matched controls, immutable lineage records, and a clear trust boundary that the evolving system cannot redefine.
