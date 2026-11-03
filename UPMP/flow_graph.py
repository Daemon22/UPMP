"""Data-driven flow graphs for workflows, screens, and other connected items.

The generic graph API draws only the nodes and edges supplied by its caller.
``workflow_flow_graph`` adapts UPMP workflow declarations and audit evidence
into that API without inventing adjacency between steps.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FlowNode:
    """One item in a flow, such as a process step or application screen."""

    id: str
    label: str
    status: str = "pending"
    attempts: int = 0
    branch_id: str = "default"
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "attempts": self.attempts,
            "branch_id": self.branch_id,
            "details": _json_safe(dict(self.details)),
        }


@dataclass(frozen=True)
class ApplicationScreen:
    """A declared application screen or routed view in an app flow map."""

    screen_id: str
    label: str
    route: str = ""
    resource_refs: Sequence[str] = ()
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicationTransition:
    """A declared navigation path between two application screens."""

    source_screen_id: str
    target_screen_id: str
    label: str = ""
    transition_id: str = ""


@dataclass(frozen=True)
class ApplicationNavigation:
    """One navigation observed by an application connector during a run."""

    source_screen_id: str
    target_screen_id: str
    label: str = ""
    transition_id: str = ""
    timestamp: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowEdge:
    """An explicitly declared connection between two graph nodes."""

    source: str
    target: str
    label: str = ""
    kind: str = "connection"
    observed: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "label": self.label,
            "kind": self.kind,
            "observed": self.observed,
            "evidence": _json_safe(dict(self.evidence)),
        }


@dataclass(frozen=True)
class FlowGraph:
    """A validated graph plus a deterministic, left-to-right layout."""

    nodes: Tuple[FlowNode, ...]
    edges: Tuple[FlowEdge, ...]
    entry_ids: Tuple[str, ...] = ()
    exit_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Mapping[str, Any]:
        layout = layout_flow_graph(self)
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "entries": list(self.entry_ids),
            "exits": list(self.exit_ids),
            "positions": layout["positions"],
            "width": layout["width"],
            "height": layout["height"],
        }


def build_flow_graph(nodes: Iterable[FlowNode], edges: Iterable[FlowEdge],
                     entry_ids: Optional[Iterable[str]] = None,
                     exit_ids: Optional[Iterable[str]] = None) -> FlowGraph:
    """Build a graph and validate its IDs and explicit edge endpoints."""
    node_list = tuple(nodes)
    edge_list = tuple(edges)
    ids = [node.id for node in node_list]
    if len(ids) != len(set(ids)):
        raise ValueError("flow graph node IDs must be unique")
    known = set(ids)
    for edge in edge_list:
        if edge.source not in known or edge.target not in known:
            raise ValueError("flow graph edge references an unknown node")
    incoming = {edge.target for edge in edge_list}
    outgoing = {edge.source for edge in edge_list}
    entries = tuple(entry_ids) if entry_ids is not None else tuple(
        node_id for node_id in ids if node_id not in incoming
    )
    exits = tuple(exit_ids) if exit_ids is not None else tuple(
        node_id for node_id in ids if node_id not in outgoing
    )
    if any(node_id not in known for node_id in entries + exits):
        raise ValueError("flow graph entry or exit references an unknown node")
    return FlowGraph(node_list, edge_list, entries, exits)


def layout_flow_graph(graph: FlowGraph) -> Mapping[str, Any]:
    """Lay out a directed graph left to right, including cycles safely.

    Strongly connected groups share a layer; cross-group edges determine the
    remaining layers. Cyclic and backwards edges remain as supplied and can be
    drawn as feedback curves by the renderer.
    """
    node_ids = sorted(node.id for node in graph.nodes)
    if not node_ids:
        return {"positions": {}, "width": 0, "height": 0}

    adjacency = {node_id: set() for node_id in node_ids}
    for edge in graph.edges:
        adjacency[edge.source].add(edge.target)

    index = 0
    indices = {}
    lowlinks = {}
    stack = []
    on_stack = set()
    components = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in sorted(adjacency[node_id]):
            if target not in indices:
                visit(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[target])
        if lowlinks[node_id] == indices[node_id]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break
            components.append(tuple(sorted(component)))

    for node_id in node_ids:
        if node_id not in indices:
            visit(node_id)

    component_for = {
        node_id: component_index
        for component_index, component in enumerate(components)
        for node_id in component
    }
    component_edges = {i: set() for i in range(len(components))}
    component_indegree = {i: 0 for i in range(len(components))}
    for source in node_ids:
        source_component = component_for[source]
        for target in adjacency[source]:
            target_component = component_for[target]
            if (source_component != target_component
                    and target_component not in component_edges[source_component]):
                component_edges[source_component].add(target_component)
                component_indegree[target_component] += 1

    def component_key(component_index: int) -> Tuple[str, ...]:
        return components[component_index]

    ready = sorted(
        (i for i, degree in component_indegree.items() if degree == 0),
        key=component_key,
    )
    ranks = {i: 0 for i in range(len(components))}
    ordered = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(component_edges[current], key=component_key):
            ranks[target] = max(ranks[target], ranks[current] + 1)
            component_indegree[target] -= 1
            if component_indegree[target] == 0:
                ready.append(target)
                ready.sort(key=component_key)

    layers = {}
    for component_index in ordered:
        layers.setdefault(ranks[component_index], []).extend(components[component_index])
    by_id = {node.id: node for node in graph.nodes}
    positions = {}
    for rank, members in layers.items():
        members.sort(key=lambda node_id: (
            by_id[node_id].branch_id, by_id[node_id].label, node_id,
        ))
        for row, node_id in enumerate(members):
            positions[node_id] = {"x": 48 + rank * 244, "y": 48 + row * 132}

    max_rank = max(layers)
    max_rows = max(len(members) for members in layers.values())
    return {
        "positions": positions,
        "width": 48 + (max_rank + 1) * 244,
        "height": 48 + max_rows * 132,
    }


def workflow_flow_graph(steps: Sequence[Any], audit: Sequence[Any] = (),
                        runtime_steps: Optional[Iterable[Mapping[str, Any]]] = None,
                        diagnosis: Any = None,
                        node_details_by_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
                        entry_step_ids: Optional[Sequence[str]] = None) -> FlowGraph:
    """Adapt workflow declarations, audit, live status, and diagnosis to a graph.

    Connections come only from ``depends_on`` and the three declared outcome
    routes. Audit ``branch_id`` values distinguish observed step instances.
    """
    runtime_by_id = {}
    if isinstance(runtime_steps, Mapping):
        runtime_by_id = {str(key): value for key, value in runtime_steps.items()}
    elif runtime_steps is not None:
        runtime_by_id = {
            str(item["id"]): item for item in runtime_steps if item.get("id")
        }

    step_by_id = {step.step_id: step for step in steps}
    if len(step_by_id) != len(steps):
        raise ValueError("workflow step IDs must be unique to build a flow graph")
    event_groups = {}
    for event in audit:
        step_id = str(getattr(event, "step_id", ""))
        if step_id not in step_by_id:
            continue
        branch_id = str(getattr(event, "branch_id", "default") or "default")
        event_groups.setdefault((step_id, branch_id), []).append(event)

    diagnosis_by_key = {}
    for diagnosis_step in getattr(diagnosis, "steps", ()):
        key = (str(getattr(diagnosis_step, "step_id", "")),
               str(getattr(diagnosis_step, "branch_id", "default") or "default"))
        diagnosis_by_key[key] = diagnosis_step

    branches_by_step = {}
    for step_id in step_by_id:
        branches = {branch for (event_step, branch) in event_groups if event_step == step_id}
        runtime = runtime_by_id.get(step_id, {})
        runtime_branch = str(runtime.get("branch_id", "default") or "default")
        if runtime_branch != "default":
            branches.add(runtime_branch)
        if not branches:
            branches.add("default")
        branches_by_step[step_id] = sorted(branches)

    node_ids = {}
    nodes = []
    observed_routes = {}
    for step_id, step in step_by_id.items():
        runtime = runtime_by_id.get(step_id, {})
        for branch_id in branches_by_step[step_id]:
            node_id = json.dumps([step_id, branch_id], ensure_ascii=False,
                                 separators=(",", ":"))
            node_ids[(step_id, branch_id)] = node_id
            events = event_groups.get((step_id, branch_id), ())
            route = _observed_route(step, events)
            if route is not None and route[1] in branches_by_step:
                target_pairs = _branch_pairs(
                    (branch_id,), branches_by_step[route[1]],
                )
                for _source_branch, target_branch in target_pairs:
                    observed_routes[(step_id, branch_id, route[1],
                                     route[0], target_branch)] = route[2]
            terminal_status = _status_from_events(events)
            diagnosis_step = diagnosis_by_key.get((step_id, branch_id))
            diagnosis_status = None
            if diagnosis_step is not None:
                diagnosis_status = _normalise_status(
                    getattr(getattr(diagnosis_step, "outcome", None), "value",
                            getattr(diagnosis_step, "outcome", "unknown"))
                )
            runtime_status = _normalise_status(runtime.get("status", "pending"))
            status = (runtime_status if runtime_status in
                      {"running", "failed", "indeterminate", "skipped", "cancelled"}
                      else diagnosis_status or terminal_status or runtime_status)
            attempts = _attempt_count(events)
            if diagnosis_step is not None:
                attempts = max(attempts, int(getattr(diagnosis_step, "attempts", 0) or 0))
            attempts = max(attempts, int(runtime.get("attempts", 0) or 0))
            findings = []
            if diagnosis is not None:
                findings = [
                    issue.to_dict() if hasattr(issue, "to_dict") else dict(issue)
                    for issue in getattr(diagnosis_step, "issues", ())
                ] if diagnosis_step is not None else []
            details = {
                "step_id": step_id,
                "action": step.action,
                "description": getattr(step, "description", ""),
                "command": runtime.get("command", ""),
                "output": runtime.get("output", ""),
                "findings": findings,
                "exit_code": runtime.get("exit_code"),
                "pid": runtime.get("pid"),
            }
            details.update((node_details_by_id or {}).get(step_id, {}))
            label = step_id.replace("_", " ").title()
            nodes.append(FlowNode(node_id, label, status, attempts, branch_id, details))

    edges = []

    def add_declared_edge(source_step: str, target_step: str,
                          label: str, kind: str) -> None:
        if source_step not in branches_by_step or target_step not in branches_by_step:
            raise ValueError(
                "declared %s edge references a missing workflow step: %s -> %s"
                % (label, source_step, target_step)
            )
        branch_pairs = _branch_pairs(
            branches_by_step[source_step], branches_by_step[target_step],
        )
        for source_branch, target_branch in branch_pairs:
            route_key = (source_step, source_branch, target_step, label, target_branch)
            route_event = observed_routes.get(route_key) if kind == "transition" else None
            edges.append(FlowEdge(
                node_ids[(source_step, source_branch)],
                node_ids[(target_step, target_branch)],
                label,
                kind,
                observed=route_event is not None,
                evidence=_route_evidence(route_event) if route_event is not None else {},
            ))

    for step in steps:
        for dependency in getattr(step, "depends_on", ()):
            add_declared_edge(str(dependency), step.step_id, "depends_on", "dependency")
        for attribute, label in (
            ("on_success", "on_success"),
            ("on_failure", "on_failure"),
            ("on_indeterminate", "on_indeterminate"),
        ):
            target = getattr(step, attribute, None)
            if target:
                add_declared_edge(step.step_id, str(target), label, "transition")

    unique_edges = {}
    for edge in edges:
        unique_edges[(edge.source, edge.target, edge.label, edge.kind)] = edge
    entry_steps = tuple(entry_step_ids) if entry_step_ids is not None else (
        (steps[0].step_id,) if steps else ()
    )
    missing_entries = [step_id for step_id in entry_steps if step_id not in branches_by_step]
    if missing_entries:
        raise ValueError("entry step does not exist: %s" % ", ".join(missing_entries))
    entry_nodes = tuple(
        node_ids[(step_id, branch_id)]
        for step_id in entry_steps if step_id in branches_by_step
        for branch_id in branches_by_step[step_id]
    )
    terminal_steps = tuple(
        step.step_id for step in steps
        if any(getattr(step, route, None) is None for route in
               ("on_success", "on_failure", "on_indeterminate"))
    )
    exit_nodes = tuple(
        node_ids[(step_id, branch_id)]
        for step_id in terminal_steps if step_id in branches_by_step
        for branch_id in branches_by_step[step_id]
    )
    return build_flow_graph(
        nodes,
        unique_edges.values(),
        entry_ids=entry_nodes,
        exit_ids=exit_nodes,
    )


def application_flow_graph(
        screens: Sequence[ApplicationScreen],
        transitions: Sequence[ApplicationTransition],
        observed_navigation: Iterable[ApplicationNavigation] = (),
        *,
        current_screen_id: Optional[str] = None,
        visited_screen_ids: Iterable[str] = (),
        failed_screen_ids: Iterable[str] = (),
        indeterminate_screen_ids: Iterable[str] = (),
        entry_screen_ids: Optional[Sequence[str]] = None,
        exit_screen_ids: Optional[Sequence[str]] = None) -> FlowGraph:
    """Map declared application screens and audit the route an app connector saw.

    Navigation that has no unique declared path is still shown as an observed,
    ``unmapped_navigation`` edge. Unknown endpoint IDs appear as indeterminate
    nodes so an incomplete application map is visible and can be corrected.
    """
    screen_by_id = {str(screen.screen_id): screen for screen in screens}
    if len(screen_by_id) != len(screens):
        raise ValueError("application screen IDs must be unique")
    if any(not screen_id for screen_id in screen_by_id):
        raise ValueError("application screen IDs cannot be empty")

    node_id_by_screen = {
        screen_id: json.dumps(["screen", screen_id], ensure_ascii=False,
                              separators=(",", ":"))
        for screen_id in screen_by_id
    }
    declared_by_id = {}
    declared_by_pair = {}
    declared_by_route = {}
    for transition in transitions:
        source = str(transition.source_screen_id)
        target = str(transition.target_screen_id)
        label = str(transition.label or "navigate")
        transition_id = str(transition.transition_id or "")
        if source not in screen_by_id or target not in screen_by_id:
            raise ValueError(
                "application transition references a missing screen: %s -> %s"
                % (source, target)
            )
        route_key = (source, target, label)
        if route_key in declared_by_route:
            raise ValueError("application transitions must have unique endpoints and labels")
        record = (transition, source, target, label, transition_id)
        declared_by_route[route_key] = record
        declared_by_pair.setdefault((source, target), []).append(record)
        if transition_id:
            if transition_id in declared_by_id:
                raise ValueError("application transition IDs must be unique")
            declared_by_id[transition_id] = record

    def check_known_ids(values: Iterable[str], name: str) -> Tuple[str, ...]:
        result = tuple(dict.fromkeys(str(value) for value in values))
        missing = [value for value in result if value not in screen_by_id]
        if missing:
            raise ValueError("%s references unknown screens: %s" % (name, ", ".join(missing)))
        return result

    visited = set(check_known_ids(visited_screen_ids, "visited_screen_ids"))
    failed = set(check_known_ids(failed_screen_ids, "failed_screen_ids"))
    indeterminate = set(check_known_ids(indeterminate_screen_ids,
                                        "indeterminate_screen_ids"))
    if current_screen_id is not None:
        current_screen_id = str(current_screen_id)

    observed_records = {}
    unmapped_records = {}
    visit_counts: Dict[str, int] = {}
    seen_in_run = set()
    unknown_screen_ids = set()
    unmapped_targets = set()
    if current_screen_id is not None:
        if current_screen_id not in screen_by_id:
            unknown_screen_ids.add(current_screen_id)
            visit_counts[current_screen_id] = 1
        else:
            visited.add(current_screen_id)

    for observation in observed_navigation:
        source = str(_application_value(observation, "source_screen_id", "") or "")
        target = str(_application_value(observation, "target_screen_id", "") or "")
        label = str(_application_value(observation, "label", "") or "")
        transition_id = str(_application_value(observation, "transition_id", "") or "")
        timestamp = str(_application_value(observation, "timestamp", "") or "")
        evidence = _application_value(observation, "evidence", {}) or {}
        if not source or not target:
            raise ValueError("observed application navigation needs source and target screen IDs")

        if source not in screen_by_id:
            unknown_screen_ids.add(source)
        if target not in screen_by_id:
            unknown_screen_ids.add(target)
        if source not in seen_in_run:
            visit_counts[source] = visit_counts.get(source, 0) + 1
        visit_counts[target] = visit_counts.get(target, 0) + 1
        seen_in_run.update((source, target))
        visited.update((source, target))

        if transition_id:
            candidate = declared_by_id.get(transition_id)
            matches = ([candidate] if candidate is not None
                       and candidate[1] == source and candidate[2] == target else [])
        elif label:
            candidate = declared_by_route.get((source, target, label))
            matches = [candidate] if candidate is not None else []
        else:
            matches = declared_by_pair.get((source, target), [])

        observation_evidence = {
            "timestamp": timestamp,
            "transition_id": transition_id,
            "label": label,
            "details": _json_safe(dict(evidence)) if isinstance(evidence, Mapping)
                       else _json_safe(evidence),
        }
        if len(matches) == 1:
            route = matches[0]
            observed_records.setdefault((route[1], route[2], route[3]), []).append(
                observation_evidence,
            )
        else:
            reason = ("ambiguous declared navigation" if len(matches) > 1
                      else "observed navigation is not declared")
            unmapped_targets.add(target)
            route_label = label or "Observed navigation"
            unmapped_records.setdefault((source, target, route_label), []).append({
                **observation_evidence,
                "issue": reason,
            })

    nodes = []
    for screen_id, screen in screen_by_id.items():
        status = "pending"
        if screen_id in visited:
            status = "completed"
        if screen_id == current_screen_id:
            status = "running"
        if screen_id in unmapped_targets or screen_id in indeterminate:
            status = "indeterminate"
        if screen_id in failed:
            status = "failed"
        details = {
            "node_type": "application_screen",
            "screen_id": screen_id,
            "route": str(screen.route or ""),
            "resource_refs": [str(ref) for ref in screen.resource_refs],
            "visit_count": visit_counts.get(screen_id, 0),
            "metadata": _json_safe(dict(screen.details)),
        }
        nodes.append(FlowNode(
            node_id_by_screen[screen_id],
            str(screen.label or screen_id),
            status,
            visit_counts.get(screen_id, 0),
            details=details,
        ))

    for screen_id in sorted(unknown_screen_ids):
        node_id = json.dumps(["unmapped_screen", screen_id], ensure_ascii=False,
                             separators=(",", ":"))
        nodes.append(FlowNode(
            node_id,
            "Unmapped screen: " + screen_id,
            "indeterminate",
            visit_counts.get(screen_id, 0),
            details={
                "node_type": "unknown_application_screen",
                "screen_id": screen_id,
                "route": screen_id,
                "visit_count": visit_counts.get(screen_id, 0),
                "issue": "Observed screen is missing from the declared application map.",
            },
        ))
        node_id_by_screen[screen_id] = node_id

    edges = []
    for route_key, (transition, source, target, label, transition_id) in declared_by_route.items():
        evidence = observed_records.get(route_key, ())
        edges.append(FlowEdge(
            node_id_by_screen[source],
            node_id_by_screen[target],
            label,
            "navigation",
            observed=bool(evidence),
            evidence={
                "transition_id": transition_id,
                "observations": list(evidence),
                "occurrences": len(evidence),
            } if evidence else {},
        ))

    for (source, target, label), evidence in unmapped_records.items():
        edges.append(FlowEdge(
            node_id_by_screen[source],
            node_id_by_screen[target],
            label,
            "unmapped_navigation",
            observed=True,
            evidence={"observations": list(evidence), "occurrences": len(evidence)},
        ))

    entry_nodes = None
    if entry_screen_ids is not None:
        entries = check_known_ids(entry_screen_ids, "entry_screen_ids")
        entry_nodes = tuple(node_id_by_screen[screen_id] for screen_id in entries)
    exit_nodes = None
    if exit_screen_ids is not None:
        exits = check_known_ids(exit_screen_ids, "exit_screen_ids")
        exit_nodes = tuple(node_id_by_screen[screen_id] for screen_id in exits)
    return build_flow_graph(nodes, edges, entry_ids=entry_nodes, exit_ids=exit_nodes)


def _application_value(observation: Any, name: str, default: Any) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(name, default)
    return getattr(observation, name, default)


def _branch_pairs(source_branches: Sequence[str],
                  target_branches: Sequence[str]) -> Tuple[Tuple[str, str], ...]:
    """Connect matching branches or a uniquely identified destination.

    Branch IDs describe execution instances, not permission to invent a
    complete bipartite graph. When both sides have several unmatched branch
    IDs, their relationship is unknown and the adapter leaves it unmapped.
    """
    shared = sorted(set(source_branches).intersection(target_branches))
    if shared:
        return tuple((branch_id, branch_id) for branch_id in shared)
    if len(target_branches) == 1:
        return tuple((source_branch, target_branches[0])
                     for source_branch in source_branches)
    return ()


def _normalise_status(status: Any) -> str:
    value = str(getattr(status, "value", status) or "pending").lower()
    return {
        "succeeded": "completed",
        "completed": "completed",
        "failed": "failed",
        "blocked": "indeterminate",
        "indeterminate": "indeterminate",
        "running": "running",
        "skipped": "skipped",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "scheduled": "pending",
        "unknown": "indeterminate",
        "waiting": "pending",
    }.get(value, value)


def _status_from_events(events: Sequence[Any]) -> Optional[str]:
    for event in reversed(events):
        name = str(getattr(event, "event", ""))
        details = getattr(event, "details", {}) or {}
        if name == "postcondition_verified":
            return {
                "pass": "completed",
                "fail": "failed",
                "indeterminate": "indeterminate",
            }.get(str(details.get("status", "indeterminate")), "indeterminate")
        if name == "step_completed":
            return "completed"
        if name == "step_failed":
            reason = str(details.get("reason", ""))
            return "indeterminate" if "indeterminate" in reason else "failed"
        if name == "step_skipped":
            return "skipped"
        if name == "step_cancelled":
            return "cancelled"
        if name == "step_blocked":
            return "indeterminate"
        if name == "action_failed" or name == "action_denied":
            return "failed"
        if name == "action_indeterminate":
            return "indeterminate"
        if name == "action_succeeded":
            return "completed"
        if name == "dispatch_blocked":
            return "indeterminate" if details.get("reason") == "indeterminate" else "failed"
    return None


def _observed_route(step: Any, events: Sequence[Any]):
    """Return the route recorded by audit, or the route implied by its outcome."""
    for event in reversed(events):
        if str(getattr(event, "event", "")) != "branch_transition":
            continue
        details = getattr(event, "details", {}) or {}
        via = details.get("via")
        target = details.get("to")
        if via in ("on_success", "on_failure", "on_indeterminate") and target:
            return via, str(target), event

    for event in reversed(events):
        name = str(getattr(event, "event", ""))
        details = getattr(event, "details", {}) or {}
        if name == "postcondition_verified":
            status = details.get("status")
            if status == "pass":
                return ("on_success", step.on_success, event) if step.on_success else None
            if status == "fail":
                return ("on_failure", step.on_failure, event) if step.on_failure else None
            if status == "indeterminate":
                return ("on_indeterminate", step.on_indeterminate, event) if step.on_indeterminate else None
        if name in ("action_failed", "action_denied"):
            return ("on_failure", step.on_failure, event) if step.on_failure else None
        if name == "action_indeterminate":
            return ("on_indeterminate", step.on_indeterminate, event) if step.on_indeterminate else None
        if name == "dispatch_blocked":
            route = ("on_indeterminate" if details.get("reason") == "indeterminate"
                     else "on_failure")
            target = getattr(step, route, None)
            return (route, target, event) if target else None
        if name == "step_failed":
            reason = str(details.get("reason", ""))
            route = "on_indeterminate" if "indeterminate" in reason else "on_failure"
            target = getattr(step, route, None)
            return (route, target, event) if target else None
        if name == "step_completed":
            return ("on_success", step.on_success, event) if step.on_success else None
    return None


def _route_evidence(event: Any) -> Mapping[str, Any]:
    return {
        "audit_event": str(getattr(event, "event", "")),
        "timestamp": str(getattr(event, "timestamp", "")),
        "branch_id": str(getattr(event, "branch_id", "default") or "default"),
        "details": _json_safe(dict(getattr(event, "details", {}) or {})),
    }


@dataclass(frozen=True)
class FlowScope:
    """A focused process slice with explicit ports to its surrounding flow."""

    graph: FlowGraph
    selected_ids: Tuple[str, ...]
    incoming: Tuple[FlowEdge, ...] = ()
    outgoing: Tuple[FlowEdge, ...] = ()
    external_nodes: Tuple[FlowNode, ...] = ()

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "graph": self.graph.to_dict(),
            "selected_ids": list(self.selected_ids),
            "incoming": [edge.to_dict() for edge in self.incoming],
            "outgoing": [edge.to_dict() for edge in self.outgoing],
            "external_nodes": [node.to_dict() for node in self.external_nodes],
        }


def flow_scope(graph: FlowGraph, selected_ids: Iterable[str],
               direction: str = "both", max_depth: Optional[int] = 1) -> FlowScope:
    """Return a local slice around exact node IDs, retaining boundary ports.

    ``max_depth=1`` gives a selected node and its immediate neighbors. Set it
    to ``None`` to follow the chosen direction to the process boundary.
    """
    if direction not in {"upstream", "downstream", "both", "selection"}:
        raise ValueError("direction must be upstream, downstream, both, or selection")
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth cannot be negative or None")
    node_by_id = {node.id: node for node in graph.nodes}
    selected = tuple(dict.fromkeys(str(node_id) for node_id in selected_ids))
    if not selected:
        raise ValueError("at least one node ID must be selected")
    unknown = [node_id for node_id in selected if node_id not in node_by_id]
    if unknown:
        raise KeyError("unknown flow graph node ID: %s" % ", ".join(unknown))

    included = set(selected)
    frontier = set(selected)
    depth = 0
    while direction != "selection" and frontier and (max_depth is None or depth < max_depth):
        discovered = set()
        for edge in graph.edges:
            if direction in ("upstream", "both") and edge.target in frontier:
                discovered.add(edge.source)
            if direction in ("downstream", "both") and edge.source in frontier:
                discovered.add(edge.target)
        discovered.difference_update(included)
        included.update(discovered)
        frontier = discovered
        depth += 1

    inside_edges = tuple(
        edge for edge in graph.edges if edge.source in included and edge.target in included
    )
    incoming = tuple(
        edge for edge in graph.edges if edge.source not in included and edge.target in included
    )
    outgoing = tuple(
        edge for edge in graph.edges if edge.source in included and edge.target not in included
    )
    external_ids = {edge.source for edge in incoming} | {edge.target for edge in outgoing}
    scope_entries = tuple(node_id for node_id in graph.entry_ids if node_id in included)
    scope_exits = tuple(node_id for node_id in graph.exit_ids if node_id in included)
    if not scope_entries:
        scope_entries = selected
    if not scope_exits:
        scope_exits = selected
    scoped_graph = build_flow_graph(
        (node_by_id[node_id] for node_id in node_by_id if node_id in included),
        inside_edges,
        entry_ids=scope_entries,
        exit_ids=scope_exits,
    )
    return FlowScope(
        scoped_graph,
        selected,
        incoming,
        outgoing,
        tuple(node_by_id[node_id] for node_id in node_by_id if node_id in external_ids),
    )


def flow_graph_from_dict(data: Mapping[str, Any]) -> FlowGraph:
    """Restore a graph previously serialized by ``FlowGraph.to_dict``."""
    nodes = tuple(FlowNode(
        id=str(item["id"]),
        label=str(item.get("label", item["id"])),
        status=str(item.get("status", "pending")),
        attempts=int(item.get("attempts", 0)),
        branch_id=str(item.get("branch_id", "default")),
        details=dict(item.get("details", {})),
    ) for item in data.get("nodes", ()))
    edges = tuple(FlowEdge(
        source=str(item["source"]),
        target=str(item["target"]),
        label=str(item.get("label", "")),
        kind=str(item.get("kind", "connection")),
        observed=bool(item.get("observed", False)),
        evidence=dict(item.get("evidence", {})),
    ) for item in data.get("edges", ()))
    return build_flow_graph(
        nodes,
        edges,
        entry_ids=data.get("entries"),
        exit_ids=data.get("exits"),
    )


class FlowCatalog:
    """Index saved process maps so tools can resolve a stable node directly."""

    FORMAT_VERSION = 1

    def __init__(self) -> None:
        self._processes: Dict[str, Dict[str, Any]] = {}

    @property
    def process_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._processes))

    def register(self, process_id: str, graph: FlowGraph,
                 metadata: Optional[Mapping[str, Any]] = None) -> None:
        process_id = str(process_id).strip()
        if not process_id:
            raise ValueError("process_id is required")
        self._processes[process_id] = {
            "graph": graph,
            "metadata": dict(metadata or {}),
        }

    def get(self, process_id: str) -> FlowGraph:
        try:
            return self._processes[process_id]["graph"]
        except KeyError as exc:
            raise KeyError("unknown process map: %s" % process_id) from exc

    def locate(self, process_id: str, *, node_id: Optional[str] = None,
               label: Optional[str] = None,
               resource_ref: Optional[str] = None) -> FlowNode:
        """Resolve exactly one node by stable ID, exact label, or resource ref."""
        selectors = [node_id is not None, label is not None, resource_ref is not None]
        if sum(selectors) != 1:
            raise ValueError("provide exactly one of node_id, label, or resource_ref")
        graph = self.get(process_id)
        if node_id is not None:
            matches = [node for node in graph.nodes if node.id == node_id]
        elif label is not None:
            query = label.casefold()
            matches = [node for node in graph.nodes if node.label.casefold() == query]
        else:
            matches = [node for node in graph.nodes
                       if resource_ref in _resource_refs(node.details)]
        if not matches:
            raise KeyError("no node matched the requested reference in %s" % process_id)
        if len(matches) > 1:
            raise LookupError(
                "reference is ambiguous in %s; provide a stable node_id" % process_id
            )
        return matches[0]

    def select(self, process_id: str, *, node_id: Optional[str] = None,
               label: Optional[str] = None, resource_ref: Optional[str] = None,
               direction: str = "both", max_depth: Optional[int] = 1) -> FlowScope:
        node = self.locate(
            process_id, node_id=node_id, label=label, resource_ref=resource_ref,
        )
        return flow_scope(self.get(process_id), [node.id], direction, max_depth)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "processes": {
                process_id: {
                    "metadata": _json_safe(value["metadata"]),
                    "graph": value["graph"].to_dict(),
                }
                for process_id, value in sorted(self._processes.items())
            },
        }

    def save(self, path: Any) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FlowCatalog":
        if data.get("format_version") != cls.FORMAT_VERSION:
            raise ValueError("unsupported flow catalog format version")
        catalog = cls()
        for process_id, value in data.get("processes", {}).items():
            catalog.register(
                process_id,
                flow_graph_from_dict(value["graph"]),
                value.get("metadata", {}),
            )
        return catalog

    @classmethod
    def load(cls, path: Any) -> "FlowCatalog":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _resource_refs(details: Mapping[str, Any]) -> Tuple[str, ...]:
    values = []
    for key in ("resource_ref", "source_ref", "resource_refs", "source_refs"):
        value = details.get(key)
        if isinstance(value, str):
            values.append(value)
        elif value is not None:
            values.extend(str(item) for item in value)
    return tuple(values)


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _attempt_count(events: Sequence[Any]) -> int:
    attempts = []
    action_events = []
    for event in events:
        name = str(getattr(event, "event", ""))
        if name.startswith("action_"):
            action_events.append(event)
            details = getattr(event, "details", {}) or {}
            try:
                attempts.append(int(details.get("attempt", 0)))
            except (TypeError, ValueError, AttributeError):
                pass
    return max(attempts, default=len(action_events))
