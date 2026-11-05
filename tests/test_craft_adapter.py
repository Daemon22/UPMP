import json

import pytest

from UPMP.craft_adapter import craft_progress_flow_graph, read_craft_progress_jsonl


def _event(operation_id, operation, step, status, *, details=None, timestamp=None):
    return {
        "version": 1,
        "operation_id": operation_id,
        "operation": operation,
        "step": step,
        "status": status,
        "timestamp": timestamp or "2030-01-01T00:00:00+00:00",
        "details": details or {},
    }


def _lifecycle(operation_id, operation, steps, *, operation_details=None):
    events = [_event(operation_id, operation, "operation", "started",
                     details=operation_details)]
    for step, details in steps:
        events.append(_event(operation_id, operation, step, "started", details=details))
        events.append(_event(operation_id, operation, step, "completed", details=details))
    events.append(_event(operation_id, operation, "operation", "completed"))
    return events


def _nodes(graph):
    return {node.details.get("step_id", node.label): node for node in graph.nodes}


def test_craft_nano_graph_highlights_encrypted_and_self_verified_route():
    events = _lifecycle(
        "nano-run",
        "nano",
        [
            ("validate_input", {"input_size_bytes": 64}),
            ("compress", {"compression_mode": "7fold", "compression_strategy": "brotli"}),
            ("derive_keys", {"iterations": 600000}),
            ("prepare_metadata", {"metadata_encrypted": True}),
            ("encrypt_metadata", {"metadata_encrypted": True}),
            ("encrypt_payload", {"algorithm": "AES-256-GCM"}),
            ("self_verify", {}),
            ("self_verify.parse_package", {"metadata_encrypted": True}),
            ("self_verify.decrypt_metadata", {"metadata_encrypted": True}),
            ("self_verify.decrypt_payload", {"algorithm": "AES-256-GCM"}),
            ("self_verify.decompress", {"compression_mode": "7fold"}),
            ("self_verify.verify_integrity", {"algorithm": "SHA-256"}),
        ],
        operation_details={"metadata_encrypted": True, "operation_mode": "verify_enabled"},
    )

    graph = craft_progress_flow_graph(events)
    nodes = _nodes(graph)
    observed = {(edge.source, edge.target, edge.label)
                for edge in graph.edges if edge.observed}

    assert nodes["compress"].status == "completed"
    assert nodes["self_verify.verify_integrity"].status == "completed"
    assert nodes["finish"].status == "completed"
    assert (nodes["prepare_metadata"].id, nodes["encrypt_metadata"].id,
            "metadata_encrypted") in observed
    assert (nodes["encrypt_payload"].id, nodes["self_verify"].id,
            "verify_enabled") in observed
    assert graph.entry_ids == (nodes["start"].id,)
    assert graph.exit_ids == (nodes["finish"].id,)


def test_craft_nano_graph_marks_metadata_and_verification_bypasses_as_skipped():
    events = _lifecycle(
        "nano-run-plain",
        "nano",
        [
            ("validate_input", {}),
            ("compress", {"compression_mode": "brotli"}),
            ("derive_keys", {}),
            ("prepare_metadata", {"metadata_encrypted": False}),
            ("encrypt_payload", {"algorithm": "AES-256-GCM"}),
        ],
        operation_details={"metadata_encrypted": False, "operation_mode": "verify_disabled"},
    )

    graph = craft_progress_flow_graph(events)
    nodes = _nodes(graph)
    observed = {(edge.source, edge.target, edge.label)
                for edge in graph.edges if edge.observed}

    assert nodes["encrypt_metadata"].status == "skipped"
    assert nodes["self_verify"].status == "skipped"
    assert nodes["self_verify.verify_integrity"].status == "skipped"
    assert (nodes["prepare_metadata"].id, nodes["encrypt_payload"].id,
            "metadata_plaintext") in observed
    assert (nodes["encrypt_payload"].id, nodes["finish"].id,
            "verify_disabled") in observed


def test_craft_macro_plaintext_metadata_selects_direct_payload_key_path():
    events = _lifecycle(
        "macro-plain",
        "macro",
        [
            ("validate_and_parse_package", {"metadata_encrypted": False}),
            ("derive_payload_key", {}),
            ("decrypt_payload", {"algorithm": "AES-256-GCM"}),
            ("decompress", {"compression_mode": "7fold"}),
            ("verify_integrity", {"algorithm": "SHA-256"}),
        ],
    )

    graph = craft_progress_flow_graph(events)
    nodes = _nodes(graph)
    observed = {(edge.source, edge.target, edge.label)
                for edge in graph.edges if edge.observed}

    assert nodes["derive_metadata_key"].status == "skipped"
    assert nodes["decrypt_metadata"].status == "skipped"
    assert nodes["verify_integrity"].status == "completed"
    assert (nodes["validate_and_parse_package"].id, nodes["derive_payload_key"].id,
            "metadata_plaintext") in observed


def test_craft_failed_stage_routes_to_the_finish_node_and_keeps_only_safe_findings():
    events = [
        _event("failed-run", "macro", "operation", "started"),
        _event("failed-run", "macro", "validate_and_parse_package", "started",
               details={"package_size_bytes": 120}),
        _event("failed-run", "macro", "validate_and_parse_package", "failed",
               details={"error_type": "Error", "password": "do-not-copy"}),
        _event("failed-run", "macro", "operation", "failed",
               details={"error_type": "Error", "message": "do-not-copy"}),
    ]

    graph = craft_progress_flow_graph(events)
    nodes = _nodes(graph)
    observed_failure = next(edge for edge in graph.edges
                            if edge.observed and edge.label == "on_failure")

    assert nodes["validate_and_parse_package"].status == "failed"
    assert nodes["finish"].status == "failed"
    assert observed_failure.source == nodes["validate_and_parse_package"].id
    assert all("do-not-copy" not in json.dumps(node.to_dict()) for node in graph.nodes)


def test_craft_jsonl_reader_ignores_incomplete_tail_and_rejects_bad_records(tmp_path):
    path = tmp_path / "progress.jsonl"
    path.write_text(
        json.dumps(_event("run", "nano", "operation", "started")) + "\n"
        + '{"version":',
        encoding="utf-8",
    )

    events = read_craft_progress_jsonl(path)

    assert len(events) == 1
    assert events[0]["step"] == "operation"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid CRAFT progress JSON"):
        read_craft_progress_jsonl(path)


def test_craft_stream_requires_an_operation_id_when_it_contains_multiple_runs():
    events = [
        _event("run-a", "nano", "operation", "started"),
        _event("run-b", "macro", "operation", "started"),
    ]

    with pytest.raises(ValueError, match="select one operation_id"):
        craft_progress_flow_graph(events)

    graph = craft_progress_flow_graph(events, operation_id="run-a")
    assert len({node.details.get("operation_id") for node in graph.nodes}) == 1
