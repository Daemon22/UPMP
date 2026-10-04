"""
UPMP 15 Core Computation Layers — The Mathematical Foundation.

These layers provide the objective logic to Determine, Verify, and Insure
process integrity. Each layer is domain-agnostic and numerically stable.
"""

from __future__ import annotations

import math
import statistics
import logging
import re
from typing import Dict, List, Optional, Tuple

from .enums import (
    DriftType,
    Severity,
    TrajectoryStatus,
)
from .models import (
    DesiredState,
    DriftReport,
    Intervention,
    Intent,
    ProjectedState,
    QualityReport,
    State,
)

logger = logging.getLogger("UPMP.Layers")

def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    """Reliability: Prevents DivisionByZero and handles NaN/Inf scenarios."""
    try:
        if abs(d) < 1e-12:
            return default
        res = n / d
        if math.isnan(res) or math.isinf(res):
            return default
        return res
    except Exception:
        return default

# ---------------------------------------------------------------------------
#  Pillar 1: Determination (Reality Mapping)
# ---------------------------------------------------------------------------

def layer4_goal_distance(current: State, desired: DesiredState) -> float:
    """Determines absolute Euclidean separation between current and target vectors."""
    dims = list(current.all_dims().keys())
    s = current.vector(dims)
    g = desired.vector(dims)
    return math.sqrt(sum((si - gi) ** 2 for si, gi in zip(s, g)))


def layer4_goal_distance_normalized(current: State, desired: DesiredState) -> float:
    """Normalized separation score [0.0 - 1.0]."""
    dims = list(current.all_dims().keys())
    max_dist = math.sqrt(len(dims))
    dist = layer4_goal_distance(current, desired)
    return _safe_div(dist, max_dist)


def layer5_progress_score(current: State, desired: DesiredState) -> float:
    """Determines % completion as a weighted achievement ratio.

    Reliability: Only considers dimensions with a non-zero target in DesiredState.
    """
    c = current.all_dims()
    d = desired.all_dims()
    ratios = []
    for k in set(list(c.keys()) + list(d.keys())):
        target = d.get(k, 0.0)
        if target < 1e-6:
            continue
        # Achievement is capped at 100% per dimension to prevent over-performance masking
        achieved = min(max(0.0, c.get(k, 0.0)), target)
        ratios.append(_safe_div(achieved, target))

    return statistics.mean(ratios) if ratios else 0.0


# ---------------------------------------------------------------------------
#  Pillar 2: Verification (Alignment & Drift Audit)
# ---------------------------------------------------------------------------

def layer1_intent_preservation(intent: Intent, current_behavior_desc: str = "") -> float:
    """Return lexical criterion coverage as a heuristic, not semantic proof."""
    if not intent.success_criteria:
        return 1.0
    if not current_behavior_desc:
        return 0.5 # Default to neutral if no telemetry provided

    def tokens(value: str) -> set:
        result = set()
        for word in re.findall(r"[a-z0-9%]+", value.lower()):
            if len(word) <= 2:
                continue
            if word.endswith("ies") and len(word) > 4:
                word = word[:-3] + "y"
            elif word.endswith("ing") and len(word) > 5:
                word = word[:-3]
                if word.endswith(("v", "t", "k", "s", "c", "g")):
                    word += "e"
            elif word.endswith("ed") and len(word) > 4:
                word = word[:-2]
            elif word.endswith("s") and len(word) > 3:
                word = word[:-1]
            result.add(word)
        return result

    observed = tokens(current_behavior_desc)
    coverage = []
    for criterion in intent.success_criteria:
        required = tokens(criterion)
        if required:
            coverage.append(_safe_div(len(required & observed), len(required)))

    return statistics.mean(coverage) if coverage else 1.0


def layer6_trajectory_quality(
    prev_state: Optional[State],
    current_state: State,
    desired_state: DesiredState,
) -> Tuple[float, TrajectoryStatus]:
    """Verifies directional alignment using Cosine Similarity.

    Insures that the process is moving TOWARDS the goal, not just moving.
    """
    if prev_state is None:
        return 1.0, TrajectoryStatus.STATIONARY

    dims = list(current_state.all_dims().keys())
    actual_delta = [current_state.all_dims().get(k, 0) - prev_state.all_dims().get(k, 0) for k in dims]
    desired_dir = [desired_state.all_dims().get(k, 0) - current_state.all_dims().get(k, 0) for k in dims]

    act_mag = math.sqrt(sum(x ** 2 for x in actual_delta))
    des_mag = math.sqrt(sum(x ** 2 for x in desired_dir))

    if act_mag < 1e-9 or des_mag < 1e-9:
        return 0.5, TrajectoryStatus.STATIONARY

    cosine = _safe_div(sum(a * d for a, d in zip(actual_delta, desired_dir)), (act_mag * des_mag))
    score = (cosine + 1.0) / 2.0 # Map [-1, 1] to [0, 1]

    if cosine > 0.866: # < 30 degrees (Strong Alignment)
        status = TrajectoryStatus.ALIGNED
    elif cosine > 0.0: # < 90 degrees (Partial Alignment)
        status = TrajectoryStatus.PARTIALLY_ALIGNED
    else:
        status = TrajectoryStatus.DIVERGENT

    return score, status


def layer8_context_integrity(
    original_context: List[str],
    retained_context: List[str],
) -> float:
    """Verifies scope integrity by checking environmental keyword retention."""
    if not original_context:
        return 1.0
    if not retained_context:
        return 0.0
    orig_set = set(w.lower() for w in original_context if len(w) > 2)
    ret_set = set(w.lower() for w in retained_context if len(w) > 2)
    return _safe_div(len(orig_set & ret_set), len(orig_set))


def layer12_drift_detection(
    intent: Intent,
    intent_score: float,
    context_integrity: float,
    current: State,
    prev: Optional[State],
    thresholds: Optional[Dict[str, float]] = None,
) -> List[DriftReport]:
    """Verification logic to detect unauthorized deviations (Drift)."""
    t = thresholds or {"intent": 0.7, "context": 0.7, "capability_drop": 0.15}
    drifts = []

    if intent_score < t["intent"]:
        drifts.append(DriftReport(
            drift_type=DriftType.GOAL,
            severity=Severity.HIGH if intent_score < 0.3 else Severity.MEDIUM,
            score=1.0 - intent_score,
            description=f"Goal Drift: Intent match dropped to {intent_score:.2%}"
        ))

    if context_integrity < t["context"]:
        drifts.append(DriftReport(
            drift_type=DriftType.CONTEXT,
            severity=Severity.MEDIUM,
            score=1.0 - context_integrity,
            description=f"Context Drift: Environmental integrity at {context_integrity:.2%}"
        ))

    if prev:
        for dim, c_val in current.all_dims().items():
            p_val = prev.all_dims().get(dim, c_val)
            if p_val - c_val > t["capability_drop"]:
                drifts.append(DriftReport(
                    drift_type=DriftType.CAPABILITY,
                    severity=Severity.HIGH,
                    score=p_val - c_val,
                    source=dim,
                    description=f"Regression: Capability '{dim}' tanked."
                ))
    return drifts


# ---------------------------------------------------------------------------
#  Pillar 3: Insurance (Risk Forecasting & Remediation)
# ---------------------------------------------------------------------------

def layer13_future_projection(
    current: State,
    desired: DesiredState,
    velocity: float,
    acceleration: float,
    progress: float,
) -> List[ProjectedState]:
    """Insures the goal by projecting current dynamics into the future."""
    projections = []
    for horizon, steps in [("Short (7)", 7), ("Medium (30)", 30), ("Long (90)", 90)]:
        # S = ut + 0.5at^2 (Kinematic displacement)
        future_progress = progress + (velocity * steps) + (0.5 * acceleration * (steps ** 2))
        future_progress = max(0.0, min(1.0, future_progress))

        proj_state = State()
        # Proportionally project all dimensions
        for dim, c_val in current.all_dims().items():
            d_val = desired.all_dims().get(dim, c_val)
            proj_val = c_val + ((d_val - c_val) * (future_progress - progress))
            setattr(proj_state, dim, max(0.0, min(1.0, proj_val)))

        projections.append(ProjectedState(
            horizon=horizon, state=proj_state, goal_completion=future_progress,
            risks=["Stagnation Risk"] if velocity < 0.001 else []
        ))
    return projections


def layer15_intervention(
    drifts: List[DriftReport],
    health: float,
    velocity: float,
    intent_score: float,
    context_integrity: float,
    quality: QualityReport,
    regressions: List[str],
    progress_score: float = 1.0,
) -> List[Intervention]:
    """Trigger insurance actions based on verification failures."""
    interventions = []

    if health < 0.3:
        interventions.append(Intervention("Health Collapse", "Critical Audit & Execution Freeze", Severity.CRITICAL))

    if intent_score < 0.5:
        interventions.append(Intervention("Intent Drift", "Re-synchronize Behavior with Success Criteria", Severity.HIGH))

    for d in drifts:
        if d.drift_type == DriftType.CAPABILITY:
            interventions.append(Intervention(f"Metric Regression: {d.source}", "Rollback to Checkpoint", Severity.HIGH))

    if velocity < 0.005 and progress_score < 0.95:
        interventions.append(Intervention("Stagnation", "Inject process variation or parameter shift", Severity.MEDIUM))

    return interventions


# ---------------------------------------------------------------------------
#  Lifecycle Components (Internal)
# ---------------------------------------------------------------------------

def layer9_velocity(current: State, prev: State, dt: float = 1.0) -> float:
    c = current.all_dims()
    p = prev.all_dims()
    delta = math.sqrt(sum((c.get(k, 0) - p.get(k, 0)) ** 2 for k in set(list(c.keys()) + list(p.keys()))))
    return _safe_div(delta, dt)

def layer10_acceleration(v1: float, v2: float, dt: float = 1.0) -> float:
    return _safe_div(v1 - v2, dt)

def layer11_state_health(p: float, t: float, c: float, i: float, q: float, w: Dict) -> float:
    total_w = sum(w.values())
    val = (w.get('progress', 0)*p + w.get('trajectory', 0)*t + w.get('context', 0)*c +
           w.get('intent', 0)*i + w.get('quality', 0)*q)
    return _safe_div(val, total_w)

def layer14_quality_assessment(c: State, p: Optional[State], i: float, hist: List[float]) -> QualityReport:
    robust = 1.0
    if p:
        d = [abs(c.all_dims().get(k, 0) - p.all_dims().get(k, 0)) for k in c.all_dims()]
        robust = max(0.0, 1.0 - (statistics.mean(d) * 2))
    consist = statistics.mean(hist) if hist else 0.5
    return QualityReport(correctness=i, robustness=robust, consistency=consist, explainability=0.8)

