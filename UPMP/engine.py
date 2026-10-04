"""Numeric monitoring/report component for the UPMP process-integrity core.

This engine emits heuristic scores and recovery recommendations. The typed
verification and closed-loop orchestration APIs live alongside it in this
package; this numeric engine does not authorize or execute external actions.
See ARCHITECTURE.md for the shared contract and adapter boundary.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Union

from .enums import AlertPriority, DriftType, Severity, TrajectoryStatus, HealthStatus
from .layers import (
    layer1_intent_preservation,
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
from .models import (
    Checkpoint,
    DesiredState,
    DriftReport,
    Intent,
    MonitorReport,
    State,
    Intervention,
)
from .stages import assess_stage
from .extended import (
    adaptive_weights,
    AlertSystem,
    AnomalyDetector,
    CheckpointManager,
    compute_confidence_intervals,
    DEFAULT_DEPENDENCY_GRAPH,
    DependencyGraph,
    HistoricalReplay,
    MilestoneTracker,
    SelfTuner,
    TimeSeriesAnalyzer,
    WeightProfile,
)

# Configure logging for architectural reliability
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UPMP.Engine")

class MonitoringEngine:
    """
    The MonitoringEngine is the core monitoring component of UPMP.

    It solves the "Execution Blindness" problem by enforcing a strict
    monitoring cycle during every evaluation:

    1. DETERMINATION: Mapping raw telemetry to a normalized vector space.
    2. VERIFICATION: Auditing trajectory and intent to detect drift.
    3. INSURANCE: Projecting risks and providing remediation/rollback.
    """

    def __init__(
        self,
        intent: Intent,
        desired: Optional[DesiredState] = None,
        initial_state: Optional[State] = None,
        original_context: Optional[List[str]] = None,
        dependency_graph: Optional[DependencyGraph] = None,
    ):
        # Pillar 0: Setup & Configuration Integrity
        if not isinstance(intent, Intent):
            raise ValueError("Invalid Intent: A process must have a defined charter (Intent object).")

        self.intent = intent
        self.desired = desired or DesiredState()
        self.current = initial_state or State()
        self.prev_state: Optional[State] = None
        self.original_context = original_context or intent.success_criteria or [intent.description]

        # Historical State Tracking (Reliability)
        self.state_history: List[State] = [self.current]
        self.report_history: List[MonitorReport] = []
        self.progress_history: List[float] = []
        self.velocity_history: List[float] = []
        self.step_count: int = 0

        # Sub-Systems (Pillar Logic)
        self.time_series = TimeSeriesAnalyzer(window=5)
        self.anomaly_detector = AnomalyDetector()
        self.alert_system = AlertSystem()
        self.milestone_tracker = MilestoneTracker()
        self.checkpoint_manager = CheckpointManager()
        self.historical_replay = HistoricalReplay()
        self.self_tuner = SelfTuner()
        self.dependency_graph = dependency_graph or DEFAULT_DEPENDENCY_GRAPH

        self._weight_profile: Optional[WeightProfile] = None
        self._retained_context: List[str] = []

        # Default Escalation Rules (Insurance)
        self.alert_system.add_escalation_rule("drift", 3, AlertPriority.ERROR)
        self.alert_system.add_escalation_rule("regression", 2, AlertPriority.CRITICAL)

    @property
    def is_healthy(self) -> bool:
        """Easy Access: Returns True if the last verification was Healthy or better."""
        if not self.report_history: return True
        return self.report_history[-1].state_health >= 0.65

    @property
    def is_aligned(self) -> bool:
        """Easy Access: Returns True if the process trajectory is currently ALIGNED."""
        if not self.report_history: return True
        return self.report_history[-1].trajectory_status == TrajectoryStatus.ALIGNED

    def evaluate(self, current_behavior_desc: str = "",
                 retained_context: Optional[List[str]] = None) -> MonitorReport:
        """
        Executes the full 15-layer integrity cycle.

        This method DETERMINES the state, VERIFIES the alignment,
        and INSURES the goal through automated intervention logic.
        """
        self.step_count += 1
        t = self.step_count

        try:
            # --- PILLAR 1: DETERMINATION (Where are we?) ---
            self.time_series.record(t, self.current)
            self.anomaly_detector.record(self.current)

            progress = layer5_progress_score(self.current, self.desired)
            self.progress_history.append(progress)

            velocity = layer9_velocity(self.current, self.prev_state, 1.0) if self.prev_state else 0.0
            self.velocity_history.append(velocity)
            acceleration = layer10_acceleration(self.velocity_history[-1], self.velocity_history[-2], 1.0) if len(self.velocity_history) >= 2 else 0.0

            # --- PILLAR 2: VERIFICATION (Are we drifting?) ---
            # Directional verification
            traj_quality, traj_status = layer6_trajectory_quality(self.prev_state, self.current, self.desired)

            # Intent verification
            intent_score = layer1_intent_preservation(self.intent, current_behavior_desc)

            # Scope verification
            ctx = retained_context or self._retained_context or self.original_context
            context_integrity = layer8_context_integrity(self.original_context, ctx)

            # Vitals assessment
            stage_assessment = assess_stage(progress, 0.5, intent_score, 0.5)
            weights = adaptive_weights(stage_assessment.current_stage, self._weight_profile).to_dict()
            quality = layer14_quality_assessment(self.current, self.prev_state, intent_score, self.progress_history)
            health = layer11_state_health(progress, traj_quality, context_integrity, intent_score, quality.composite, weights)

            # Drift detection
            drifts = layer12_drift_detection(self.intent, intent_score, context_integrity, self.current, self.prev_state, self.self_tuner.get_thresholds())

            # --- PILLAR 3: INSURANCE (How do we succeed?) ---
            # Risk forecasting
            projections = layer13_future_projection(self.current, self.desired, velocity, acceleration, progress)

            # Remediations
            regressions = self.anomaly_detector.detect_regressions(self.current)
            existing_regressions = {drift.source for drift in drifts if drift.drift_type.value == "capability"}
            for dimension in regressions:
                if dimension not in existing_regressions:
                    previous_peak = max(self.anomaly_detector.history.get(dimension, [self.current.all_dims().get(dimension, 0.0)]))
                    current_value = self.current.all_dims().get(dimension, 0.0)
                    drifts.append(DriftReport(
                        drift_type=DriftType.CAPABILITY, severity=Severity.HIGH,
                        score=max(0.0, previous_peak - current_value), source=dimension,
                        description="Sustained regression detected in '%s'" % dimension,
                    ))
            drift_score = max((drift.score for drift in drifts), default=0.0)
            alerts = []
            for drift in drifts:
                priority = {
                    Severity.CRITICAL: AlertPriority.CRITICAL,
                    Severity.HIGH: AlertPriority.ERROR,
                    Severity.MEDIUM: AlertPriority.WARNING,
                    Severity.LOW: AlertPriority.INFO,
                    Severity.NONE: AlertPriority.INFO,
                }[drift.severity]
                alerts.append(self.alert_system.raise_alert(
                    priority, "drift", drift.description,
                    {"drift_type": drift.drift_type.value, "score": drift.score, "source": drift.source},
                ))
            interventions = layer15_intervention(
                drifts, health, velocity, intent_score, context_integrity,
                quality, regressions, progress_score=progress,
            )

            # Integrity updates
            anomaly_flags = self.anomaly_detector.detect_anomalies(self.current)
            ci = compute_confidence_intervals(self.anomaly_detector.history)
            convergence = self.time_series.overall_convergence()
            trends = self.time_series.all_trends()
            self.milestone_tracker.check_progress(self.current)

            # Issue verification report
            report = MonitorReport(
                intent_score=intent_score,
                progress_score=progress,
                state_health=health,
                trajectory_quality=traj_quality,
                trajectory_status=traj_status,
                context_integrity=context_integrity,
                drift_score=drift_score,
                drifts=drifts,
                velocity=velocity,
                acceleration=acceleration,
                quality=quality,
                interventions=interventions,
                projected_states=projections,
                current_state=copy.deepcopy(self.current),
                desired_state=copy.deepcopy(self.desired),
                confidence_intervals=ci,
                convergence=convergence,
                trends=trends,
                milestones_status=self.milestone_tracker.status(),
                anomaly_flags=anomaly_flags,
                alerts=alerts,
                regressions=regressions,
                evaluation_count=self.step_count,
            )

            # Auto-Archive
            self.report_history.append(report)
            self.historical_replay.record_frame(report, self.step_count)
            self.prev_state = copy.deepcopy(self.current)

            # Self-Tuning
            if len(self.report_history) % 5 == 0:
                self.self_tuner.auto_tune(self.report_history)

            return report

        except Exception as e:
            logger.error(f"Process Verification FAILED at Step {t}: {str(e)}", exc_info=True)
            return self._generate_error_report(t, str(e))

    def _generate_error_report(self, step: int, error_msg: str) -> MonitorReport:
        """Reliability: Generates a fail-safe report when evaluation fails."""
        return MonitorReport(
            evaluation_count=step,
            state_health=0.0,
            trajectory_status=TrajectoryStatus.STATIONARY,
            interventions=[Intervention(
                issue="Internal verification fault",
                action="Emergency process audit required",
                priority=Severity.CRITICAL,
                reasoning=error_msg
            )]
        )

    def set_current_state(self, state: State):
        """Easy Access: Updates current process coordinates."""
        if not isinstance(state, State):
            raise TypeError("State must be an instance of upmp.State")
        self.current = state

    def set_original_context(self, context: List[str]) -> None:
        """Set the stable context scope used by subsequent verification."""
        if not isinstance(context, list) or any(not isinstance(item, str) for item in context):
            raise TypeError("context must be a list of strings")
        self.original_context = list(context)

    def set_desired_state(self, desired: DesiredState):
        """Easy Access: Sets the target coordinate (the goal)."""
        self.desired = desired

    def checkpoint(self, label: str = "") -> Checkpoint:
        """Insurance: Creates a restorable safety snapshot."""
        return self.checkpoint_manager.save(label or f"safe-point-{self.step_count}", self.current, self.desired, self.intent)

    def rollback(self, checkpoint_id: Optional[str] = None) -> bool:
        """Restore in-memory monitoring state from a named or latest snapshot."""
        cp = (self.checkpoint_manager.restore(checkpoint_id)
              if checkpoint_id else self.checkpoint_manager.restore_last())
        if cp:
            self.current = cp.current_state or self.current
            logger.warning(f"CRITICAL REMEDIATION: Rollback triggered to {cp.label}")
            return True
        return False

def quick_monitor(intent_desc: str, current_vals: Dict[str, float], desired_vals: Dict[str, float],
                  success_criteria: Optional[List[str]] = None) -> MonitorReport:
    """Easy Access: Rapid process determination for one-off checks."""
    intent = Intent(description=intent_desc, success_criteria=success_criteria or [])

    def _map(cls, vals):
        obj = cls(**{k: v for k, v in vals.items() if hasattr(cls(), k)})
        obj.custom = {k: v for k, v in vals.items() if not hasattr(cls(), k)}
        return obj

    engine = MonitoringEngine(intent=intent, desired=_map(DesiredState, desired_vals), initial_state=_map(State, current_vals))
    return engine.evaluate()

