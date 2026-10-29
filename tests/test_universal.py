#!/usr/bin/env python3
"""
UPMP Universal Process Verification — SDLC Monitoring Case
=========================================================
Tests the framework's ability to monitor a non-AI process (Software Development).
"""

import json
import os
import sys

# Ensure the parent directory is in the path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from UPMP import MonitoringEngine, Intent, State, DesiredState, DriftType, Severity

def test_sdlc_monitoring():
    print("\n" + "="*60)
    print("UNIVERSAL TEST: Software Development Lifecycle Monitoring")
    print("="*60)

    # 1. Define Intent (Charter)
    intent = Intent(
        description="Ship a stable, high-coverage API for financial transactions.",
        success_criteria=["90% test coverage", "Zero critical bugs", "High deployment velocity"],
        priority=1.0
    )

    # 2. Define Target State (KPIs)
    # Mapping:
    # knowledge -> test_coverage
    # performance -> deployment_velocity
    # efficiency -> resource_utilization
    # memory -> stability
    desired = DesiredState(
        knowledge=0.95,     # 95% coverage
        performance=0.80,   # High velocity
        efficiency=0.70,    # Good resource use
        memory=1.0          # 100% stability
    )

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["api", "stable", "coverage", "bugs", "velocity"])

    # 3. Step 1: Initial Sprint (Good Progress)
    engine.set_current_state(State(
        knowledge=0.40, performance=0.30, efficiency=0.50, memory=0.90
    ))
    report1 = engine.evaluate(
        current_behavior_desc="Implementing core endpoints and writing unit tests",
        retained_context=["api", "endpoints", "tests", "coverage"]
    )
    print(f"Sprint 1: Progress={report1.progress_score:.2f}, Health={report1.state_health:.2f}")

    # 4. Step 2: "Crunch Mode" (Drift Injection)
    # Team sacrifices stability and coverage for velocity (Performance)
    engine.set_current_state(State(
        knowledge=0.35,     # Coverage dropped (Capability Drift)
        performance=0.90,   # Velocity peaked
        efficiency=0.40,    # Resource use grew chaotic
        memory=0.50         # Stability tanked (Critical Regression)
    ))
    report2 = engine.evaluate(
        current_behavior_desc="Bypassing tests to hit ship date; maximizing deployment speed",
        retained_context=["velocity", "ship", "fast"]
    )

    print(f"\nSprint 2 (Crunch):")
    print(f"  Drifts Detected: {[d.drift_type.value for d in report2.drifts]}")
    print(f"  Intent Preservation: {report2.intent_score:.2f} (Target: >0.70)")
    print(f"  Alerts Raised: {len(report2.alerts)}")

    # 5. Verify Functional Logic
    # Did it detect the loss of intent?
    intent_preserved = report2.intent_score < 0.6  # Should be low because criteria "90% coverage" and "Zero critical bugs" are ignored in behavior

    # Did it detect the regressions?
    regression_detected = "memory" in report2.regressions or any(d.drift_type == DriftType.CAPABILITY for d in report2.drifts)

    # 6. Final Interventions
    print(f"\nRecommended Interventions:")
    for iv in report2.interventions:
        print(f"  -> {iv.issue}: {iv.action}")

    passed = intent_preserved and regression_detected and len(report2.interventions) > 0
    print(f"\nVerification Result: {'PASS' if passed else 'FAIL'}")
    return report2

if __name__ == "__main__":
    test_sdlc_monitoring()

