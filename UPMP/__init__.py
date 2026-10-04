"""
UPMP — Universal Progress Monitoring Package
==============================================
A domain-independent architectural framework for monitoring, verifying, and
insuring the integrity of any complex process.

UPMP's four-pillar architecture contract is Determine-Verify-Insure-Orchestrate.
This package provides typed observation and verification contracts, monitoring
reports, and a bounded closed-loop orchestration runtime. Scores and
recommendations are heuristics, not safety proofs.

  1. Determine: Represent observations and derived process measures.
  2. Verify: Compare observations with intent, desired outcomes and constraints.
  3. Insure: Assess risk and recommend containment or recovery.
  4. Orchestrate: Coordinate authorized work and feed observed results back.

Core 15 Layers:
    1. Intent Model (Charter)
    2. State Representation (Telemetry)
    3. Desired State (Goal)
    4. Goal Distance (Separation)
    5. Progress Score (Achievement)
    6. Trajectory Quality (Alignment)
    7. Intent Preservation (Consistency)
    8. Context Integrity (Scope)
    9. Growth Velocity (Speed)
   10. Acceleration (Momentum)
   11. State Health (Vitals)
   12. Drift Detection (Deviation)
   13. Future Projection (Forecasting)
   14. Quality Assessment (Reliability)
    15. Intervention Engine (Remediation recommendation)

Usage:
    from UPMP import MonitoringEngine, Intent, State, DesiredState, quick_monitor

    # Quick Determination
    report = quick_monitor(
        intent_desc="System Optimization",
        current_vals={"performance": 0.5, "efficiency": 0.4},
        desired_vals={"performance": 0.9, "efficiency": 0.9}
    )
    print(report.summary())
"""

__version__ = "2.3.0"
__author__ = "UPMP Framework"
__license__ = "MIT"

from .enums import (
    TrajectoryStatus,
    HealthStatus,
    DriftType,
    Severity,
    AlertPriority,
    ConvergenceStatus,
    TrendDirection,
)

from .models import (
    Intent,
    State,
    DesiredState,
    DriftReport,
    QualityReport,
    Intervention,
    ConfidenceInterval,
    ProjectedState,
    Alert,
    Milestone,
    Checkpoint,
    MonitorReport,
)

from .stages import (
    STAGE_DEFINITIONS,
    StageAssessment,
    assess_stage,
)

from .layers import (
    layer1_intent_preservation,
    layer4_goal_distance,
    layer4_goal_distance_normalized,
    layer5_progress_score,
    layer6_trajectory_quality,
    layer8_context_integrity,
    layer9_velocity,
    layer10_acceleration,
    layer11_state_health,
    layer12_drift_detection,
    layer13_future_projection,
    layer14_quality_assessment,
    layer15_intervention,
)

from .extended import (
    WeightProfile,
    STAGE_WEIGHTS,
    adaptive_weights,
    DependencyGraph,
    DEFAULT_DEPENDENCY_GRAPH,
    TimeSeriesAnalyzer,
    AnomalyDetector,
    compute_confidence_intervals,
    AlertSystem,
    MilestoneTracker,
    CheckpointManager,
    HistoricalReplay,
    SelfTuner,
    MultiAgentMonitor,
    AgentProfile,
    Exporter,
)

from .engine import (
    MonitoringEngine,
    quick_monitor,
)

from .contracts import (
    DataQuality, FindingStatus, Observation, ProcessState, FieldSchema,
    StateSchema, SchemaValidationResult, SchemaRegistry, Constraint,
    VerificationFinding, VerificationResult, VerificationPolicy,
)
from .closed_loop import (
    ActionStatus, RunStatus, ActionRequest, ActionResult, WorkflowStep,
    AuditEvent, WorkflowResult, ActionDispatcher, ClosedLoopOrchestrator,
    AuthorizationCheck,
)

__all__ = [
    # Version
    "__version__",
    # Enums
    "TrajectoryStatus", "HealthStatus", "DriftType", "Severity",
    "AlertPriority", "ConvergenceStatus", "TrendDirection",
    # Core models
    "Intent", "State", "DesiredState",
    # Report models
    "DriftReport", "QualityReport", "Intervention", "ConfidenceInterval",
    "ProjectedState", "Alert", "Milestone", "Checkpoint", "MonitorReport",
    # Stage model
    "STAGE_DEFINITIONS", "StageAssessment", "assess_stage",
    # Core layers
    "layer1_intent_preservation", "layer4_goal_distance",
    "layer4_goal_distance_normalized", "layer5_progress_score",
    "layer6_trajectory_quality", "layer8_context_integrity",
    "layer9_velocity", "layer10_acceleration", "layer11_state_health",
    "layer12_drift_detection", "layer13_future_projection",
    "layer14_quality_assessment", "layer15_intervention",
    # Extended features
    "WeightProfile", "STAGE_WEIGHTS", "adaptive_weights",
    "DependencyGraph", "DEFAULT_DEPENDENCY_GRAPH",
    "TimeSeriesAnalyzer", "AnomalyDetector", "compute_confidence_intervals",
    "AlertSystem", "MilestoneTracker", "CheckpointManager",
    "HistoricalReplay", "SelfTuner",
    "MultiAgentMonitor", "AgentProfile",
    "Exporter", "quick_monitor",
    # Engine
    "MonitoringEngine",
    # Domain-neutral contracts and closed-loop workflow runtime
    "DataQuality", "FindingStatus", "Observation", "ProcessState",
    "FieldSchema", "StateSchema", "SchemaValidationResult", "SchemaRegistry",
    "Constraint", "VerificationFinding", "VerificationResult",
    "VerificationPolicy", "ActionStatus", "RunStatus", "ActionRequest",
    "ActionResult", "WorkflowStep", "AuditEvent", "WorkflowResult",
    "ActionDispatcher", "ClosedLoopOrchestrator", "AuthorizationCheck",
]

