"""Policy Updater — applies hypotheses as experiments.

For each hypothesis:
1. Validate change_spec
2. Check safety boundaries
3. Create candidate policy (current + change)
4. Save as experiment in SQLite
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import structlog
import yaml

from aibp.self_learning.db import save_policy_version, sqlite_conn
from aibp.self_learning.safety import is_autopilot_paused
from aibp.utils.config import PROJECT_ROOT, load_policy

log = structlog.get_logger()

REPORTS_DIR = PROJECT_ROOT / "reports" / "self_learning"


def load_latest_hypotheses() -> list[dict]:
    """Load the latest hypotheses JSON file."""
    files = sorted(REPORTS_DIR.glob("hypotheses_*.json"), reverse=True)
    if not files:
        return []
    return json.loads(files[0].read_text(encoding="utf-8"))


def validate_change_spec(hypothesis: dict, current_policy: dict) -> tuple[bool, str]:
    """Validate that change_spec is safe and well-formed."""
    exp_type = hypothesis.get("experiment_type")
    spec = hypothesis.get("change_spec", {})

    if not exp_type or not spec:
        return False, "missing experiment_type or change_spec"

    if exp_type == "rubric_weight":
        rubric = spec.get("rubric")
        if rubric not in current_policy.get("rubric_weights", {}):
            return False, f"unknown rubric: {rubric}"
        weight = spec.get("new_weight")
        if not isinstance(weight, (int, float)) or weight < 0 or weight > 3:
            return False, f"weight out of range (0-3): {weight}"
        return True, "ok"

    if exp_type == "post_param":
        slot = spec.get("slot")
        if slot not in current_policy.get("post_params", {}):
            return False, f"unknown slot: {slot}"
        return True, "ok"

    if exp_type == "source_score":
        domain = spec.get("domain")
        if not domain:
            return False, "missing domain"
        score = spec.get("new_score")
        if not isinstance(score, (int, float)) or score < -1 or score > 1:
            return False, f"score out of range (-1 to 1): {score}"
        return True, "ok"

    if exp_type == "regex_gate":
        name = spec.get("name")
        pattern = spec.get("pattern")
        action = spec.get("action", "warn")
        if not name or not pattern:
            return False, "missing name or pattern"
        if action not in ("warn", "fail"):
            return False, f"invalid action: {action}"
        # Test regex compiles
        import re
        try:
            re.compile(pattern)
        except re.error as e:
            return False, f"invalid regex: {e}"
        return True, "ok"

    if exp_type == "visual":
        return True, "ok"

    return False, f"unknown experiment_type: {exp_type}"


def apply_change_to_policy(policy: dict, hypothesis: dict) -> dict:
    """Apply one hypothesis change to a copy of policy. Returns new policy."""
    new_policy = copy.deepcopy(policy)
    exp_type = hypothesis["experiment_type"]
    spec = hypothesis["change_spec"]

    if exp_type == "rubric_weight":
        new_policy["rubric_weights"][spec["rubric"]] = spec["new_weight"]

    elif exp_type == "post_param":
        slot = spec["slot"]
        param = spec["param"]
        new_policy["post_params"][slot][param] = spec["new_value"]

    elif exp_type == "source_score":
        new_policy["source_scores"][spec["domain"]] = spec["new_score"]

    elif exp_type == "regex_gate":
        new_policy["regex_gates"].append({
            "name": spec["name"],
            "pattern": spec["pattern"],
            "action": spec.get("action", "warn"),
            "slot": spec.get("slot", "all"),
        })

    elif exp_type == "visual":
        param = spec["param"]
        new_policy["visual_policy"][param] = spec["new_value"]

    else:
        raise ValueError(f"Unknown experiment_type: {exp_type}")

    # Bump version
    new_policy["version"] = f"v_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return new_policy


def create_experiment(hypothesis: dict, current_policy: dict) -> int | None:
    """Create one experiment from a hypothesis. Returns experiment ID."""
    # Validate
    valid, reason = validate_change_spec(hypothesis, current_policy)
    if not valid:
        log.warning("hypothesis_rejected", hypothesis=hypothesis, reason=reason)
        return None

    # Apply change
    new_policy = apply_change_to_policy(current_policy, hypothesis)

    # Save new policy version
    new_yaml = yaml.dump(new_policy, allow_unicode=True, default_flow_style=False, sort_keys=False)
    new_version = save_policy_version(
        policy_dict=new_policy,
        yaml_content=new_yaml,
        applies_to="stage",  # shadow test on stage first
        created_by="autopilot",
        parent_version=current_policy.get("version"),
    )

    # Create experiment record
    with sqlite_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO experiments_log
                (started_at, experiment_type, hypothesis, policy_before, policy_after,
                 applies_to, status)
            VALUES (?, ?, ?, ?, ?, 'stage', 'draft')
            """,
            (
                datetime.now(UTC).isoformat(),
                hypothesis["experiment_type"],
                hypothesis.get("hypothesis", ""),
                current_policy.get("version", "unknown"),
                new_version,
            ),
        )
        exp_id = cur.lastrowid

    log.info("experiment_created", id=exp_id, type=hypothesis["experiment_type"], new_policy=new_version)
    return exp_id


def run() -> int:
    """Main entry point — process all hypotheses into experiments."""
    if is_autopilot_paused():
        log.warning("autopilot_paused_skipping")
        return 0

    hypotheses = load_latest_hypotheses()
    if not hypotheses:
        log.info("no_hypotheses")
        return 0

    current_policy = load_policy()
    log.info("processing_hypotheses", count=len(hypotheses))

    created = 0
    for hyp in hypotheses:
        exp_id = create_experiment(hyp, current_policy)
        if exp_id is not None:
            created += 1

    log.info("experiments_created", created=created, total=len(hypotheses))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
