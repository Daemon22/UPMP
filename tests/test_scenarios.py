#!/usr/bin/env python3
"""
UPMP Multi-Scenario Stress Test & Verification Suite
=====================================================
This suite verifies the Universal Progress Monitoring Package (UPMP) across
diverse process types, lengths, intents, and dynamics.

Scenarios:
  1. Micro-Process (Login): 2 steps, high-speed completion.
  2. Corporate Marathon: 100 steps, long-term growth with noise.
  3. Manufacturing Conflict: 15 steps, detecting intent drift (speed vs precision).
  4. Skill Plateau: 20 steps, detecting stagnation and triggering interventions.
  5. Network Volatility: 25 steps, detecting statistical anomalies and health drops.
  6. S-Curve Growth: 30 steps, projecting completion during decelerating momentum.
  7. Cyclical Maintenance: 60 steps, monitoring recurring patterns and oscillations.
  8. Critical Failure & Recovery: 20 steps, monitoring rollback and remediation.
  9. High-Frequency Bursts: 100 steps of rapid, small state changes.
  10. Resource Decay (Entropy): 40 steps, monitoring degradation and capability drift.
  11. Chaotic Walk: 50 steps, verifying trajectory quality in non-directional processes.
  12. Multi-Phase Lifecycle: 3 distinct process phases in one timeline.
  13. Supply Chain Logistics: 15 steps, monitoring inventory, latency, and cost.
  14. Biological Vitals: 30 steps, monitoring stability of physiological signals.
  15. Financial Risk: 25 steps, monitoring portfolio drift from risk intent.
"""

import json
import os
import sys
import random
import math
import statistics

# Ensure the parent directory is in the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import UPMP as upmp
from UPMP import (
    MonitoringEngine, Intent, State, DesiredState,
    DriftType, HealthStatus, ConvergenceStatus, TrendDirection,
    Severity, AlertPriority, TrajectoryStatus
)

def run_scenario(name, intent, desired, state_generator, steps, behavior_generator=None, context_generator=None):
    print(f"\n>>> [SCENARIO] {name} ({steps} steps)")
    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(intent.success_criteria or [intent.description])

    reports = []
    for i in range(steps):
        current_state = state_generator(i, steps)
        behavior = behavior_generator(i, steps) if behavior_generator else ""
        context = context_generator(i, steps) if context_generator else None

        engine.set_current_state(current_state)
        report = engine.evaluate(current_behavior_desc=behavior, retained_context=context)
        reports.append(report)

    final = reports[-1]
    print(f"    Final Metrics: Progress={final.progress_score:.4f}, Health={final.state_health:.4f}, Convergence={final.convergence.value}")
    return reports

# ---------------------------------------------------------------------------
#  SCENARIOS
# ---------------------------------------------------------------------------

def scenario_micro():
    intent = Intent("Auth", success_criteria=["validate", "check", "return"])
    desired = DesiredState(performance=1.0)
    def gen(s, t): return State(performance=0.5 if s==0 else 1.0)
    reports = run_scenario("Micro-Process", intent, desired, gen, 2)
    assert reports[-1].progress_score == 1.0
    print("    [PASS] Micro-process verified.")

def scenario_marathon():
    intent = Intent("Scale-up", success_criteria=["growth"])
    desired = DesiredState(performance=0.9, efficiency=0.8)
    def gen(s, t):
        f = (s + 1) / t
        return State(performance=min(1.0, f*0.9), efficiency=min(1.0, f*0.8))
    reports = run_scenario("Marathon", intent, desired, gen, 100)
    assert reports[-1].convergence in (ConvergenceStatus.CONVERGING, ConvergenceStatus.STATIONARY)
    print("    [PASS] Marathon growth verified.")

def scenario_conflict():
    intent = Intent("Precision Mfg", success_criteria=["speed", "precision"])
    desired = DesiredState(performance=1.0, custom={"precision": 1.0})
    def gen(s, t):
        if s < 8: return State(performance=s/10, custom={"precision": s/10})
        return State(performance=0.95, custom={"precision": 0.10})
    def behavior(s, t): return "Balancing" if s < 8 else "Speed ONLY"
    reports = run_scenario("Conflict Drift", intent, desired, gen, 15, behavior)
    assert any(d.drift_type == DriftType.GOAL for d in reports[10].drifts)
    print("    [PASS] Conflict drift verified.")

def scenario_plateau():
    intent = Intent("Opt", success_criteria=["converge"])
    desired = DesiredState(efficiency=1.0)
    def gen(s, t): return State(efficiency=0.5 if s > 5 else s/10)
    reports = run_scenario("Plateau", intent, desired, gen, 20)
    assert reports[-1].velocity < 0.05
    print("    [PASS] Stagnation verified.")

def scenario_volatility():
    intent = Intent("Stabilize", success_criteria=["smooth"])
    desired = DesiredState(performance=0.8)
    def gen(s, t):
        if s == 12: return State(performance=0.01)
        return State(performance=0.7 + random.uniform(-0.02, 0.02))
    reports = run_scenario("Volatility", intent, desired, gen, 25)
    assert any(reports[12].anomaly_flags.values())
    print("    [PASS] Anomaly detection verified.")

def scenario_scurve():
    intent = Intent("Saturation", success_criteria=["penetration"])
    desired = DesiredState(performance=1.0)
    def gen(s, t):
        x = (s - t/2) / (t/10)
        return State(performance=1 / (1 + math.exp(-x)))
    reports = run_scenario("S-Curve", intent, desired, gen, 30)
    assert any(r.acceleration < 0 for r in reports[20:])
    print("    [PASS] Deceleration verified.")

def scenario_cyclical():
    intent = Intent("Resource Cycle", success_criteria=["cycle"])
    desired = DesiredState(efficiency=0.5)
    def gen(s, t): return State(efficiency=0.5 + 0.4 * math.sin(s * 0.4))
    reports = run_scenario("Cyclical", intent, desired, gen, 60)
    assert reports[-1].convergence == ConvergenceStatus.OSCILLATING
    print("    [PASS] Oscillation verified.")

def scenario_failure_recovery():
    intent = Intent("Uptime", success_criteria=["available"])
    desired = DesiredState(performance=1.0)
    engine = MonitoringEngine(intent, desired)
    reports = []
    for i in range(20):
        if i == 5: s = State(performance=0.05); engine.checkpoint("crash")
        elif i == 10: engine.rollback(); s = State(performance=0.85)
        else: s = State(performance=1.0 if i < 5 or i > 10 else 0.1)
        engine.set_current_state(s)
        reports.append(engine.evaluate())
    assert reports[-1].progress_score == 1.0
    print("    [PASS] Recovery verified.")

def scenario_bursts():
    intent = Intent("Bursts", success_criteria=["rapid"])
    desired = DesiredState(performance=1.0)
    def gen(s, t): return State(performance=min(1.0, s * 0.01))
    reports = run_scenario("High-Freq Bursts", intent, desired, gen, 100)
    assert reports[-1].progress_score >= 0.99
    print("    [PASS] Burst accumulation verified.")

def scenario_decay():
    intent = Intent("Maintenance", success_criteria=["longevity"])
    desired = DesiredState(performance=1.0)
    def gen(s, t): return State(performance=max(0.0, 1.0 - (s/t)*0.8)) # Decaying
    reports = run_scenario("Resource Decay (Entropy)", intent, desired, gen, 40)
    assert reports[-1].state_health < reports[0].state_health
    assert any(d.drift_type in (DriftType.CAPABILITY, DriftType.KNOWLEDGE) for d in reports[-1].drifts)
    print("    [PASS] Decay monitoring verified.")

def scenario_chaotic():
    intent = Intent("Directional", success_criteria=["forward"])
    desired = DesiredState(performance=1.0, reasoning=1.0)
    def gen(s, t): return State(performance=random.random(), reasoning=random.random())
    reports = run_scenario("Chaotic Walk", intent, desired, gen, 50)
    non_aligned = [r for r in reports if r.trajectory_status != TrajectoryStatus.ALIGNED]
    assert len(non_aligned) > 10 # Should have some misalignments
    print("    [PASS] Non-directional trajectory verified.")

def scenario_multiphase():
    intent = Intent("Multi-Phase", success_criteria=["build", "test", "run"])
    desired = DesiredState(knowledge=1.0, performance=1.0, efficiency=1.0)
    def gen(s, t):
        if s < 10: return State(knowledge=s/10) # Building
        if s < 20: return State(knowledge=1.0, performance=(s-10)/10) # Testing
        return State(knowledge=1.0, performance=1.0, efficiency=(s-20)/10) # Running
    reports = run_scenario("Multi-Phase Lifecycle", intent, desired, gen, 30)
    assert reports[9].progress_score < reports[19].progress_score < reports[29].progress_score
    print("    [PASS] Phase transitions verified.")

def scenario_supply_chain():
    intent = Intent("Supply Chain", success_criteria=["inventory_opt", "latency_reduction"])
    desired = DesiredState(efficiency=0.8, custom={"inventory_health": 0.9, "logistics_latency": 0.2})
    def gen(s, t):
        # Latency dropping over time (good), inventory stabilizing
        f = (s + 1) / t
        return State(efficiency=0.5 + f*0.3, custom={"inventory_health": 0.4 + f*0.5, "logistics_latency": 1.0 - f*0.7})
    reports = run_scenario("Supply Chain Logistics", intent, desired, gen, 15)
    assert reports[-1].progress_score > 0.7
    print("    [PASS] Logistics process verified.")

def scenario_biological():
    intent = Intent("Health Monitoring", success_criteria=["stable_heart_rate", "optimal_oxygen"])
    desired = DesiredState(custom={"heart_rate_variability": 0.8, "oxygen_saturation": 0.98})
    def gen(s, t):
        # Mostly stable, one dip
        if s == 20: return State(custom={"heart_rate_variability": 0.2, "oxygen_saturation": 0.85})
        return State(custom={"heart_rate_variability": 0.75 + random.uniform(-0.05, 0.05),
                             "oxygen_saturation": 0.97 + random.uniform(-0.01, 0.01)})
    reports = run_scenario("Biological Vitals", intent, desired, gen, 30)
    assert any(reports[20].anomaly_flags.values())
    print("    [PASS] Physiological signal monitoring verified.")

def scenario_financial():
    intent = Intent("Portfolio Rebalancing", success_criteria=["risk_parity", "yield_target"])
    # Normalize inverse metrics at the domain boundary: a lower raw volatility
    # score means less risk and therefore a higher desired risk-control value.
    desired = DesiredState(performance=0.15, custom={"sharpe_ratio": 0.8, "risk_control": 1.0})
    def gen(s, t):
        # Drift away from volatility limit
        f = (s + 1) / t
        raw_volatility = 0.10 + f*0.30
        return State(performance=0.10 + f*0.05, custom={"sharpe_ratio": 0.6 + f*0.2, "risk_control": 1.0 - raw_volatility})
    reports = run_scenario("Financial Risk Management", intent, desired, gen, 25)
    assert any(d.drift_type == DriftType.CAPABILITY for d in reports[-1].drifts) # High volatility is a capability loss in this context
    print("    [PASS] Financial risk drift verified.")

# ---------------------------------------------------------------------------
#  MAIN
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"\n{'#'*80}\n#  UPMP UNIVERSAL PROCESS VERIFICATION SUITE\n{'#'*80}")
    scenarios = [
        scenario_micro, scenario_marathon, scenario_conflict, scenario_plateau,
        scenario_volatility, scenario_scurve, scenario_cyclical,
        scenario_failure_recovery, scenario_bursts, scenario_decay,
        scenario_chaotic, scenario_multiphase, scenario_supply_chain,
        scenario_biological, scenario_financial
    ]
    passed = 0
    for s in scenarios:
        try:
            s(); passed += 1
        except Exception as e:
            print(f"    [FAIL] {s.__name__}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*80}\nVERIFICATION SUMMARY: {passed}/{len(scenarios)} Passed\n{'='*80}")
    sys.exit(0 if passed == len(scenarios) else 1)

