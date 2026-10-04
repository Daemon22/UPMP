"""
UPMP Core Data Models — The foundation for process state and intent.

These models are designed for reliability (data clamping) and easy access
(dictionary conversion, vectorization, and summary generation).
"""

from __future__ import annotations

import uuid
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from .enums import (
    ConvergenceStatus,
    DriftType,
    HealthStatus,
    Severity,
    TrendDirection,
    TrajectoryStatus,
)

VERSION = "2.3.0"

# ---------------------------------------------------------------------------
#  Layer 1: Intent Model (The Charter)
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    """
    Captures the original process charter and success criteria.
    Used to verify that execution remains aligned with the 'Why'.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    description: str = ""
    priority: float = 1.0
    success_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
#  Layer 2: State Representation (The Reality)
# ---------------------------------------------------------------------------

@dataclass
class State:
    """
    A multi-dimensional coordinate in the process state-space.
    Reliability: Clamps all inputs to [0.0, 1.0] to insure mathematical stability.
    """
    knowledge: float = 0.0
    reasoning: float = 0.0
    memory: float = 0.0
    performance: float = 0.0
    confidence: float = 0.0
    adaptability: float = 0.0
    creativity: float = 0.0
    efficiency: float = 0.0
    custom: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        """Insures data integrity by normalizing all scalars."""
        fields = ["knowledge", "reasoning", "memory", "performance",
                  "confidence", "adaptability", "creativity", "efficiency"]
        for f in fields:
            try:
                val = float(getattr(self, f))
                setattr(self, f, max(0.0, min(1.0, val)))
            except (ValueError, TypeError):
                setattr(self, f, 0.0)

        # Reliability: Ensure custom dimensions are also valid scalars
        sanitized_custom = {}
        if isinstance(self.custom, dict):
            for k, v in self.custom.items():
                try:
                    sanitized_custom[k] = max(0.0, min(1.0, float(v)))
                except (ValueError, TypeError):
                    sanitized_custom[k] = 0.0
        self.custom = sanitized_custom

    def all_dims(self) -> Dict[str, float]:
        """Provides easy access to all process dimensions."""
        d = {
            "knowledge": self.knowledge, "reasoning": self.reasoning,
            "memory": self.memory, "performance": self.performance,
            "confidence": self.confidence, "adaptability": self.adaptability,
            "creativity": self.creativity, "efficiency": self.efficiency,
        }
        d.update(self.custom)
        return d

    def vector(self, dim_order: Optional[List[str]] = None) -> List[float]:
        """Converts state to a numerical vector for mathematical analysis."""
        d = self.all_dims()
        # Ensure deterministic vector order
        keys = dim_order or sorted(list(d.keys()))
        return [d.get(k, 0.0) for k in keys]


@dataclass
class DesiredState(State):
    """The target coordinate representing goal realization."""
    pass


# ---------------------------------------------------------------------------
#  Report & Verification Models
# ---------------------------------------------------------------------------

@dataclass
class DriftReport:
    """Verifies specific deviations from the plan."""
    drift_type: DriftType = DriftType.GOAL
    severity: Severity = Severity.NONE
    score: float = 0.0
    source: str = ""
    description: str = ""


@dataclass
class QualityReport:
    """Assesses process reliability (Robustness, Consistency)."""
    correctness: float = 0.0
    robustness: float = 0.0
    consistency: float = 0.0
    explainability: float = 0.0

    @property
    def composite(self) -> float:
        return (self.correctness + self.robustness + self.consistency + self.explainability) / 4.0


@dataclass
class Intervention:
    """An insurance action triggered to remediate detected failure."""
    issue: str = ""
    action: str = ""
    priority: Severity = Severity.MEDIUM
    reasoning: str = ""


@dataclass
class ProjectedState:
    """A predictive forecast of where the process will be in the future."""
    horizon: str = ""
    state: Optional[State] = None
    goal_completion: float = 0.0
    risks: List[str] = field(default_factory=list)


@dataclass
class ConfidenceInterval:
    value: float = 0.0
    lower: float = 0.0
    upper: float = 0.0
    confidence_level: float = 0.95


@dataclass
class Alert:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    priority: Any = None # Expected AlertPriority enum
    category: str = ""
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    dismissed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorReport:
    """
    The unified process integrity audit report.
    Answers: Where are we? (Determine), Are we on track? (Verify), and What's next? (Insure).
    """
    # Determination Metrics
    intent_score: float = 0.0
    progress_score: float = 0.0
    state_health: float = 0.0

    # Verification Metrics
    trajectory_quality: float = 0.0
    trajectory_status: TrajectoryStatus = TrajectoryStatus.STATIONARY
    context_integrity: float = 1.0
    drift_score: float = 0.0
    drifts: List[DriftReport] = field(default_factory=list)

    # Insurance Metrics
    velocity: float = 0.0
    acceleration: float = 0.0
    quality: QualityReport = field(default_factory=QualityReport)
    interventions: List[Intervention] = field(default_factory=list)
    projected_states: List[ProjectedState] = field(default_factory=list)

    # State Snapshots
    current_state: Optional[State] = None
    desired_state: Optional[DesiredState] = None

    # Extended Verifications
    regressions: List[str] = field(default_factory=list)
    convergence: ConvergenceStatus = ConvergenceStatus.UNKNOWN
    trends: Dict[str, TrendDirection] = field(default_factory=dict)
    milestones_status: List[Milestone] = field(default_factory=list)
    alerts: List[Alert] = field(default_factory=list)
    anomaly_flags: Dict[str, bool] = field(default_factory=dict)
    confidence_intervals: Dict[str, ConfidenceInterval] = field(default_factory=dict)

    # Meta
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evaluation_count: int = 0

    @property
    def health_status(self) -> HealthStatus:
        if self.state_health >= 0.85: return HealthStatus.THRIVING
        if self.state_health >= 0.65: return HealthStatus.HEALTHY
        if self.state_health >= 0.45: return HealthStatus.CAUTION
        if self.state_health >= 0.25: return HealthStatus.CRITICAL
        return HealthStatus.FAILING

    @property
    def is_healthy(self) -> bool:
        """Easy Use: Rapid verification of system vitals."""
        return self.state_health >= 0.65

    @property
    def is_aligned(self) -> bool:
        """Easy Access: Rapid verification of goal alignment."""
        return self.trajectory_status == TrajectoryStatus.ALIGNED

    @property
    def is_on_track(self) -> bool:
        """Reliability: Unified verification check."""
        return (self.is_healthy and self.is_aligned and self.intent_score > 0.7)

    @property
    def is_divergent(self) -> bool:
        """Verification: Explicit drift check."""
        return self.trajectory_status == TrajectoryStatus.DIVERGENT or self.drift_score > 0.5

    def to_dict(self) -> Dict[str, Any]:
        """Easy Use: Recursive dictionary conversion for API integration."""
        def _ser(obj):
            from enum import Enum
            if isinstance(obj, Enum): return obj.value
            if isinstance(obj, (list, tuple)): return [_ser(x) for x in obj]
            if isinstance(obj, dict): return {k: _ser(v) for k, v in obj.items()}
            if hasattr(obj, "__dataclass_fields__"): return _ser(asdict(obj))
            return obj
        return _ser(self)

    def to_json(self) -> str:
        """Easy Use: Direct JSON export."""
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        """Easy Use: Human-readable integrity summary."""
        lines = [
            f"--- UPMP Integrity Report [Step {self.evaluation_count}] ---",
            f"  Status             : {'ON TRACK' if self.is_on_track else 'DEVIATION DETECTED'}",
            f"  Overall Health     : {self.state_health:.2%} ({self.health_status.value})",
            f"  Progress Completion : {self.progress_score:.2%}",
            f"  Trajectory Alignment: {self.trajectory_status.value} (Quality: {self.trajectory_quality:.2%})",
            f"  Intent Preservation : {self.intent_score:.2%}",
            f"  Drift / Deviations  : {len(self.drifts)} detected",
        ]
        if self.interventions:
            lines.append("  REMEDIATION REQUIRED:")
            for iv in self.interventions:
                lines.append(f"    -> [{iv.priority.value}] {iv.issue}: {iv.action}")
        return "\n".join(lines)


@dataclass
class Milestone:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""
    target_dims: Dict[str, float] = field(default_factory=dict)
    achieved: bool = False
    achieved_at: Optional[str] = None
    progress: float = 0.0


@dataclass
class Checkpoint:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    current_state: Optional[State] = None
    desired_state: Optional[DesiredState] = None
    intent: Optional[Intent] = None
    snapshot: Dict[str, Any] = field(default_factory=dict)

