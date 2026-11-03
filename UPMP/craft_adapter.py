"""Bridge CRAFT Engine JSONL progress events into UPMP flow graphs.

The adapter consumes only CRAFT's versioned, content-free progress stream. It
does not run Craft Engine or retain artifacts or credentials; the caller owns
those responsibilities and can later connect Vault and Keeper of Secrets.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .flow_graph import FlowEdge, FlowGraph, FlowNode, build_flow_graph


_SAFE_DETAIL_KEYS = frozenset({
    "algorithm",
    "compression_mode",
    "compression_strategy",
    "error_type",
    "input_size_bytes",
    "iterations",
    "metadata_encrypted",
    "operation_mode",
    "package_payload_size_bytes",
    "package_size_bytes",
    "restored_size_bytes",
    "strategy_number",
})
_STATUSES = frozenset({"started", "completed", "failed"})
_OPERATIONS = frozenset({"nano", "macro"})


def read_craft_progress_jsonl(path: Any, *, max_bytes: int = 10_000_000
                              ) -> Tuple[Mapping[str, Any], ...]:
    """Read complete, validated CRAFT progress records from a JSONL file.

    An unfinished trailing line is ignored so a monitor can poll while CRAFT
    is appending to the file. The size limit bounds accidental oversized logs.
    """
    source = Path(path)
    size = source.stat().st_size
    if size > max_bytes:
        raise ValueError("CRAFT progress file exceeds the configured size limit")
    try:
        contents = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("CRAFT progress file must be UTF-8 JSONL") from exc
    lines = contents.splitlines()
    if contents and not contents.endswith(("\n", "\r")):
        lines = lines[:-1]
    events = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid CRAFT progress JSON on line %d" % line_number) from exc
        events.append(_validate_event(raw, line_number))
    return tuple(events)


def craft_progress_flow_graph(
        events: Iterable[Mapping[str, Any]],
        *, operation_id: Optional[str] = None) -> FlowGraph:
    """Build the declared CRAFT pipeline and highlight the recorded route.

    The graph shows optional metadata encryption and self-verification paths,
    plus the nested parse/decrypt/decompress/check steps used for verification.
    A failed stage routes to the end node through an observed failure edge.
    """
    checked = tuple(_validate_event(event, index + 1) for index, event in enumerate(events))
    if not checked:
        raise ValueError("at least one CRAFT progress event is required")
    run_ids = {event["operation_id"] for event in checked}
    if operation_id is None:
        if len(run_ids) != 1:
            raise ValueError("select one operation_id when the progress stream has multiple runs")
        operation_id = next(iter(run_ids))
    selected = tuple(event for event in checked if event["operation_id"] == operation_id)
    if not selected:
        raise KeyError("unknown CRAFT operation_id: %s" % operation_id)
    operations = {event["operation"] for event in selected}
    if len(operations) != 1:
        raise ValueError("one CRAFT operation_id cannot describe multiple operation types")
    operation = next(iter(operations))

    stage_details = {}
    stage_attempts = {}
    stage_statuses = {}
    stage_history = {}
    operation_status = "pending"
    operation_details = {}
    lifecycle_started = False
    for event in selected:
        step = event["step"]
        status = event["status"]
        details = dict(event.get("details", {}))
        if step == "operation":
            if status == "started":
                lifecycle_started = True
                operation_status = "running"
                operation_details.update(details)
            elif status in ("completed", "failed"):
                operation_status = status
                operation_details.update(details)
            stage_history.setdefault(step, []).append(event)
            continue
        history = stage_history.setdefault(step, [])
        history.append(event)
        if status == "started":
            stage_attempts[step] = stage_attempts.get(step, 0) + 1
            stage_statuses[step] = "running"
        elif status in ("completed", "failed"):
            stage_statuses[step] = status
        stage_details.setdefault(step, {}).update(details)

    steps, transitions, optional_skips = _pipeline(operation, stage_details, operation_details)
    for step in optional_skips:
        if step not in stage_statuses:
            stage_statuses[step] = "skipped"

    all_steps = tuple(dict.fromkeys(steps + tuple(stage_history.keys())))
    nodes_by_step = {}
    nodes = []
    for step in all_steps:
        if step == "operation":
            continue
        status = stage_statuses.get(step, "pending")
        node_id = _node_id(operation, step)
        history = stage_history.get(step, ())
        details = {
            "node_type": "craft_stage",
            "operation": operation,
            "step_id": step,
            "operation_id": operation_id,
            "events": [
                {"status": event["status"], "timestamp": event["timestamp"],
                 "duration_ms": event.get("duration_ms"),
                 "details": dict(event.get("details", {}))}
                for event in history
            ],
            "findings": ([stage_details[step].get("error_type")]
                         if status == "failed" else []),
        }
        nodes_by_step[step] = node_id
        nodes.append(FlowNode(
            node_id,
            _step_label(step),
            status,
            stage_attempts.get(step, 0),
            details=details,
        ))

    start_id = _node_id(operation, "start")
    finish_id = _node_id(operation, "finish")
    nodes.extend((
        FlowNode(start_id, "Start " + operation, "completed" if lifecycle_started else "pending",
                 details={"node_type": "craft_boundary", "step_id": "start",
                          "operation_id": operation_id}),
        FlowNode(finish_id, "Finish " + operation, operation_status,
                 details={"node_type": "craft_boundary", "step_id": "finish",
                          "operation_id": operation_id,
                          "operation_status": operation_status}),
    ))
    nodes_by_step["start"] = start_id
    nodes_by_step["finish"] = finish_id

    declared_edges = {}
    for source, target, label in transitions:
        edge = FlowEdge(
            nodes_by_step[source],
            nodes_by_step[target],
            label,
            "transition",
        )
        declared_edges[(source, target, label)] = edge
    for step in steps:
        if step in nodes_by_step and step not in ("start", "finish"):
            key = (step, "finish", "on_failure")
            declared_edges[key] = FlowEdge(
                nodes_by_step[step], nodes_by_step["finish"],
                "on_failure", "transition",
            )

    observed = {}
    started_sequence = [
        event["step"] for event in selected
        if event["status"] == "started" and event["step"] != "operation"
    ]
    if lifecycle_started:
        started_sequence.insert(0, "start")
    for source, target in zip(started_sequence, started_sequence[1:]):
        candidates = [key for key in declared_edges if key[0] == source and key[1] == target]
        if candidates:
            key = next((candidate for candidate in candidates
                        if candidate[2] not in ("on_failure", "verify_disabled", "metadata_plaintext")),
                       candidates[0])
            observed[key] = _event_evidence_for_step(stage_history.get(target, ()))

    if operation_status in ("completed", "failed"):
        if started_sequence:
            last_step = started_sequence[-1]
            if operation_status == "failed":
                key = (last_step, "finish", "on_failure")
                if key in declared_edges:
                    observed[key] = _event_evidence_for_step(stage_history.get(last_step, ()))
            else:
                candidates = [key for key in declared_edges
                              if key[0] == last_step and key[1] == "finish"
                              and key[2] != "on_failure"]
                if len(candidates) == 1:
                    key = candidates[0]
                    observed[key] = _event_evidence_for_step(stage_history.get(last_step, ()))
        elif operation_status == "failed":
            failure_key = ("start", "finish", "on_failure")
            if failure_key not in declared_edges:
                declared_edges[failure_key] = FlowEdge(
                    start_id, finish_id, "on_failure", "transition",
                )
            observed[failure_key] = {"operation_id": operation_id, "status": "failed"}

    edges = []
    for key, edge in declared_edges.items():
        if key in observed:
            evidence = observed[key]
            edges.append(FlowEdge(
                edge.source, edge.target, edge.label, edge.kind, True,
                {"operation_id": operation_id, **evidence},
            ))
        else:
            edges.append(edge)

    return build_flow_graph(nodes, edges, entry_ids=(start_id,), exit_ids=(finish_id,))


def _validate_event(raw: Any, line_number: int) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("CRAFT progress line %d must contain a JSON object" % line_number)
    if raw.get("version") != 1:
        raise ValueError("unsupported CRAFT progress version on line %d" % line_number)
    operation_id = raw.get("operation_id")
    operation = raw.get("operation")
    step = raw.get("step")
    status = raw.get("status")
    timestamp = raw.get("timestamp")
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("CRAFT progress line %d has no operation_id" % line_number)
    if operation not in _OPERATIONS:
        raise ValueError("CRAFT progress line %d has an unknown operation" % line_number)
    if not isinstance(step, str) or not step or len(step) > 120:
        raise ValueError("CRAFT progress line %d has an invalid step" % line_number)
    if status not in _STATUSES:
        raise ValueError("CRAFT progress line %d has an invalid status" % line_number)
    if not isinstance(timestamp, str):
        raise ValueError("CRAFT progress line %d has no timestamp" % line_number)
    safe_details = {}
    raw_details = raw.get("details", {})
    if raw_details is not None:
        if not isinstance(raw_details, Mapping):
            raise ValueError("CRAFT progress line %d has invalid details" % line_number)
        for key, value in raw_details.items():
            if key not in _SAFE_DETAIL_KEYS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_details[key] = value
    duration_ms = raw.get("duration_ms")
    if duration_ms is not None and (not isinstance(duration_ms, (int, float)) or duration_ms < 0):
        raise ValueError("CRAFT progress line %d has invalid duration_ms" % line_number)
    return {
        "version": 1,
        "operation_id": operation_id,
        "operation": operation,
        "step": step,
        "status": status,
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": safe_details,
    }


def _pipeline(operation: str, details: Mapping[str, Mapping[str, Any]],
              operation_details: Mapping[str, Any]):
    if operation == "nano":
        steps = (
            "validate_input", "compress", "derive_keys", "prepare_metadata",
            "encrypt_metadata", "encrypt_payload", "self_verify",
            "self_verify.parse_package", "self_verify.decrypt_metadata",
            "self_verify.decrypt_payload", "self_verify.decompress",
            "self_verify.verify_integrity",
        )
        edges = [
            ("start", "validate_input", "on_success"),
            ("validate_input", "compress", "on_success"),
            ("compress", "derive_keys", "on_success"),
            ("derive_keys", "prepare_metadata", "on_success"),
            ("prepare_metadata", "encrypt_metadata", "metadata_encrypted"),
            ("prepare_metadata", "encrypt_payload", "metadata_plaintext"),
            ("encrypt_metadata", "encrypt_payload", "on_success"),
            ("encrypt_payload", "self_verify", "verify_enabled"),
            ("encrypt_payload", "finish", "verify_disabled"),
            ("self_verify", "self_verify.parse_package", "on_success"),
            ("self_verify.parse_package", "self_verify.decrypt_metadata", "metadata_encrypted"),
            ("self_verify.parse_package", "self_verify.decrypt_payload", "metadata_plaintext"),
            ("self_verify.decrypt_metadata", "self_verify.decrypt_payload", "on_success"),
            ("self_verify.decrypt_payload", "self_verify.decompress", "on_success"),
            ("self_verify.decompress", "self_verify.verify_integrity", "on_success"),
            ("self_verify.verify_integrity", "finish", "on_success"),
        ]
        skips = []
        prepare = details.get("prepare_metadata", {})
        if prepare.get("metadata_encrypted") is False or operation_details.get("metadata_encrypted") is False:
            skips.append("encrypt_metadata")
        if operation_details.get("operation_mode") == "verify_disabled":
            skips.extend(steps[6:])
        elif details.get("prepare_metadata", {}).get("metadata_encrypted") is False:
            skips.append("self_verify.decrypt_metadata")
        return steps, edges, tuple(skips)

    steps = (
        "validate_and_parse_package", "derive_metadata_key", "decrypt_metadata",
        "derive_payload_key", "decrypt_payload", "decompress", "verify_integrity",
    )
    edges = [
        ("start", "validate_and_parse_package", "on_success"),
        ("validate_and_parse_package", "derive_metadata_key", "metadata_encrypted"),
        ("validate_and_parse_package", "derive_payload_key", "metadata_plaintext"),
        ("derive_metadata_key", "decrypt_metadata", "on_success"),
        ("decrypt_metadata", "derive_payload_key", "on_success"),
        ("derive_payload_key", "decrypt_payload", "on_success"),
        ("decrypt_payload", "decompress", "on_success"),
        ("decompress", "verify_integrity", "on_success"),
        ("verify_integrity", "finish", "on_success"),
    ]
    skips = []
    if details.get("validate_and_parse_package", {}).get("metadata_encrypted") is False:
        skips.extend(("derive_metadata_key", "decrypt_metadata"))
    return steps, edges, tuple(skips)


def _node_id(operation: str, step: str) -> str:
    return json.dumps(["craft", operation, step], ensure_ascii=False, separators=(",", ":"))


def _step_label(step: str) -> str:
    return step.replace("_", " ").replace(".", " · ").title()


def _event_evidence_for_step(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    started = next((event for event in events if event.get("status") == "started"), None)
    return {
        "step": events[-1].get("step") if events else "",
        "timestamp": started.get("timestamp", "") if started else "",
        "status": events[-1].get("status", "") if events else "",
    }
