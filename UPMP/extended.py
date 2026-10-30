"""
UPMP Extended Features — Professional-grade monitoring extensions.

These 12 features provide the statistical depth and recovery infrastructure
required to verify and insure complex process lifecycles.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import statistics
import logging
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .enums import (
    AlertPriority,
    ConvergenceStatus,
    DriftType,
    Severity,
    TrendDirection,
    TrajectoryStatus,
)
from .models import (
    Alert,
    Checkpoint,
    ConfidenceInterval,
    DesiredState,
    DriftReport,
    Intervention,
    Intent,
    Milestone,
    MonitorReport,
    ProjectedState,
    QualityReport,
    State,
)
from .stages import assess_stage

logger = logging.getLogger("UPMP.Extended")

# ---------------------------------------------------------------------------
#  1. Adaptive Weighting Engine (Reliability)
# ---------------------------------------------------------------------------

@dataclass
class WeightProfile:
    """
    Insures that health metrics are interpreted correctly based on process maturity.
    """
    name: str = "default"
    progress: float = 0.25
    trajectory: float = 0.20
    context: float = 0.20
    intent: float = 0.20
    quality: float = 0.15

    def to_dict(self) -> Dict[str, float]:
        return {
            "progress": self.progress,
            "trajectory": self.trajectory,
            "context": self.context,
            "intent": self.intent,
            "quality": self.quality,
        }


STAGE_WEIGHTS = {
    0: WeightProfile("initialization", 0.10, 0.10, 0.30, 0.40, 0.10),
    1: WeightProfile("acquisition", 0.30, 0.10, 0.30, 0.20, 0.10),
    2: WeightProfile("formation", 0.25, 0.20, 0.20, 0.20, 0.15),
    3: WeightProfile("development", 0.20, 0.30, 0.15, 0.20, 0.15),
    4: WeightProfile("stability", 0.20, 0.25, 0.15, 0.25, 0.15),
    5: WeightProfile("evaluation", 0.15, 0.20, 0.15, 0.25, 0.25),
    6: WeightProfile("optimization", 0.15, 0.25, 0.15, 0.20, 0.25),
    7: WeightProfile("autonomous", 0.15, 0.30, 0.10, 0.20, 0.25),
    8: WeightProfile("mastery", 0.10, 0.20, 0.15, 0.40, 0.15),
    9: WeightProfile("realization", 0.10, 0.15, 0.10, 0.50, 0.15),
}


def adaptive_weights(stage: int, custom_override: Optional[WeightProfile] = None) -> WeightProfile:
    """Easy Access: Returns the optimized weight profile for the current process stage."""
    if custom_override:
        return custom_override
    return STAGE_WEIGHTS.get(stage, WeightProfile())


# ---------------------------------------------------------------------------
#  2. Capability Dependency Graph (Determination)
# ---------------------------------------------------------------------------

class DependencyGraph:
    """
    Determines how changes in one process dimension impact others.
    Essential for identifying bottlenecks and root causes of drift.
    """

    def __init__(self):
        self.dependencies: Dict[str, Set[str]] = defaultdict(set)
        self.strengths: Dict[Tuple[str, str], float] = {}

    def add_dependency(self, source: str, target: str, strength: float = 1.0):
        self.dependencies[target].add(source)
        self.strengths[(source, target)] = strength

    def get_prerequisites(self, capability: str) -> Set[str]:
        return set(self.dependencies.get(capability, set()))

    def get_dependents(self, capability: str) -> Set[str]:
        return {target for target, sources in self.dependencies.items() if capability in sources}

    def bottleneck_score(self, capability: str) -> float:
        return float(len(self.get_dependents(capability)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dependencies": {key: sorted(values) for key, values in self.dependencies.items()},
            "strengths": {"%s->%s" % key: value for key, value in self.strengths.items()},
        }

    def impact_analysis(self, capability: str, delta: float) -> Dict[str, float]:
        """Verifies downstream effects of a specific deviation."""
        impact = {}
        queue = [(capability, delta)]
        visited = set()
        while queue:
            cap, d = queue.pop(0)
            if cap in visited: continue
            visited.add(cap)
            for dep in {k for k, v in self.dependencies.items() if cap in v}:
                str_val = self.strengths.get((cap, dep), 0.5)
                propagated = d * str_val
                impact[dep] = impact.get(dep, 0.0) + propagated
                queue.append((dep, propagated))
        return impact

# Default graph mappings
DEFAULT_DEPENDENCY_GRAPH = DependencyGraph()
DEFAULT_DEPENDENCY_GRAPH.add_dependency("knowledge", "reasoning", 0.7)
DEFAULT_DEPENDENCY_GRAPH.add_dependency("performance", "efficiency", 0.7)


# ---------------------------------------------------------------------------
#  3. Time-Series Analysis (Verification)
# ---------------------------------------------------------------------------

class TimeSeriesAnalyzer:
    """
    Verifies process stability and detects oscillations or plateaus.
    """

    def __init__(self, window: int = 5):
        self.window = window
        self.history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)

    def record(self, t: float, state: State):
        for dim, val in state.all_dims().items():
            self.history[dim].append((t, val))

    def simple_moving_average(self, dim: str) -> Optional[float]:
        values = [value for _, value in self.history.get(dim, [])[-self.window:]]
        return statistics.mean(values) if values else None

    def exponential_moving_average(self, dim: str, alpha: float = 0.3) -> Optional[float]:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        values = [value for _, value in self.history.get(dim, [])]
        if not values:
            return None
        average = values[0]
        for value in values[1:]:
            average = alpha * value + (1.0 - alpha) * average
        return average

    def momentum(self, dim: str) -> Optional[float]:
        values = [value for _, value in self.history.get(dim, [])]
        return values[-1] - values[-2] if len(values) >= 2 else None

    def trend_direction(self, dim: str) -> TrendDirection:
        """Determines if a dimension is improving, declining, or volatile."""
        vals = [v for _, v in self.history.get(dim, [])[-self.window:]]
        if len(vals) < 3: return TrendDirection.STABLE

        # Simple slope determination
        slope = (vals[-1] - vals[0]) / len(vals)
        if abs(slope) < 0.005: return TrendDirection.STABLE
        return TrendDirection.IMPROVING if slope > 0 else TrendDirection.DECLINING

    def overall_convergence(self) -> ConvergenceStatus:
        """Aggregate per-dimension convergence without hiding oscillation."""
        statuses = [self.convergence_status(dim) for dim in self.history]
        statuses = [status for status in statuses if status != ConvergenceStatus.UNKNOWN]
        if not statuses:
            return ConvergenceStatus.UNKNOWN
        if ConvergenceStatus.OSCILLATING in statuses:
            return ConvergenceStatus.OSCILLATING
        counts: Dict[ConvergenceStatus, int] = defaultdict(int)
        for status in statuses:
            counts[status] += 1
        return max(counts, key=counts.get)

    def convergence_status(self, dim: str) -> ConvergenceStatus:
        # Keep enough history to observe multiple turns in a periodic process.
        values = [value for _, value in self.history.get(dim, [])[-max(self.window * 2, 30):]]
        if len(values) < 4:
            return ConvergenceStatus.UNKNOWN
        half = len(values) // 2
        first_var = statistics.variance(values[:half]) if half > 1 else 0.0
        second_var = statistics.variance(values[half:]) if len(values[half:]) > 1 else 0.0
        diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
        avg_diff = statistics.mean(abs(value) for value in diffs) if diffs else 0.0
        sign_changes = sum(1 for i in range(1, len(diffs)) if diffs[i] * diffs[i - 1] < 0)
        if sign_changes >= 3 and (max(values) - min(values)) >= 0.1:
            return ConvergenceStatus.OSCILLATING
        if first_var > second_var and avg_diff < 0.02:
            return ConvergenceStatus.CONVERGING
        if avg_diff > 0.05:
            return ConvergenceStatus.DIVERGING
        return ConvergenceStatus.STATIONARY

    def all_trends(self) -> Dict[str, TrendDirection]:
        return {dim: self.trend_direction(dim) for dim in self.history}


# ---------------------------------------------------------------------------
#  4. Anomaly & Regression Detection (Verification)
# ---------------------------------------------------------------------------

class AnomalyDetector:
    """
    Verifies that no sudden, unauthorized deviations occur.
    """

    def __init__(self, z_threshold: float = 2.5):
        self.z_threshold = z_threshold
        self.history: Dict[str, List[float]] = defaultdict(list)

    def record(self, state: State):
        for dim, val in state.all_dims().items():
            self.history[dim].append(val)

    def detect_anomalies(self, state: State) -> Dict[str, bool]:
        """Flag dimensions that show statistically improbable values."""
        flags = {}
        for dim, val in state.all_dims().items():
            vals = self.history.get(dim, [])
            if len(vals) < 5:
                flags[dim] = False
                continue

            mean = statistics.mean(vals[:-1])
            std = statistics.stdev(vals[:-1])
            if std < 1e-6:
                flags[dim] = abs(val - mean) > 0.2
            else:
                z = abs(val - mean) / std
                flags[dim] = z > self.z_threshold
        return flags

    def detect_regressions(self, state: State) -> List[str]:
        """Insures against capability loss by flagging drops from peak performance."""
        regressions = []
        for dim, val in state.all_dims().items():
            vals = self.history.get(dim, [])
            if len(vals) < 3: continue
            peak = max(vals)
            if peak > 0.1 and val < peak * 0.75: # 25% regression from peak
                regressions.append(dim)
        return regressions


# ---------------------------------------------------------------------------
#  5. Confidence Intervals (Insurance)
# ---------------------------------------------------------------------------

def compute_confidence_intervals(history: Dict[str, List[float]]) -> Dict[str, ConfidenceInterval]:
    """Provides statistical insurance on state accuracy."""
    intervals = {}
    for dim, vals in history.items():
        if len(vals) < 3: continue
        mean = statistics.mean(vals)
        std = statistics.stdev(vals)
        margin = 1.96 * (std / math.sqrt(len(vals))) # 95% confidence
        intervals[dim] = ConfidenceInterval(
            value=vals[-1], lower=max(0, mean - margin), upper=min(1, mean + margin)
        )
    return intervals


# ---------------------------------------------------------------------------
#  6-8. Alerts, Milestones, Checkpoints (Insurance & Recovery)
# ---------------------------------------------------------------------------

class AlertSystem:
    """Insures immediate visibility into critical drift events."""
    def __init__(self):
        self.alerts: List[Alert] = []
        self.escalation_rules: List[Dict[str, Any]] = []

    def add_escalation_rule(self, category: str, count: int, level: AlertPriority):
        if count < 1:
            raise ValueError("escalation count must be positive")
        self.escalation_rules.append({"category": category, "count": count, "level": level})

    def raise_alert(self, priority: AlertPriority, category: str, message: str, meta: Dict = None) -> Alert:
        effective = priority
        recent_count = sum(1 for alert in self.alerts[-10:] if alert.category == category and not alert.dismissed)
        for rule in self.escalation_rules:
            if rule["category"] == category and recent_count >= rule["count"]:
                if rule["level"].value > effective.value:
                    effective = rule["level"]
        alert = Alert(priority=effective, category=category, message=message, metadata=meta or {})
        self.alerts.append(alert)
        return alert

    def active_alerts(self) -> List[Alert]:
        return [alert for alert in self.alerts if not alert.dismissed]

    def dismiss(self, alert_id: str) -> bool:
        for alert in self.alerts:
            if alert.id == alert_id:
                alert.dismissed = True
                return True
        return False

    def alerts_by_priority(self) -> Dict[AlertPriority, List[Alert]]:
        grouped: Dict[AlertPriority, List[Alert]] = defaultdict(list)
        for alert in self.alerts:
            grouped[alert.priority].append(alert)
        return dict(grouped)

class MilestoneTracker:
    """Verifies progression against intermediate plan gates."""
    def __init__(self):
        self.milestones: List[Milestone] = []

    def auto_generate_milestones(self, desired: DesiredState, num: int = 5) -> List[Milestone]:
        for i in range(1, num + 1):
            frac = i / num
            targets = {k: v * frac for k, v in desired.all_dims().items()}
            self.milestones.append(Milestone(name=f"Gate {int(frac*100)}%", target_dims=targets))
        return self.milestones

    def check_progress(self, current: State) -> List[Milestone]:
        newly_done = []
        cur_dims = current.all_dims()
        for m in self.milestones:
            if m.achieved: continue
            if all(cur_dims.get(k, 0) >= v for k, v in m.target_dims.items()):
                m.achieved = True
                m.achieved_at = datetime.now(timezone.utc).isoformat()
                newly_done.append(m)
        return newly_done

    def status(self) -> List[Milestone]:
        return self.milestones

class CheckpointManager:
    """The core Insurance mechanism for process recovery."""
    def __init__(self):
        self.checkpoints: List[Checkpoint] = []

    def save(self, label: str, cur: State, des: DesiredState, intent: Intent) -> Checkpoint:
        cp = Checkpoint(label=label, current_state=copy.deepcopy(cur),
                        desired_state=copy.deepcopy(des), intent=copy.deepcopy(intent))
        self.checkpoints.append(cp)
        return cp

    def restore_last(self) -> Optional[Checkpoint]:
        return copy.deepcopy(self.checkpoints[-1]) if self.checkpoints else None

    def restore(self, checkpoint_id: str) -> Optional[Checkpoint]:
        for checkpoint in reversed(self.checkpoints):
            if checkpoint.id == checkpoint_id:
                return copy.deepcopy(checkpoint)
        return None


# ---------------------------------------------------------------------------
#  9. Historical Replay (Verification)
# ---------------------------------------------------------------------------

class HistoricalReplay:
    """Verifies process history via step-by-step audit."""
    def __init__(self):
        self.frames: List[Dict] = []

    def record_frame(self, report: MonitorReport, step: int, extra: Optional[Dict[str, Any]] = None):
        self.frames.append({
            "step": step, "timestamp": report.timestamp,
            "progress": report.progress_score, "health": report.state_health,
            "intent": report.intent_score, "quality": report.quality.composite,
            "velocity": report.velocity, "convergence": report.convergence.value,
            "alerts_count": len(report.alerts),
            "drifts_count": len([drift for drift in report.drifts if drift.severity != Severity.NONE]),
            "status": report.trajectory_status.value, "extra": extra or {},
        })

    def get_frame(self, step: int) -> Optional[Dict[str, Any]]:
        return next((frame for frame in self.frames if frame["step"] == step), None)

    def diff(self, step_a: int, step_b: int) -> Dict[str, Tuple[Any, Any]]:
        first, second = self.get_frame(step_a), self.get_frame(step_b)
        if first is None or second is None:
            return {}
        keys = ("progress", "health", "intent", "quality", "velocity")
        return {key: (first.get(key), second.get(key)) for key in keys}

    def replay(self, start: int = 0, end: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.frames[start:end] if end is not None else self.frames[start:]

    def summary(self) -> str:
        if not self.frames:
            return "No frames recorded."
        lines = ["=== Historical Replay ==="]
        for frame in self.frames:
            lines.append("Step %3d | P=%.3f H=%.3f I=%.3f Q=%.3f V=%.3f C=%s" % (
                frame["step"], frame["progress"], frame["health"], frame["intent"],
                frame["quality"], frame["velocity"], frame["convergence"]))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
#  10. Self-Tuner (Reliability)
# ---------------------------------------------------------------------------

class SelfTuner:
    """Insures system reliability by adapting drift sensitivity to noise."""
    def __init__(self):
        self.thresholds = {"intent": 0.7, "context": 0.7, "capability_drop": 0.15}
        self.adjustment_history = []

    def auto_tune(self, reports: List[MonitorReport]):
        if len(reports) < 5: return
        # Adjust drift threshold if the process is naturally noisy
        intents = [r.intent_score for r in reports]
        if statistics.stdev(intents) > 0.05:
            self.thresholds["intent"] = max(0.5, self.thresholds["intent"] - 0.02)
        self.adjustment_history.append(dict(self.thresholds))

    def get_thresholds(self) -> Dict[str, float]:
        return self.thresholds


# ---------------------------------------------------------------------------
#  11. Multi-Agent Monitoring (Determination)
# ---------------------------------------------------------------------------

@dataclass
class AgentProfile:
    agent_id: str
    name: str = ""
    engine: Optional[Any] = None # Stores MonitoringEngine
    reports: List[MonitorReport] = field(default_factory=list)

class MultiAgentMonitor:
    """Easy Access: Monitor multiple processes or agents simultaneously."""
    def __init__(self):
        self.agents: Dict[str, AgentProfile] = {}

    def register_agent(self, agent_id: str, name: str, intent: Intent,
                       desired: DesiredState, initial: State = None):
        from .engine import MonitoringEngine
        engine = MonitoringEngine(intent, desired, initial)
        self.agents[agent_id] = AgentProfile(agent_id, name, engine)

    def update_agent(self, agent_id: str, state: State) -> MonitorReport:
        agent = self.agents[agent_id]
        agent.engine.set_current_state(state)
        report = agent.engine.evaluate()
        agent.reports.append(report)
        return report

    def leaderboard(self, metric: str = "progress_score") -> List[Tuple[str, float]]:
        rankings = []
        for agent in self.agents.values():
            if not agent.reports:
                continue
            value = getattr(agent.reports[-1], metric, 0.0)
            if isinstance(value, (int, float)):
                rankings.append((agent.name or agent.agent_id, float(value)))
        return sorted(rankings, key=lambda x: x[1], reverse=True)

    def detect_outlier_agents(self, metric: str = "progress_score") -> List[str]:
        """Return agent ids beyond 1.5 IQR for a numeric report metric."""
        values = []
        for agent_id, agent in self.agents.items():
            if agent.reports:
                value = getattr(agent.reports[-1], metric, None)
                if isinstance(value, (int, float)):
                    values.append((agent_id, float(value)))
        if len(values) < 4:
            return []
        ordered = sorted(value for _, value in values)
        q1 = ordered[len(ordered) // 4]
        q3 = ordered[(3 * len(ordered)) // 4]
        spread = q3 - q1
        lower, upper = q1 - 1.5 * spread, q3 + 1.5 * spread
        return [agent_id for agent_id, value in values if value < lower or value > upper]

    def comparative_summary(self) -> Dict[str, Dict[str, Any]]:
        """Return the latest comparable integrity measures for each agent."""
        summary: Dict[str, Dict[str, Any]] = {}
        for agent_id, agent in self.agents.items():
            if not agent.reports:
                continue
            report = agent.reports[-1]
            summary[agent_id] = {
                "name": agent.name,
                "progress": report.progress_score,
                "health": report.state_health,
                "intent": report.intent_score,
                "quality": report.quality.composite,
                "convergence": report.convergence.value,
                "stage": assess_stage(report.progress_score, report.state_health,
                                      report.intent_score, report.quality.composite).stage_name,
            }
        return summary


# ---------------------------------------------------------------------------
#  12. Rich Export (Easy Use)
# ---------------------------------------------------------------------------

class Exporter:
    """Easy Use: One-click export for verified process reports."""
    @staticmethod
    def to_json(report: MonitorReport, filepath: Optional[str] = None) -> str:
        value = report.to_json()
        if filepath:
            with open(filepath, "w", encoding="utf-8") as handle:
                handle.write(value)
        return value

    @staticmethod
    def to_csv(reports: List[MonitorReport], filepath: Optional[str] = None) -> str:
        fields = ("timestamp", "progress", "health", "intent", "trajectory", "context", "drift", "velocity", "acceleration", "quality", "convergence", "health_status")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            writer.writerow({
                "timestamp": report.timestamp, "progress": report.progress_score,
                "health": report.state_health, "intent": report.intent_score,
                "trajectory": report.trajectory_quality, "context": report.context_integrity,
                "drift": report.drift_score, "velocity": report.velocity,
                "acceleration": report.acceleration, "quality": report.quality.composite,
                "convergence": report.convergence.value, "health_status": report.health_status.value,
            })
        value = output.getvalue()
        if filepath:
            with open(filepath, "w", encoding="utf-8", newline="") as handle:
                handle.write(value)
        return value

    @staticmethod
    def to_markdown(report: MonitorReport) -> str:
        return ("# UPMP Monitor Report\n\n| Metric | Value |\n|---|---|\n"
                "| Intent Score | %.4f |\n| Progress Score | %.4f |\n"
                "| State Health | %.4f (%s) |\n| Trajectory Quality | %.4f (%s) |\n"
                "| Context Integrity | %.4f |\n| Drift Score | %.4f |\n\n%s" % (
                    report.intent_score, report.progress_score, report.state_health,
                    report.health_status.value, report.trajectory_quality,
                    report.trajectory_status.value, report.context_integrity,
                    report.drift_score, report.summary()))

    @staticmethod
    def to_html(report: MonitorReport) -> str:
        import html
        return "<!doctype html><html><head><meta charset=\"utf-8\"><title>UPMP report</title></head><body><h1>UPMP Monitor Report</h1><pre>%s</pre></body></html>" % html.escape(Exporter.to_markdown(report))

