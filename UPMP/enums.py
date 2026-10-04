"""UPMP Enumeration Types — Process Agnostic."""

from enum import Enum, auto


class TrajectoryStatus(Enum):
    ALIGNED = "aligned"
    PARTIALLY_ALIGNED = "partially_aligned"
    DIVERGENT = "divergent"
    STATIONARY = "stationary"


class HealthStatus(Enum):
    THRIVING = "thriving"
    HEALTHY = "healthy"
    CAUTION = "caution"
    CRITICAL = "critical"
    FAILING = "failing"


class DriftType(Enum):
    # Core process drifts
    GOAL = "goal"           # Deviating from the defined intent
    CONTEXT = "context"     # The environment or scope has shifted
    CAPABILITY = "capability" # Loss of a previously held skill or metric level

    # Specific but common drifts
    BEHAVIOR = "behavior"   # Action patterns don't match expected profile
    KNOWLEDGE = "knowledge" # Information decay (for AI/Humans)
    EFFICIENCY = "efficiency" # Resource usage vs output drift
    QUALITY = "quality"     # Output standard degradation


class Severity(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertPriority(Enum):
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


class ConvergenceStatus(Enum):
    CONVERGING = "converging"
    OSCILLATING = "oscillating"
    DIVERGING = "diverging"
    STATIONARY = "stationary"
    UNKNOWN = "unknown"


class TrendDirection(Enum):
    IMPROVING = "improving"
    DECLINING = "declining"
    STABLE = "stable"
    VOLATILE = "volatile"

