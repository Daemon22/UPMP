"""Run a small UPMP workflow and watch its actual events in the River Map.

Run from the repository root with:
    python examples/live_process_observatory.py

Open the printed local address while the steps execute. The snapshot updates
after every audit event and once more with the terminal goal verification.
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from UPMP import (
    ActionDispatcher,
    ActionResult,
    ClosedLoopOrchestrator,
    Constraint,
    Observation,
    ProcessCharter,
    ProcessPart,
    ProcessRunTracker,
    ProcessState,
    VerificationPolicy,
    WorkflowStep,
    make_observatory_server,
)


def _state(phase: str) -> ProcessState:
    return ProcessState("example.delivery", "1", {
        "ready": Observation(True, source="delivery-demo"),
        "phase": Observation(phase, source="delivery-demo"),
    })


def _policy(policy_id: str, field: str, expected: object) -> VerificationPolicy:
    return VerificationPolicy(
        policy_id, "1", (Constraint(field, "eq", expected),),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--step-delay", type=float, default=1.0,
                        help="seconds to pause in each demonstration action")
    parser.add_argument("--snapshot", type=Path,
                        default=Path("output/live_process_run.json"))
    args = parser.parse_args()
    if args.step_delay < 0:
        parser.error("--step-delay cannot be negative")

    steps = [
        WorkflowStep(
            "validate", "validate", _policy("ready", "ready", True),
            _policy("validated", "phase", "validated"),
            on_success="enrich", description="Validate the incoming record",
        ),
        WorkflowStep(
            "enrich", "enrich", _policy("ready", "ready", True),
            _policy("enriched", "phase", "enriched"),
            on_success="review", description="Join required reference data",
        ),
        WorkflowStep(
            "review", "review", _policy("ready", "ready", True),
            _policy("reviewed", "phase", "reviewed"),
            on_success="publish", description="Review the assembled evidence",
        ),
        WorkflowStep(
            "publish", "publish", _policy("ready", "ready", True),
            _policy("published", "phase", "published"),
            description="Publish the supported result",
        ),
    ]
    parts = (
        ProcessPart("delivery", "Supported record delivery"),
        ProcessPart("prepare", "Prepare", "delivery", weight=1),
        ProcessPart("validate-part", "Validate record", "prepare", "validate"),
        ProcessPart("assemble", "Assemble & review", "delivery", weight=2),
        ProcessPart("enrich-part", "Join reference data", "assemble", "enrich"),
        ProcessPart("review-part", "Review evidence", "assemble", "review"),
        ProcessPart("release", "Controlled release", "delivery", weight=1),
        ProcessPart("publish-part", "Publish result", "release", "publish"),
    )
    charter = ProcessCharter(
        "supported-record-delivery", "1",
        "Publish a reviewed record with its required reference evidence.",
        _policy("delivery-acceptance", "phase", "published"),
        parts=parts,
    )

    dispatcher = ActionDispatcher([step.action for step in steps])
    for step, phase in zip(steps, ("validated", "enriched", "reviewed", "published")):
        def handler(request, current, expected_phase=phase):
            time.sleep(args.step_delay)
            return ActionResult("succeeded", "observed phase: %s" % expected_phase,
                                state=_state(expected_phase))
        dispatcher.register(step.action, handler)

    initial = _state("received")
    tracker = ProcessRunTracker(charter, steps, initial, args.snapshot)
    tracker.begin()
    ui_root = Path(__file__).resolve().parents[1] / "output" / "upmp-visual-prototype"
    server = make_observatory_server(str(args.snapshot), str(ui_root), args.host, args.port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print("UPMP live River Map: http://%s:%s/" % (host, server.server_address[1]))
    print("Running the declared workflow. Keep this window open; press Ctrl+C to stop the viewer.")

    try:
        result = ClosedLoopOrchestrator(
            steps, dispatcher, audit_sink=tracker,
            audit_metadata=charter.audit_metadata,
        ).run(initial)
        view = tracker.finish(result)
        print("Run: %s | Goal: %s | Verified checkpoints: %s/%s" % (
            view.run_status, view.goal_outcome.value,
            view.passed_checkpoints, view.total_checkpoints,
        ))
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
