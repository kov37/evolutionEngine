"""compile_context(): the single entry point agent.py calls every turn to
build the actual message list sent to the model. Dispatches to the named
policy so a run's recorded memory_policy (memory/store.py's RunStore)
always matches what actually built its prompts — not just a label attached
after the fact.
"""

from context.policies import POLICIES


def compile_context(memory_policy: str, messages: list, run_dir: str = None) -> list:
    if memory_policy not in POLICIES:
        raise ValueError(f"unknown memory_policy '{memory_policy}' — known policies: {list(POLICIES)}")
    system_and_task = messages[:2]
    tail = messages[2:]
    return POLICIES[memory_policy](system_and_task, tail, run_dir=run_dir)
