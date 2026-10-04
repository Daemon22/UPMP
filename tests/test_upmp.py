#!/usr/bin/env python3
"""
UPMP v2.1.0 — Comprehensive Test Suite
========================================
Tests all 15 core layers + 12 extended features including:
  1. 88x8 computation test (from original)
  2. Multi-agent monitoring test
  3. Anomaly & regression detection test
  4. Milestone tracking test
  5. Checkpoint & rollback test
  6. Time-series analysis test
  7. Adaptive weighting test
  8. Dependency graph test
  9. Alert system test
  10. Self-tuning test
  11. Historical replay test
  12. Rich export test
  13. Full integration test with drift injection & recovery
"""

import json
import os
import sys
from datetime import datetime, timezone

# Ensure the parent directory is in the path so we can import the UPMP package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import UPMP as upmp
from UPMP import *

# ===========================================================================
#  Test Results Collector
# ===========================================================================

test_results = {
    "package": "UPMP",
    "version": upmp.__version__,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "tests": {},
    "summary": {"total": 0, "passed": 0, "failed": 0},
}

def record_test(name: str, passed: bool, details: str = ""):
    test_results["tests"][name] = {
        "passed": passed,
        "details": details,
    }
    test_results["summary"]["total"] += 1
    if passed:
        test_results["summary"]["passed"] += 1
    else:
        test_results["summary"]["failed"] += 1
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {details}" if details else ""))

def assert_test(name: str, condition: bool, details: str = ""):
    record_test(name, condition, details)

# ===========================================================================
#  TEST 1: 88x8 Computation Test
# ===========================================================================

def test_88x8():
    print("\n" + "="*60)
    print("TEST 1: 88x8 Computation Test")
    print("="*60)

    intent = Intent(
        description="Compute 88 * 8 accurately",
        success_criteria=["multiply 88 by 8", "get correct result 704"],
        priority=1.0,
    )

    desired = DesiredState(
        knowledge=1.0, reasoning=1.0, memory=1.0,
        performance=1.0, confidence=1.0,
    )

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["multiply", "88", "8", "704", "result"])

    # Simulate 10 learning steps toward 88*8=704
    states = []
    for step in range(10):
        frac = (step + 1) / 10.0
        s = State(
            knowledge=frac * 0.9,
            reasoning=frac * 0.8,
            memory=min(1.0, frac * 1.0),
            performance=frac * 0.85,
            confidence=frac * 0.75,
        )
        states.append(s)

    # Phase 1: Normal progression (steps 0-6)
    reports = []
    for i in range(7):
        engine.set_current_state(states[i])
        r = engine.evaluate(
            current_behavior_desc="learning multiplication and practicing 88*8",
            retained_context=["multiply", "88", "8", "704", "result"],
        )
        reports.append(r)

    # Phase 2: Inject goal drift (step 7)
    engine.set_current_state(State(
        knowledge=0.65, reasoning=0.58, memory=0.75,
        performance=0.60, confidence=0.55,
    ))
    r_drift = engine.evaluate(
        current_behavior_desc="maximizing speed of calculation instead of accuracy",
        retained_context=["speed", "fast", "calculation"],
    )
    reports.append(r_drift)

    # Phase 3: Inject context loss (step 8)
    engine.set_current_state(State(
        knowledge=0.72, reasoning=0.65, memory=0.40,
        performance=0.70, confidence=0.60,
    ))
    r_ctx = engine.evaluate(
        current_behavior_desc="practicing multiplication",
        retained_context=["multiply", "practice"],
    )
    reports.append(r_ctx)

    # Phase 4: Full recovery (step 9)
    engine.set_current_state(states[9])
    r_recovery = engine.evaluate(
        current_behavior_desc="multiply 88 by 8 and get correct result 704 accurately",
        retained_context=["multiply", "88", "8", "704", "result", "accurate"],
    )
    reports.append(r_recovery)

    # Assertions
    assert_test("88x8: Progress reaches near target",
                r_recovery.progress_score >= 0.85,
                f"progress={r_recovery.progress_score:.4f}")

    assert_test("88x8: Goal drift detected at step 7",
                any(d.drift_type == DriftType.GOAL for d in r_drift.drifts),
                f"drifts={[d.drift_type.value for d in r_drift.drifts]}")

    assert_test("88x8: Context drift detected at step 8",
                any(d.drift_type == DriftType.CONTEXT for d in r_ctx.drifts),
                f"context_integrity={r_ctx.context_integrity:.4f}")

    assert_test("88x8: Intent preserved after recovery",
                r_recovery.intent_score >= 0.9,
                f"intent={r_recovery.intent_score:.4f}")

    assert_test("88x8: State health is healthy after recovery",
                r_recovery.health_status in (HealthStatus.HEALTHY, HealthStatus.THRIVING),
                f"health_status={r_recovery.health_status.value}")

    assert_test("88x8: Interventions were generated",
                any(r.interventions for r in reports),
                f"total_interventions={sum(len(r.interventions) for r in reports)}")

    return reports


# ===========================================================================
#  TEST 2: Multi-Agent Monitoring
# ===========================================================================

def test_multi_agent():
    print("\n" + "="*60)
    print("TEST 2: Multi-Agent Monitoring")
    print("="*60)

    mam = MultiAgentMonitor()

    intent_a = Intent(description="Agent A: Learn physics", success_criteria=["understand mechanics"])
    intent_b = Intent(description="Agent B: Learn chemistry", success_criteria=["balance equations"])
    intent_c = Intent(description="Agent C: Learn math", success_criteria=["solve algebra"])

    desired = DesiredState(knowledge=1.0, reasoning=1.0, performance=1.0)

    mam.register_agent("a", "Physics Agent", intent_a, desired,
                       State(knowledge=0.3, reasoning=0.2, performance=0.1))
    mam.register_agent("b", "Chemistry Agent", intent_b, desired,
                       State(knowledge=0.6, reasoning=0.5, performance=0.4))
    mam.register_agent("c", "Math Agent", intent_c, desired,
                       State(knowledge=0.8, reasoning=0.7, performance=0.6))

    mam.update_agent("a", State(knowledge=0.5, reasoning=0.4, performance=0.3))
    mam.update_agent("b", State(knowledge=0.7, reasoning=0.6, performance=0.5))
    mam.update_agent("c", State(knowledge=0.9, reasoning=0.85, performance=0.8))

    summary = mam.comparative_summary()
    assert_test("Multi-Agent: 3 agents registered",
                len(summary) == 3, f"count={len(summary)}")

    lb = mam.leaderboard("progress_score")
    assert_test("Multi-Agent: Leaderboard ranks agents",
                len(lb) == 3, f"leaderboard={lb}")

    assert_test("Multi-Agent: Math Agent leads progress",
                lb[0][0] == "Math Agent", f"leader={lb[0][0]}")

    outliers = mam.detect_outlier_agents()
    assert_test("Multi-Agent: Outlier detection runs",
                isinstance(outliers, list), f"outliers={outliers}")


# ===========================================================================
#  TEST 3: Anomaly & Regression Detection
# ===========================================================================

def test_anomaly_regression():
    print("\n" + "="*60)
    print("TEST 3: Anomaly & Regression Detection")
    print("="*60)

    intent = Intent(description="Test anomaly detection", success_criteria=["stable growth"])
    desired = DesiredState(knowledge=1.0, reasoning=1.0)

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["test"])

    for i in range(5):
        frac = (i + 1) / 10.0
        engine.set_current_state(State(knowledge=frac, reasoning=frac * 0.8))
        engine.evaluate()

    engine.set_current_state(State(knowledge=0.99, reasoning=0.4))
    r_anomaly = engine.evaluate()

    assert_test("Anomaly: Z-score anomaly detection works",
                any(r_anomaly.anomaly_flags.values()),
                f"flags={r_anomaly.anomaly_flags}")

    engine.set_current_state(State(knowledge=0.3, reasoning=0.2))
    r_regression = engine.evaluate()

    assert_test("Regression: Capability regression detected",
                len(r_regression.regressions) > 0,
                f"regressions={r_regression.regressions}")


# ===========================================================================
#  TEST 4: Milestone Tracking
# ===========================================================================

def test_milestones():
    print("\n" + "="*60)
    print("TEST 4: Milestone Tracking")
    print("="*60)

    intent = Intent(description="Reach milestones", success_criteria=["progress"])
    desired = DesiredState(knowledge=1.0, reasoning=1.0, performance=1.0)

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["progress"])

    milestones = engine.milestone_tracker.auto_generate_milestones(desired, num=4)
    assert_test("Milestones: Auto-generated 4 milestones",
                len(milestones) == 4, f"count={len(milestones)}")

    engine.set_current_state(State(knowledge=0.25, reasoning=0.25, performance=0.25))
    r1 = engine.evaluate()
    achieved_1 = sum(1 for m in engine.milestone_tracker.status() if m.achieved)
    assert_test("Milestones: First milestone achieved",
                achieved_1 >= 1, f"achieved={achieved_1}")

    engine.set_current_state(State(knowledge=0.55, reasoning=0.55, performance=0.55))
    r2 = engine.evaluate()
    achieved_2 = sum(1 for m in engine.milestone_tracker.status() if m.achieved)
    assert_test("Milestones: Second milestone achieved",
                achieved_2 >= 2, f"achieved={achieved_2}")

    engine.set_current_state(State(knowledge=1.0, reasoning=1.0, performance=1.0))
    r3 = engine.evaluate()
    achieved_all = sum(1 for m in engine.milestone_tracker.status() if m.achieved)
    assert_test("Milestones: All milestones achieved",
                achieved_all == 4, f"achieved={achieved_all}/4")


# ===========================================================================
#  TEST 5: Checkpoint & Rollback
# ===========================================================================

def test_checkpoint_rollback():
    print("\n" + "="*60)
    print("TEST 5: Checkpoint & Rollback")
    print("="*60)

    intent = Intent(description="Checkpoint test", success_criteria=["test"])
    desired = DesiredState(knowledge=1.0, reasoning=1.0)

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["test"])

    engine.set_current_state(State(knowledge=0.5, reasoning=0.5))
    engine.evaluate()

    cp = engine.checkpoint("good-state")
    assert_test("Checkpoint: Saved successfully",
                cp is not None and cp.label == "good-state")

    engine.set_current_state(State(knowledge=0.8, reasoning=0.8))
    r_forward = engine.evaluate()
    assert_test("Checkpoint: Progress moved forward",
                r_forward.progress_score > 0.5, f"progress={r_forward.progress_score:.4f}")

    success = engine.rollback(cp.id)
    assert_test("Checkpoint: Rollback succeeded", success)

    r_after = engine.evaluate()
    assert_test("Checkpoint: State restored after rollback",
                r_after.current_state.knowledge == 0.5,
                f"knowledge={r_after.current_state.knowledge}")


# ===========================================================================
#  TEST 6: Time-Series Analysis
# ===========================================================================

def test_time_series():
    print("\n" + "="*60)
    print("TEST 6: Time-Series Analysis")
    print("="*60)

    tsa = TimeSeriesAnalyzer(window=5)

    for i in range(10):
        tsa.record(i, State(knowledge=i * 0.1, reasoning=i * 0.05))

    trend_k = tsa.trend_direction("knowledge")
    trend_r = tsa.trend_direction("reasoning")

    assert_test("TimeSeries: Knowledge trend is IMPROVING",
                trend_k == TrendDirection.IMPROVING, f"trend={trend_k.value}")

    assert_test("TimeSeries: SMA computed",
                tsa.simple_moving_average("knowledge") is not None)

    assert_test("TimeSeries: EMA computed",
                tsa.exponential_moving_average("knowledge") is not None)

    assert_test("TimeSeries: Momentum computed",
                tsa.momentum("knowledge") is not None)

    for i in range(10):
        tsa.record(i + 10, State(knowledge=0.9, reasoning=0.45))

    conv = tsa.convergence_status("knowledge")
    assert_test("TimeSeries: Convergence detected for stable values",
                conv in (ConvergenceStatus.CONVERGING, ConvergenceStatus.STATIONARY),
                f"convergence={conv.value}")


# ===========================================================================
#  TEST 7: Adaptive Weighting
# ===========================================================================

def test_adaptive_weighting():
    print("\n" + "="*60)
    print("TEST 7: Adaptive Weighting")
    print("="*60)

    w0 = adaptive_weights(0)
    assert_test("Adaptive: Stage 0 weights intent heavily",
                w0.intent > w0.progress, f"intent={w0.intent}, progress={w0.progress}")

    w9 = adaptive_weights(9)
    assert_test("Adaptive: Stage 9 weights intent highest",
                w9.intent >= max(w9.progress, w9.trajectory, w9.context, w9.quality),
                f"intent={w9.intent}")

    w1 = adaptive_weights(1)
    assert_test("Adaptive: Stage 1 weights progress",
                w1.progress >= 0.25, f"progress_weight={w1.progress}")

    custom = WeightProfile("custom", 0.5, 0.1, 0.1, 0.2, 0.1)
    w_custom = adaptive_weights(5, custom)
    assert_test("Adaptive: Custom override works",
                w_custom.name == "custom", f"name={w_custom.name}")


# ===========================================================================
#  TEST 8: Dependency Graph
# ===========================================================================

def test_dependency_graph():
    print("\n" + "="*60)
    print("TEST 8: Capability Dependency Graph")
    print("="*60)

    g = DEFAULT_DEPENDENCY_GRAPH

    prereqs = g.get_prerequisites("reasoning")
    assert_test("DepGraph: Reasoning depends on knowledge",
                "knowledge" in prereqs, f"prereqs={prereqs}")

    deps = g.get_dependents("knowledge")
    assert_test("DepGraph: Knowledge has dependents",
                len(deps) > 0, f"dependents={deps}")

    impact = g.impact_analysis("knowledge", -0.2)
    assert_test("DepGraph: Impact analysis propagates",
                "reasoning" in impact or "memory" in impact,
                f"impact={impact}")

    bn = g.bottleneck_score("knowledge")
    assert_test("DepGraph: Knowledge is a bottleneck",
                bn > 0, f"bottleneck={bn}")

    d = g.to_dict()
    assert_test("DepGraph: Serializes to dict",
                "dependencies" in d and "strengths" in d)


# ===========================================================================
#  TEST 9: Alert System
# ===========================================================================

def test_alert_system():
    print("\n" + "="*60)
    print("TEST 9: Alert System")
    print("="*60)

    alerts = AlertSystem()
    alerts.add_escalation_rule("drift", 3, AlertPriority.CRITICAL)

    a1 = alerts.raise_alert(AlertPriority.INFO, "test", "Test alert")
    assert_test("Alert: Info alert created",
                a1.priority == AlertPriority.INFO)

    for i in range(4):
        a = alerts.raise_alert(AlertPriority.WARNING, "drift", f"Drift warning {i}")

    assert_test("Alert: Escalation after 3 repeat drifts",
                a.priority == AlertPriority.CRITICAL,
                f"priority={a.priority.name}")

    active = alerts.active_alerts()
    assert_test("Alert: Active alerts tracked",
                len(active) >= 4, f"count={len(active)}")

    alerts.dismiss(a1.id)
    assert_test("Alert: Dismiss works",
                a1.dismissed, f"dismissed={a1.dismissed}")

    by_p = alerts.alerts_by_priority()
    assert_test("Alert: Can group by priority",
                isinstance(by_p, dict), f"groups={len(by_p)}")


# ===========================================================================
#  TEST 10: Self-Tuning Parameters
# ===========================================================================

def test_self_tuning():
    print("\n" + "="*60)
    print("TEST 10: Self-Tuning Parameters")
    print("="*60)

    tuner = SelfTuner()
    initial_thresholds = tuner.get_thresholds()

    reports = []
    for i in range(6):
        r = MonitorReport(
            intent_score=0.5 + i * 0.02,
            velocity=0.05 + i * 0.01,
            drifts=[DriftReport(drift_type=DriftType.GOAL, severity=Severity.MEDIUM, score=0.3)]
        )
        reports.append(r)

    tuner.auto_tune(reports)
    new_thresholds = tuner.get_thresholds()

    assert_test("SelfTuning: Thresholds adjusted",
                initial_thresholds != new_thresholds or len(tuner.adjustment_history) > 0,
                f"adjustments={len(tuner.adjustment_history)}")

    assert_test("SelfTuning: Intent threshold in valid range",
                0.5 <= new_thresholds["intent"] <= 0.85,
                f"intent_threshold={new_thresholds['intent']}")


# ===========================================================================
#  TEST 11: Historical Replay
# ===========================================================================

def test_historical_replay():
    print("\n" + "="*60)
    print("TEST 11: Historical Replay")
    print("="*60)

    intent = Intent(description="Replay test", success_criteria=["test"])
    desired = DesiredState(knowledge=1.0)

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["test"])

    for i in range(5):
        engine.set_current_state(State(knowledge=(i + 1) * 0.2))
        engine.evaluate()

    replay = engine.historical_replay
    frames = replay.replay()

    assert_test("Replay: 5 frames recorded",
                len(frames) == 5, f"frames={len(frames)}")

    f3 = replay.get_frame(3)
    assert_test("Replay: Can retrieve frame by step",
                f3 is not None and f3["step"] == 3)

    diff = replay.diff(1, 5)
    assert_test("Replay: Diff between frames works",
                len(diff) > 0, f"diff_keys={list(diff.keys())}")

    summary = replay.summary()
    assert_test("Replay: Summary generates text",
                len(summary) > 50, f"len={len(summary)}")


# ===========================================================================
#  TEST 12: Rich Export
# ===========================================================================

def test_rich_export():
    print("\n" + "="*60)
    print("TEST 12: Rich Export")
    print("="*60)

    report = quick_monitor(
        intent_desc="Export test",
        current_vals={"knowledge": 0.5, "reasoning": 0.3},
        desired_vals={"knowledge": 1.0, "reasoning": 1.0},
    )

    json_str = Exporter.to_json(report)
    assert_test("Export: JSON export works",
                len(json_str) > 100 and "progress_score" in json_str,
                f"len={len(json_str)}")

    csv_str = Exporter.to_csv([report])
    assert_test("Export: CSV export works",
                "progress" in csv_str and "health" in csv_str,
                f"len={len(csv_str)}")

    md_str = Exporter.to_markdown(report)
    assert_test("Export: Markdown export works",
                "| Metric |" in md_str and "Progress Score" in md_str,
                f"len={len(md_str)}")

    html_str = Exporter.to_html(report)
    assert_test("Export: HTML export works",
                "<html>" in html_str and "UPMP" in html_str,
                f"len={len(html_str)}")

    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)

    json_path = os.path.join(export_dir, "test_export_final.json")
    Exporter.to_json(report, json_path)
    assert_test("Export: JSON file saved",
                os.path.exists(json_path), f"path={json_path}")


# ===========================================================================
#  TEST 13: Full Integration Test
# ===========================================================================

def test_full_integration():
    print("\n" + "="*60)
    print("TEST 13: Full Integration Test (Drift + Recovery + All Features)")
    print("="*60)

    intent = Intent(
        description="Master advanced reasoning",
        success_criteria=["solve complex problems", "explain reasoning", "apply to new domains"],
    )

    desired = DesiredState(
        knowledge=0.95, reasoning=0.95, memory=0.90,
        performance=0.90, confidence=0.85, adaptability=0.80,
        creativity=0.75, efficiency=0.85,
    )

    engine = MonitoringEngine(intent=intent, desired=desired)
    engine.set_original_context(["solve", "complex", "problems", "reasoning", "apply", "domains"])

    # Auto-generate milestones
    milestones = engine.milestone_tracker.auto_generate_milestones(desired, num=5)

    # Phase 1: Healthy growth (steps 1-8)
    for i in range(8):
        frac = (i + 1) / 12.0
        s = State(
            knowledge=frac * 0.95, reasoning=frac * 0.90, memory=frac * 0.88,
            performance=frac * 0.85, confidence=frac * 0.80, adaptability=frac * 0.75,
            creativity=frac * 0.70, efficiency=frac * 0.82,
        )
        engine.set_current_state(s)
        engine.evaluate(
            current_behavior_desc="solving complex problems and explaining reasoning",
            retained_context=["solve", "complex", "problems", "reasoning", "apply", "domains"],
        )

    # Checkpoint at step 8
    cp = engine.checkpoint("mid-training")
    assert_test("Integration: Mid-training checkpoint saved",
                cp is not None, f"cp_id={cp.id}")

    # Phase 2: Drift event (step 9)
    drift_state = State(
        knowledge=0.65, reasoning=0.40, memory=0.60,
        performance=0.80, confidence=0.50, adaptability=0.30,
        creativity=0.20, efficiency=0.85,
    )
    engine.set_current_state(drift_state)
    r_drift = engine.evaluate(
        current_behavior_desc="maximizing speed and efficiency over reasoning quality",
        retained_context=["speed", "efficiency", "fast"],
    )

    assert_test("Integration: Goal drift detected",
                any(d.drift_type == DriftType.GOAL for d in r_drift.drifts),
                f"drifts={len(r_drift.drifts)}")

    assert_test("Integration: Context drift detected",
                r_drift.context_integrity < 0.5,
                f"ctx={r_drift.context_integrity:.3f}")

    assert_test("Integration: Alerts raised for drift",
                len(r_drift.alerts) > 0,
                f"alerts={len(r_drift.alerts)}")

    # Phase 3: Recovery (steps 10-12)
    for i in range(3):
        frac = 0.65 + (i + 1) * 0.1
        s = State(
            knowledge=min(0.95, frac * 0.95), reasoning=min(0.95, frac * 0.92),
            memory=min(0.90, frac * 0.89), performance=min(0.90, frac * 0.88),
            confidence=min(0.85, frac * 0.83), adaptability=min(0.80, frac * 0.78),
            creativity=min(0.75, frac * 0.73), efficiency=min(0.85, frac * 0.84),
        )
        engine.set_current_state(s)
        engine.evaluate(
            current_behavior_desc="solving complex problems with deep reasoning and applying to new domains",
            retained_context=["solve", "complex", "problems", "reasoning", "apply", "domains"],
        )

    final_report = engine.report_history[-1]

    assert_test("Integration: Final progress near target",
                final_report.progress_score >= 0.7,
                f"progress={final_report.progress_score:.4f}")

    assert_test("Integration: Intent preserved after recovery",
                final_report.intent_score >= 0.8,
                f"intent={final_report.intent_score:.4f}")

    assert_test("Integration: Convergence status computed",
                final_report.convergence != ConvergenceStatus.UNKNOWN,
                f"convergence={final_report.convergence.value}")

    assert_test("Integration: Trends computed",
                len(final_report.trends) > 0,
                f"trend_dims={len(final_report.trends)}")

    assert_test("Integration: Historical replay has all steps",
                len(engine.historical_replay.frames) >= 10,
                f"frames={len(engine.historical_replay.frames)}")

    assert_test("Integration: Self-tuner adjusted thresholds",
                len(engine.self_tuner.adjustment_history) > 0,
                f"adjustments={len(engine.self_tuner.adjustment_history)}")

    assert_test("Integration: Confidence intervals present",
                len(final_report.confidence_intervals) > 0,
                f"ci_dims={len(final_report.confidence_intervals)}")

    achieved = sum(1 for m in engine.milestone_tracker.status() if m.achieved)
    assert_test("Integration: Some milestones achieved",
                achieved > 0, f"achieved={achieved}/5")

    # Export
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)

    json_path = os.path.join(export_dir, "integration_report_final.json")
    Exporter.to_json(final_report, json_path)

    md_path = os.path.join(export_dir, "integration_report_final.md")
    with open(md_path, "w") as f:
        f.write(Exporter.to_markdown(final_report))

    assert_test("Integration: Export files created",
                os.path.exists(json_path) and os.path.exists(md_path))

    return final_report


# ===========================================================================
#  RUN ALL TESTS
# ===========================================================================

if __name__ == "__main__":
    print(f"\n{'#'*60}")
    print(f"#  UPMP v{upmp.__version__} -- Comprehensive Test Suite")
    print(f"{'#'*60}")

    try:
        test_88x8()
        test_multi_agent()
        test_anomaly_regression()
        test_milestones()
        test_checkpoint_rollback()
        test_time_series()
        test_adaptive_weighting()
        test_dependency_graph()
        test_alert_system()
        test_self_tuning()
        test_historical_replay()
        test_rich_export()
        final = test_full_integration()
    except Exception as e:
        print(f"\n!!! UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()

    # Print summary
    print(f"\n{'='*60}")
    print(f"TEST SUMMARY")
    print(f"{'='*60}")
    s = test_results["summary"]
    print(f"  Total:  {s['total']}")
    print(f"  Passed: {s['passed']}")
    print(f"  Failed: {s['failed']}")
    print(f"  Rate:   {s['passed']/s['total']*100:.1f}%" if s['total'] > 0 else "  N/A")

    # Save results
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
    results_path = os.path.join(export_dir, "test_results_final.json")
    with open(results_path, "w") as f:
        json.dump(test_results, f, indent=2, default=str)
    print(f"\n  Results saved to: {results_path}")

    if 'final' in dir():
        print(f"\n{final.summary()}")

    sys.exit(0 if s['failed'] == 0 else 1)

