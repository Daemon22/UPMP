"""UPMP Process Lifecycle Stage Model — Universal 10-Stage Framework.

While these stages are often applied to intelligence training, they represent
a generic progression from initialization to goal realization for any
complex process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


STAGE_DEFINITIONS: Dict[int, Tuple[str, str]] = {
    0: ("Initialization", "Process bootstrapped, no measurable progress yet"),
    1: ("Acquisition", "Early stage absorption of resources or basic patterns"),
    2: ("Pattern Formation", "Establishing stable internal structures or behaviors"),
    3: ("Operational Development", "Building functional and logical capabilities"),
    4: ("Generalization", "Process stability across varying conditions/domains"),
    5: ("Self-Evaluation", "Systemic assessment of performance and gaps"),
    6: ("Optimization", "Active refining and efficiency improvements"),
    7: ("Autonomous Operation", "Self-sustaining and self-correcting cycles"),
    8: ("Intent Mastery", "High alignment with original success criteria"),
    9: ("Goal Realization", "Process successfully achieved desired state"),
}


@dataclass
class StageAssessment:
    current_stage: int = 0
    stage_name: str = "Initialization"
    confidence: float = 0.0
    next_stage: int = 1
    next_stage_name: str = "Acquisition"
    distance_to_next: float = 1.0


def assess_stage(progress: float, health: float, intent: float, quality: float) -> StageAssessment:
    """Estimate the current process stage from composite metrics.

    The composite score is a weighted combination:
        progress (35%) + health (25%) + intent (25%) + quality (15%)

    Stage thresholds map composite ranges to discrete stages 0-9.
    """
    composite = (progress * 0.35 + health * 0.25 + intent * 0.25 + quality * 0.15)

    # Stage thresholds
    thresholds = [0.0, 0.05, 0.15, 0.25, 0.40, 0.55, 0.65, 0.75, 0.85, 0.95]
    stage = 0
    for i, t in enumerate(thresholds):
        if composite >= t:
            stage = i

    stage_name = STAGE_DEFINITIONS.get(stage, ("Unknown", ""))[0]
    next_stage = min(stage + 1, 9)
    next_name = STAGE_DEFINITIONS.get(next_stage, ("Unknown", ""))[0]

    if stage < 9:
        t_curr = thresholds[stage]
        t_next = thresholds[min(stage + 1, 9)]
        dist = (t_next - composite) / (t_next - t_curr) if t_next > t_curr else 0.0
        dist = max(0.0, min(1.0, dist))
    else:
        dist = 0.0

    conf = min(1.0, composite * 1.1) if stage > 0 else 0.5

    return StageAssessment(
        current_stage=stage,
        stage_name=stage_name,
        confidence=conf,
        next_stage=next_stage,
        next_stage_name=next_name,
        distance_to_next=dist,
    )

