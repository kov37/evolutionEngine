"""Subgoal memory chunks — written when a subgoal is marked complete
(controller/subgoals.py calls create_episode() on a successful
subgoal_complete). Each episode links a compact LLM summary back to the
exact raw event range it was generated from — "compact but reversible
through their artifact links" (the plan doc's Layer 3) — so
memory_expand can always pull the full detail back, and fidelity_ok
gives a best-effort cross-check that the summary isn't inventing paths
that were never touched.
"""

import json
import os

from memory.summaries import check_fidelity, generate_summary


def episodes_dir(run_dir: str) -> str:
    path = os.path.join(run_dir, "episodes")
    os.makedirs(path, exist_ok=True)
    return path


def create_episode(run_dir: str, subgoal_id: str, goal: str, success_condition: str, conclusion: str,
                    from_event_id: str, to_event_id: str, model: str, auto_closed: bool = False) -> dict:
    summary_text, raw_text = generate_summary(run_dir, from_event_id, to_event_id, goal, conclusion, model=model)
    episode = {
        "subgoal_id": subgoal_id, "goal": goal, "success_condition": success_condition,
        "conclusion": conclusion, "summary": summary_text, "fidelity_ok": check_fidelity(summary_text, raw_text),
        "from_event_id": from_event_id, "to_event_id": to_event_id, "auto_closed": auto_closed,
    }
    path = os.path.join(episodes_dir(run_dir), f"{subgoal_id}.json")
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(episode, f, indent=2)
    os.replace(tmp, path)
    return episode


def list_episodes(run_dir: str) -> list:
    directory = os.path.join(run_dir, "episodes")
    if not os.path.isdir(directory):
        return []
    episodes = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), "r", encoding="utf-8") as f:
                episodes.append(json.load(f))
    return episodes
