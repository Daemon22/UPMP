from examples.live_browser_process import (
    ALL_STEP_IDS,
    CONTACT_STEP_ID,
    LiveState,
    PAGE,
    STEP_IDS,
    _failure_summary,
    _workflow_steps,
)


def test_failure_summary_keeps_all_ten_pytest_names():
    names = ["tests/test_process_preemption.py::Test%d::test_failure" % index
             for index in range(10)]
    output = "\n".join("FAILED %s - AssertionError" % name for name in names)
    output += "\n10 failed, 31 skipped in 2.00s\n"

    report = _failure_summary(output, 1)

    assert "reports 10 failed; listed 10" in report
    for name in names:
        assert name in report


def test_failure_summary_explicitly_reports_count_name_mismatch():
    output = "\n".join(
        "FAILED tests/test_process_preemption.py::Test%d::test_failure - AssertionError"
        % index
        for index in range(9)
    )
    output += "\n10 failed, 31 skipped in 2.00s\n"

    report = _failure_summary(output, 1)

    assert "INCOMPLETE FAILURE REPORT" in report
    assert "pytest reports 10 failed, but only 9 failure names were captured" in report


def test_live_page_graph_has_declared_failure_paths_to_user_contact():
    steps = {step.step_id: step for step in _workflow_steps()}
    assert tuple(steps) == ALL_STEP_IDS
    for step_id in STEP_IDS:
        assert steps[step_id].on_failure == CONTACT_STEP_ID
        assert steps[step_id].on_indeterminate == CONTACT_STEP_ID

    snapshot = LiveState().snapshot()
    nodes = {node["details"]["step_id"]: node for node in snapshot["graph"]["nodes"]}
    contact = nodes[CONTACT_STEP_ID]
    routes = [edge for edge in snapshot["graph"]["edges"]
              if edge["target"] == contact["id"]]
    assert sum(edge["label"] == "on_failure" for edge in routes) == len(STEP_IDS)
    assert sum(edge["label"] == "on_indeterminate" for edge in routes) == len(STEP_IDS)
    assert '<svg id="flow"' in PAGE
    assert 'fetch("/direction"' in PAGE


def test_live_page_can_inspect_application_screens_and_unmapped_routes():
    assert 'details.node_type' in PAGE
    assert 'appendDetail("Route"' in PAGE
    assert 'appendDetail("Source references"' in PAGE
    assert 'edge.kind === "unmapped_navigation"' in PAGE
    assert '.edge.unmapped_navigation' in PAGE
