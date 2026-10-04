#!/usr/bin/env python3
"""UPMP Quick Start Example — demonstrates core features."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from UPMP import (
    MonitoringEngine, Intent, State, DesiredState, quick_monitor,
    Exporter, MultiAgentMonitor, assess_stage,
    DriftType, HealthStatus, ConvergenceStatus,
)

# =========================================================================
#  Example 1: One-Shot Quick Monitor
# =========================================================================
print("=" * 60)
print("Example 1: Quick Monitor (one-shot)")
print("=" * 60)

report = quick_monitor(
    intent_desc="Master reasoning",
    current_vals={"knowledge": 0.5, "reasoning": 0.3},
    desired_vals={"knowledge": 1.0, "reasoning": 1.0},
    success_criteria=["reasoning", "mastery"],
)
print(report.summary())
print()

# =========================================================================
#  Example 2: Full Engine with Drift & Recovery
# =========================================================================
print("=" * 60)
print("Example 2: Full Engine (drift injection + recovery)")
print("=" * 60)

intent = Intent(
    description="Compute 88 * 8 accurately",
    success_criteria=["multiply 88 by 8", "get correct result 704"],
)
desired = DesiredState(knowledge=1.0, reasoning=1.0, memory=1.0)
engine = MonitoringEngine(intent=intent, desired=desired)
engine.set_original_context(["multiply", "88", "8", "704", "result"])

# Normal growth
for step in range(1, 6):
    frac = step / 6.0
    engine.set_current_state(State(
        knowledge=frac * 0.9, reasoning=frac * 0.8, memory=frac * 1.0
    ))
    engine.evaluate(
        current_behavior_desc="learning multiplication and practicing 88*8",
        retained_context=["multiply", "88", "8", "704", "result"],
    )

# Inject drift
engine.set_current_state(State(knowledge=0.5, reasoning=0.3, memory=0.4))
r_drift = engine.evaluate(
    current_behavior_desc="maximizing speed instead of accuracy",
    retained_context=["speed", "fast"],
)
print(f"\n--- After Drift ---")
print(f"  Drift detected: {len(r_drift.drifts)} drifts")
print(f"  Intent score:   {r_drift.intent_score:.3f}")
print(f"  Context:        {r_drift.context_integrity:.3f}")

# Recover
engine.set_current_state(State(knowledge=0.9, reasoning=0.85, memory=0.95))
r_final = engine.evaluate(
    current_behavior_desc="multiply 88 by 8 and get correct result 704 accurately",
    retained_context=["multiply", "88", "8", "704", "result"],
)
print(f"\n--- After Recovery ---")
print(f"  Progress:       {r_final.progress_score:.3f}")
print(f"  Intent score:   {r_final.intent_score:.3f}")
print(f"  Health:         {r_final.health_status.value}")
print(f"  Convergence:    {r_final.convergence.value}")
print()

# =========================================================================
#  Example 3: Stage Assessment
# =========================================================================
print("=" * 60)
print("Example 3: Stage Assessment")
print("=" * 60)

stage = assess_stage(
    progress=0.6, health=0.7, intent=0.8, quality=0.5
)
print(f"  Current Stage:  {stage.current_stage} — {stage.stage_name}")
print(f"  Confidence:     {stage.confidence:.3f}")
print(f"  Next Stage:     {stage.next_stage} — {stage.next_stage_name}")
print(f"  Distance:       {stage.distance_to_next:.3f}")
print()

# =========================================================================
#  Example 4: Export
# =========================================================================
print("=" * 60)
print("Example 4: Export Report")
print("=" * 60)

json_str = Exporter.to_json(r_final)
print(f"  JSON length:    {len(json_str)} chars")
md_str = Exporter.to_markdown(r_final)
print(f"  Markdown length: {len(md_str)} chars")
print(f"  HTML length:    {len(Exporter.to_html(r_final))} chars")

# Save to file
export_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.json")
Exporter.to_json(r_final, export_path)
print(f"  Saved to:       {export_path}")
print()

# =========================================================================
#  Example 5: Multi-Agent Monitoring
# =========================================================================
print("=" * 60)
print("Example 5: Multi-Agent Monitoring")
print("=" * 60)

mam = MultiAgentMonitor()
desired_common = DesiredState(knowledge=1.0, reasoning=1.0)

mam.register_agent("a", "Physics Agent",
    Intent(description="Learn physics", success_criteria=["mechanics"]),
    desired_common, State(knowledge=0.3, reasoning=0.2))

mam.register_agent("b", "Math Agent",
    Intent(description="Learn math", success_criteria=["algebra"]),
    desired_common, State(knowledge=0.6, reasoning=0.5))

mam.update_agent("a", State(knowledge=0.5, reasoning=0.4))
mam.update_agent("b", State(knowledge=0.8, reasoning=0.7))

summary = mam.comparative_summary()
for aid, data in summary.items():
    print(f"  {data['name']:20s} | P={data['progress']:.3f} H={data['health']:.3f} Stage={data['stage']}")

lb = mam.leaderboard()
print(f"  Leader: {lb[0][0]} (score={lb[0][1]:.3f})")
print()

print("=" * 60)
print("All examples completed successfully!")
print("=" * 60)

