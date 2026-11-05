from types import SimpleNamespace

import pytest

from UPMP.flow_graph import (
    ApplicationNavigation,
    ApplicationScreen,
    ApplicationTransition,
    FlowCatalog,
    FlowEdge,
    FlowNode,
    application_flow_graph,
    build_flow_graph,
    flow_scope,
    workflow_flow_graph,
)


def _step(step_id, *, depends_on=(), on_success=None, on_failure=None,
          on_indeterminate=None):
    return SimpleNamespace(
        step_id=step_id,
        action=step_id + "_action",
        description="Description for " + step_id,
        depends_on=depends_on,
        on_success=on_success,
        on_failure=on_failure,
        on_indeterminate=on_indeterminate,
    )


def test_generic_graph_preserves_only_supplied_edges_and_lays_out_merge():
    graph = build_flow_graph(
        [FlowNode("left", "Left"), FlowNode("right", "Right"), FlowNode("merge", "Merge")],
        [FlowEdge("left", "merge", "depends_on", "dependency"),
         FlowEdge("right", "merge", "depends_on", "dependency")],
    )

    rendered = graph.to_dict()

    assert {(edge["source"], edge["target"]) for edge in rendered["edges"]} == {
        ("left", "merge"), ("right", "merge"),
    }
    assert rendered["positions"]["merge"]["x"] > rendered["positions"]["left"]["x"]
    assert rendered["positions"]["merge"]["x"] > rendered["positions"]["right"]["x"]


def test_workflow_graph_uses_declared_routes_and_audit_branch_status():
    steps = [
        _step("check", on_success="next", on_failure="contact",
              on_indeterminate="contact"),
        _step("next", depends_on=("check",)),
        _step("contact"),
    ]
    audit = [SimpleNamespace(
        step_id="check",
        branch_id="check-branch",
        event="action_failed",
        details={"attempt": 2, "message": "command returned 1"},
    )]
    graph = workflow_flow_graph(
        steps,
        audit,
        runtime_steps=[{"id": "check", "status": "failed", "attempts": 2}],
    )

    nodes = {node.details["step_id"] + ":" + node.branch_id: node
             for node in graph.nodes}
    check = nodes["check:check-branch"]
    assert check.status == "failed"
    assert check.attempts == 2
    edges = {(edge.source, edge.target, edge.label) for edge in graph.edges}
    assert (check.id, nodes["contact:default"].id, "on_failure") in edges
    assert (check.id, nodes["contact:default"].id, "on_indeterminate") in edges
    observed = {(edge.source, edge.target, edge.label) for edge in graph.edges if edge.observed}
    assert observed == {(check.id, nodes["contact:default"].id, "on_failure")}
    assert (check.id, nodes["next:default"].id, "on_success") in edges
    assert (check.id, nodes["next:default"].id, "depends_on") in edges
    assert all(target != nodes["contact:default"].id or source == check.id
               for source, target, _ in edges)


def test_audit_branch_transition_marks_the_route_that_actually_ran():
    steps = [_step("check", on_success="next", on_failure="contact"),
             _step("next"), _step("contact")]
    audit = [SimpleNamespace(
        step_id="check",
        branch_id="default",
        event="branch_transition",
        timestamp="2030-01-01T00:00:00+00:00",
        details={"from": "check", "to": "next", "via": "on_success"},
    )]

    graph = workflow_flow_graph(steps, audit)

    observed = {(edge.label, edge.target) for edge in graph.edges if edge.observed}
    assert observed == {("on_success", '["next","default"]')}


def test_multi_branch_routes_never_invent_cross_branch_connections():
    steps = [_step("source", on_success="target"), _step("target")]
    audit = [
        SimpleNamespace(step_id=step_id, branch_id=branch_id,
                        event=("action_started"
                               if step_id == "source" and branch_id == "branch-b"
                               else "step_completed"),
                        details={})
        for step_id in ("source", "target")
        for branch_id in ("branch-a", "branch-b")
    ]
    audit.append(SimpleNamespace(
        step_id="source",
        branch_id="branch-a",
        event="branch_transition",
        timestamp="2030-01-01T00:00:00+00:00",
        details={"from": "source", "to": "target", "via": "on_success"},
    ))

    graph = workflow_flow_graph(steps, audit)
    by_key = {(node.details["step_id"], node.branch_id): node.id
              for node in graph.nodes}
    transitions = {(edge.source, edge.target, edge.label, edge.observed)
                   for edge in graph.edges if edge.kind == "transition"}

    assert transitions == {
        (by_key[("source", "branch-a")], by_key[("target", "branch-a")],
         "on_success", True),
        (by_key[("source", "branch-b")], by_key[("target", "branch-b")],
         "on_success", False),
    }


def test_parallel_branch_ids_connect_to_a_single_declared_destination():
    steps = [_step("source", on_success="target"), _step("target")]
    audit = [
        SimpleNamespace(step_id="source", branch_id="source",
                        event="step_completed", details={}),
        SimpleNamespace(step_id="target", branch_id="target",
                        event="step_completed", details={}),
    ]

    graph = workflow_flow_graph(steps, audit)

    assert {(edge.source, edge.target, edge.label) for edge in graph.edges
            if edge.kind == "transition"} == {
        ('["source","source"]', '["target","target"]', "on_success"),
    }


def test_application_flow_maps_split_rejoin_and_observed_navigation():
    screens = [
        ApplicationScreen("home", "Home", "/", resource_refs=("app/home.py#Home",)),
        ApplicationScreen("search", "Search", "/search"),
        ApplicationScreen("account", "Account", "/account"),
        ApplicationScreen("summary", "Summary", "/summary"),
    ]
    transitions = [
        ApplicationTransition("home", "search", "Search link", "home-search"),
        ApplicationTransition("home", "account", "Account link", "home-account"),
        ApplicationTransition("search", "summary", "Open summary", "search-summary"),
        ApplicationTransition("account", "summary", "Return to summary", "account-summary"),
    ]
    graph = application_flow_graph(
        screens,
        transitions,
        observed_navigation=[
            ApplicationNavigation("home", "account", "Account link", "home-account",
                                  "2030-01-01T00:00:00+00:00", {"source": "browser"}),
            ApplicationNavigation("account", "summary", "Return to summary",
                                  "account-summary"),
        ],
        current_screen_id="summary",
        entry_screen_ids=("home",),
        exit_screen_ids=("summary",),
    )

    by_screen = {node.details["screen_id"]: node for node in graph.nodes}
    edges = {(edge.source, edge.target, edge.observed) for edge in graph.edges}
    assert edges == {
        (by_screen["home"].id, by_screen["search"].id, False),
        (by_screen["home"].id, by_screen["account"].id, True),
        (by_screen["search"].id, by_screen["summary"].id, False),
        (by_screen["account"].id, by_screen["summary"].id, True),
    }
    assert by_screen["summary"].status == "running"
    assert by_screen["account"].details["visit_count"] == 1
    assert graph.entry_ids == (by_screen["home"].id,)
    assert graph.exit_ids == (by_screen["summary"].id,)
    assert graph.to_dict()["positions"][by_screen["summary"].id]["x"] > graph.to_dict()["positions"][by_screen["search"].id]["x"]

    catalog = FlowCatalog()
    catalog.register("sample-app", graph)
    assert catalog.locate("sample-app", resource_ref="app/home.py#Home").id == by_screen["home"].id


def test_unmapped_application_navigation_is_visible_as_an_indeterminate_route():
    graph = application_flow_graph(
        [ApplicationScreen("home", "Home", "/")],
        [],
        observed_navigation=[ApplicationNavigation(
            "home", "new-checkout", "Checkout link",
            timestamp="2030-01-01T00:00:00+00:00",
        )],
        entry_screen_ids=("home",),
    )

    home = next(node for node in graph.nodes if node.details.get("screen_id") == "home")
    unmapped = next(node for node in graph.nodes
                    if node.details.get("screen_id") == "new-checkout")
    observed = next(edge for edge in graph.edges if edge.observed)
    assert home.status == "completed"
    assert unmapped.status == "indeterminate"
    assert observed.kind == "unmapped_navigation"
    assert observed.source == home.id
    assert observed.target == unmapped.id
    assert "missing from the declared application map" in unmapped.details["issue"]


def test_flow_scope_keeps_external_boundary_ports_for_focused_handoff():
    graph = build_flow_graph(
        [FlowNode("start", "Start"), FlowNode("payment", "Payment"),
         FlowNode("receipt", "Receipt")],
        [FlowEdge("start", "payment", "on_success", "transition", observed=True),
         FlowEdge("payment", "receipt", "on_success", "transition")],
        entry_ids=("start",),
    )

    scope = flow_scope(graph, ["payment"], direction="selection", max_depth=0)

    assert [node.id for node in scope.graph.nodes] == ["payment"]
    assert [(edge.source, edge.target) for edge in scope.incoming] == [("start", "payment")]
    assert [(edge.source, edge.target) for edge in scope.outgoing] == [("payment", "receipt")]
    assert {node.id for node in scope.external_nodes} == {"start", "receipt"}


def test_catalog_persists_map_and_resolves_exact_resource_reference(tmp_path):
    graph = build_flow_graph(
        [FlowNode("a", "Payment", details={"resource_refs": ["src/payment.py#A"]}),
         FlowNode("b", "Payment", details={"resource_refs": ["src/payment.py#B"]})],
        [],
    )
    catalog = FlowCatalog()
    catalog.register("checkout", graph, metadata={"revision": "rev-17"})
    path = tmp_path / "process-map.json"

    catalog.save(path)
    loaded = FlowCatalog.load(path)

    assert loaded.process_ids == ("checkout",)
    assert loaded.locate("checkout", resource_ref="src/payment.py#B").id == "b"
    with pytest.raises(LookupError, match="ambiguous"):
        loaded.locate("checkout", label="Payment")
