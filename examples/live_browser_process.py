"""Run the UPMP repository checks through a real local browser session.

The script serves a small status page on loopback, opens it in the default
browser, and starts each repository check when that page loads. The page polls
the local server for live step status and subprocess output. Nothing is sent to
an external site.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from UPMP import (  # noqa: E402
    ActionDispatcher,
    ActionResult,
    ActionStatus,
    ClosedLoopOrchestrator,
    Constraint,
    Observation,
    ProcessState,
    VerificationPolicy,
    WorkflowStep,
    diagnose_workflow,
)
from UPMP.flow_graph import workflow_flow_graph  # noqa: E402


STEP_IDS = (
    "browser_opened",
    "compile",
    "pytest",
    "scenarios",
    "simulation",
    "diagnostics_example",
)
CONTACT_STEP_ID = "user_contact"
ALL_STEP_IDS = STEP_IDS + (CONTACT_STEP_ID,)
OUTPUT_LIMIT = 24000
PROCESS_TIMEOUT_SECONDS = 300

PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UPMP Live Local Process</title>
  <style>
    :root { color-scheme: light; --ink: #17212b; --muted: #566575; --line: #aab8c5; }
    body { font: 16px/1.5 system-ui, sans-serif; margin: 1.5rem auto; max-width: 1200px; padding: 0 1rem; color: var(--ink); }
    h1 { margin-bottom: .25rem; } h2 { margin: 1.5rem 0 .5rem; }
    .muted { color: var(--muted); }
    #run-status { font-weight: 700; }
    #contact { border: 2px solid #b42318; border-radius: 10px; padding: 1rem; margin: 1rem 0; background: #fff1f0; }
    #contact[hidden] { display: none; }
    #direction { box-sizing: border-box; width: 100%; min-height: 5rem; margin: .5rem 0; padding: .6rem; font: inherit; }
    button { padding: .6rem 1rem; font: inherit; font-weight: 700; cursor: pointer; }
    .graph-wrap { overflow: auto; border: 1px solid #d8e0e8; border-radius: 10px; background: #fbfdff; }
    #flow { display: block; min-width: 100%; }
    .edge-water { fill: none; stroke: #64b5d8; stroke-width: 13; opacity: .16; }
    .edge { fill: none; stroke: #8b9baa; stroke-width: 2; opacity: .76; }
    .edge.observed { stroke: #147d9b; stroke-width: 4.5; opacity: 1; }
    .edge.observed.on_failure { stroke: #b42318; stroke-width: 5.5; }
    .edge.observed.on_indeterminate { stroke: #c2410c; stroke-width: 5.5; }
    .edge.unmapped_navigation { stroke: #b42318; stroke-width: 5.5; opacity: 1; }
    .edge-label.observed { font-weight: 800; fill: #174d5e; }
    .edge-label { font: 12px system-ui, sans-serif; fill: #465767; paint-order: stroke; stroke: #fbfdff; stroke-width: 5px; stroke-linejoin: round; }
    .node { cursor: pointer; outline: none; }
    .node rect { stroke-width: 2.5; rx: 12; }
    .node .node-title { font: 700 15px system-ui, sans-serif; fill: #17212b; }
    .node .node-status { font: 700 13px system-ui, sans-serif; }
    .node .node-meta { font: 12px system-ui, sans-serif; fill: #465767; }
    .node.pending rect { fill: #f2f5f8; stroke: #aab8c5; }
    .node.completed rect { fill: #e6f5ec; stroke: #16804a; }
    .node.running rect { fill: #fff4d6; stroke: #bd7900; stroke-width: 3.5; }
    .node.failed rect { fill: #ffe5e2; stroke: #b42318; stroke-width: 5; }
    .node.indeterminate rect { fill: #ffe9d6; stroke: #c2410c; stroke-width: 5; }
    .node.skipped rect, .node.cancelled rect { fill: #edf0f3; stroke: #687786; }
    .node.failed .node-status, .node.indeterminate .node-status { fill: #a11b12; }
    .node.selected rect, .node:focus rect { stroke: #175cd3; stroke-width: 5; }
    #details { border: 1px solid #d8e0e8; border-radius: 10px; margin: 1rem 0; padding: 1rem; }
    #details pre { background: #f3f6f9; border-radius: 6px; max-height: 18rem; overflow: auto; padding: .75rem; white-space: pre-wrap; overflow-wrap: anywhere; }
    #report { background: #f8fafc; border-left: 4px solid #2878a5; padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }
  </style>
</head>
<body>
  <h1>UPMP live local process</h1>
  <p class="muted">This page is served by your device at 127.0.0.1. Opening it starts local checks; nothing is sent to an external site.</p>
  <p id="connection">Connecting to the local runner…</p>
  <p id="run-status">Waiting</p>
  <section id="contact" hidden>
    <h2>UPMP needs your direction</h2>
    <p id="contact-message"></p>
    <label for="direction">Tell UPMP what to do next. The current run will pause until you submit a direction.</label>
    <textarea id="direction" autocomplete="off"></textarea>
    <button id="send-direction" type="button">Send direction</button>
    <p id="contact-result" class="muted"></p>
  </section>
  <h2>Process flow</h2>
  <p class="muted">Faint tributaries show declared routes; a strong current marks the route recorded in the audit. Joins come from declared dependencies. Select a node to inspect its command, output, and findings.</p>
  <div class="graph-wrap"><svg id="flow" role="img" aria-label="Live process flow"></svg></div>
  <section id="details" aria-live="polite"><h2 id="detail-title">Step details</h2><p id="detail-hint" class="muted">Select a step in the flow to see its command, output, and findings.</p><div id="detail-content"></div></section>
  <h2>UPMP diagnosis</h2>
  <pre id="report">Waiting for checks to finish…</pre>
  <script>
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.getElementById("flow");
    const reportNode = document.getElementById("report");
    const statusNode = document.getElementById("run-status");
    const connectionNode = document.getElementById("connection");
    const contactNode = document.getElementById("contact");
    const detailTitle = document.getElementById("detail-title");
    const detailHint = document.getElementById("detail-hint");
    const detailContent = document.getElementById("detail-content");
    let poller = null;
    let selectedNodeId = null;

    function svgElement(name, attributes = {}, text = null) {
      const element = document.createElementNS(ns, name);
      for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
      if (text !== null) element.textContent = String(text);
      return element;
    }

    function appendDetail(label, value) {
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = label;
      const pre = document.createElement("pre");
      pre.textContent = value || "No information recorded.";
      section.append(heading, pre);
      detailContent.append(section);
    }

    function showDetails(node) {
      selectedNodeId = node.id;
      const details = node.details || {};
      const isApplicationScreen = String(details.node_type || "").includes("application_screen");
      const count = isApplicationScreen ? Number(details.visit_count || 0) : node.attempts;
      const countLabel = isApplicationScreen
        ? `${count} visit${count === 1 ? "" : "s"}`
        : `${count} attempt${count === 1 ? "" : "s"}`;
      detailTitle.textContent = `${node.label} · ${node.status} · ${countLabel}`;
      detailHint.textContent = node.branch_id && node.branch_id !== "default" ? `Branch: ${node.branch_id}` : "Step evidence";
      detailContent.replaceChildren();
      if (isApplicationScreen) {
        appendDetail("Route", details.route || details.screen_id);
        appendDetail("Source references", (details.resource_refs || []).join("\n"));
        appendDetail("Map status", details.issue || "Declared application screen");
        appendDetail("Screen details", JSON.stringify(details.metadata || {}, null, 2));
      } else {
        appendDetail("Command", details.command);
        appendDetail("Output", details.output);
        appendDetail("Findings", JSON.stringify(details.findings || [], null, 2));
      }
    }

    function renderGraph(graph) {
      if (!graph || !graph.nodes) return;
      svg.replaceChildren();
      svg.setAttribute("viewBox", `0 0 ${graph.width} ${graph.height}`);
      svg.setAttribute("width", graph.width);
      svg.setAttribute("height", graph.height);
      const defs = svgElement("defs");
      for (const [name, color] of [["normal", "#718394"], ["flow", "#147d9b"], ["alert", "#b42318"], ["warning", "#c2410c"]]) {
        const marker = svgElement("marker", { id: `arrow-${name}`, markerWidth: 10, markerHeight: 10, refX: 8, refY: 4, orient: "auto", markerUnits: "strokeWidth" });
        marker.append(svgElement("path", { d: "M0,0 L0,8 L9,4 z", fill: color }));
        defs.append(marker);
      }
      svg.append(defs);
      const byId = new Map(graph.nodes.map(node => [node.id, node]));
      const cardWidth = 210;
      const cardHeight = 104;
      for (const entryId of graph.entries || []) {
        const point = graph.positions[entryId];
        if (point) svg.append(svgElement("text", { x: Math.max(5, point.x - 38), y: point.y - 12, class: "edge-label", "font-weight": 800 }, "START"));
      }
      for (const edge of graph.edges) {
        const source = byId.get(edge.source);
        const target = byId.get(edge.target);
        const from = graph.positions[edge.source];
        const to = graph.positions[edge.target];
        if (!source || !target || !from || !to) continue;
        const observed = Boolean(edge.observed);
        const routeClass = ["on_success", "on_failure", "on_indeterminate"].includes(edge.label) ? edge.label : "";
        const unmappedClass = edge.kind === "unmapped_navigation" ? " unmapped_navigation" : "";
        const edgeClass = `edge${observed ? " observed" : ""}${routeClass ? ` ${routeClass}` : ""}${unmappedClass}`;
        let d;
        if (to.x > from.x) {
          const startX = from.x + cardWidth;
          const endX = to.x;
          const bend = Math.max(35, (endX - startX) * 0.45);
          d = `M ${startX} ${from.y + cardHeight / 2} C ${startX + bend} ${from.y + cardHeight / 2}, ${endX - bend} ${to.y + cardHeight / 2}, ${endX} ${to.y + cardHeight / 2}`;
        } else {
          const startX = from.x + cardWidth / 2;
          const endX = to.x + cardWidth / 2;
          const loopY = Math.min(from.y, to.y) - 25;
          d = `M ${startX} ${from.y} C ${startX + 55} ${loopY}, ${endX - 55} ${loopY}, ${endX} ${to.y}`;
        }
        svg.append(svgElement("path", { d, class: "edge-water" }));
        const marker = !observed ? "normal"
          : edge.kind === "unmapped_navigation" || edge.label === "on_failure" ? "alert"
          : edge.label === "on_indeterminate" ? "warning" : "flow";
        svg.append(svgElement("path", { d, class: edgeClass, "marker-end": `url(#arrow-${marker})` }));
        if (edge.label) {
          const label = svgElement("text", { x: (from.x + to.x + cardWidth) / 2, y: (from.y + to.y + cardHeight) / 2 - 8, class: `edge-label${observed ? " observed" : ""}`, "text-anchor": "middle" }, edge.label);
          svg.append(label);
        }
      }
      for (const node of graph.nodes) {
        const point = graph.positions[node.id];
        if (!point) continue;
        const group = svgElement("g", { class: `node ${node.status}${node.id === selectedNodeId ? " selected" : ""}`, transform: `translate(${point.x},${point.y})`, tabindex: 0, role: "button", "aria-label": `${node.label}, ${node.status}, ${node.attempts} attempts` });
        group.append(svgElement("rect", { width: cardWidth, height: cardHeight }));
        group.append(svgElement("text", { x: 14, y: 25, class: "node-title" }, node.label));
        group.append(svgElement("text", { x: 14, y: 52, class: "node-status" }, node.status.toUpperCase()));
        const details = node.details || {};
        const isApplicationScreen = String(details.node_type || "").includes("application_screen");
        const branch = node.branch_id && node.branch_id !== "default" ? `Branch ${node.branch_id} · ` : "";
        const count = isApplicationScreen ? Number(details.visit_count || 0) : node.attempts;
        const countLabel = isApplicationScreen
          ? `${count} visit${count === 1 ? "" : "s"}`
          : `${count} attempt${count === 1 ? "" : "s"}`;
        group.append(svgElement("text", { x: 14, y: 78, class: "node-meta" }, `${branch}${countLabel}`));
        group.addEventListener("click", () => showDetails(node));
        group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showDetails(node); } });
        svg.append(group);
      }
      for (const exitId of graph.exits || []) {
        const point = graph.positions[exitId];
        if (point) svg.append(svgElement("text", { x: point.x + cardWidth - 8, y: point.y + cardHeight + 17, class: "edge-label", "text-anchor": "end", "font-weight": 800 }, "END"));
      }
    }

    async function sendDirection() {
      const button = document.getElementById("send-direction");
      const result = document.getElementById("contact-result");
      const direction = document.getElementById("direction").value.trim();
      if (!direction) { result.textContent = "Enter a direction before submitting."; return; }
      button.disabled = true;
      try {
        const response = await fetch("/direction", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction }) });
        result.textContent = response.ok ? "Direction received. UPMP is recording it and will finish this run." : await response.text();
      } catch (error) {
        result.textContent = "Could not send the direction to the local runner.";
        button.disabled = false;
      }
    }
    document.getElementById("send-direction").addEventListener("click", sendDirection);

    async function refresh() {
      try {
        const response = await fetch("/status", { cache: "no-store" });
        const data = await response.json();
        connectionNode.textContent = data.browser_connected ? "Browser connected to the local runner." : "Waiting for browser request…";
        statusNode.textContent = data.done
          ? `Process observed. UPMP run status: ${data.run_status}. ${data.needs_attention ? "Review the diagnosis below." : "All checks passed."}`
          : `Current step: ${data.current_step || "starting"}`;
        renderGraph(data.graph);
        contactNode.hidden = !data.contact_requested || data.direction_received;
        document.getElementById("contact-message").textContent = data.contact_message || "A check failed or could not be verified.";
        reportNode.textContent = data.report || "UPMP is checking each step…";
        if (data.done && poller) clearInterval(poller);
      } catch (error) {
        connectionNode.textContent = "Local runner connection ended.";
      }
    }

    poller = setInterval(refresh, 500);
    fetch("/start", { method: "POST" }).then(refresh).catch(refresh);
    refresh();
  </script>
</body>
</html>"""


def _workflow_steps() -> List[WorkflowStep]:
    ready = VerificationPolicy(
        "local-browser-connected", "1",
        (Constraint("browser_connected", "eq", True),),
    )
    checked = VerificationPolicy(
        "command-exited-zero", "1",
        (Constraint("exit_code", "eq", 0),),
    )
    direction_received = VerificationPolicy(
        "user-direction-received", "1",
        (Constraint("user_direction_received", "eq", True),),
    )
    steps = []
    for index, step_id in enumerate(STEP_IDS):
        following = STEP_IDS[index + 1] if index + 1 < len(STEP_IDS) else None
        steps.append(WorkflowStep(
            step_id,
            step_id,
            ready,
            ready if step_id == "browser_opened" else checked,
            on_success=following,
            on_failure=CONTACT_STEP_ID,
            on_indeterminate=CONTACT_STEP_ID,
            description="Live local check: " + step_id.replace("_", " "),
        ))
    steps.append(WorkflowStep(
        CONTACT_STEP_ID,
        CONTACT_STEP_ID,
        ready,
        direction_received,
        description="Wait for the user's direction",
    ))
    return steps


class LiveState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.browser_connected = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.final_seen = threading.Event()
        self.direction_submitted = threading.Event()
        self.started_once = False
        self.pytest_output_lines: List[str] = []
        self.workflow_steps = _workflow_steps()
        self.audit_events: List[Any] = []
        self.diagnosis: Any = None
        self.direction_text = ""
        self.data: Dict[str, Any] = {
            "browser_connected": False,
            "done": False,
            "current_step": "browser_opened",
            "run_status": "waiting",
            "needs_attention": False,
            "report": "",
            "contact_requested": False,
            "contact_message": "",
            "direction_received": False,
            "steps": [
                {
                    "id": step_id,
                    "label": step_id.replace("_", " ").title(),
                    "status": "waiting",
                    "exit_code": None,
                    "pid": None,
                    "command": "",
                    "output": "",
                }
                for step_id in ALL_STEP_IDS
            ],
        }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            result = json.loads(json.dumps(self.data))
            result["graph"] = workflow_flow_graph(
                self.workflow_steps,
                self.audit_events,
                self.data["steps"],
                self.diagnosis,
            ).to_dict()
            return result

    def update_step(self, step_id: str, **values: Any) -> None:
        with self.lock:
            self.data["current_step"] = step_id
            for step in self.data["steps"]:
                if step["id"] == step_id:
                    step.update(values)
                    return

    def append_output(self, step_id: str, line: str) -> None:
        with self.lock:
            if step_id == "pytest":
                self.pytest_output_lines.append(line)
            for step in self.data["steps"]:
                if step["id"] == step_id:
                    step["output"] = (step["output"] + line)[-OUTPUT_LIMIT:]
                    return

    def complete_output(self, step_id: str) -> str:
        with self.lock:
            if step_id == "pytest":
                return "".join(self.pytest_output_lines)
            for step in self.data["steps"]:
                if step["id"] == step_id:
                    return step["output"]
            return ""


class _LiveAuditSink:
    """Publish each audit event to the page as soon as the runtime records it."""

    def __init__(self, live: LiveState) -> None:
        self.live = live

    def append(self, event: Any) -> int:
        with self.live.lock:
            self.live.audit_events.append(event)
            return len(self.live.audit_events)


def _commands(pytest_temp: Path) -> Dict[str, List[str]]:
    python = sys.executable
    return {
        "compile": [python, "-u", "-m", "compileall", "-q", "UPMP", "tests", "examples", "scripts"],
        "pytest": [python, "-u", "-m", "pytest", "tests/", "-q", "--tb=short",
                   "--basetemp", str(pytest_temp)],
        "scenarios": [python, "-u", "tests/test_scenarios.py"],
        "simulation": [python, "-u", "examples/closed_loop_simulation.py"],
        "diagnostics_example": [python, "-u", "examples/diagnose_workflow.py"],
    }


def _failure_summary(output: str, return_code: int) -> str:
    failure_names = []
    reported_count = None
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("FAILED "):
            failure_names.append(text[len("FAILED "):].split(" - ", 1)[0].strip())
        summary_match = re.search(r"(?:^|\s)(\d+) failed(?:,|\s|$)", text)
        if summary_match:
            reported_count = int(summary_match.group(1))

    if reported_count is not None:
        parsed_count = len(failure_names)
        if parsed_count != reported_count:
            captured = ", ".join(failure_names) if failure_names else "none"
            return (
                "INCOMPLETE FAILURE REPORT: pytest reports %d failed, but only %d "
                "failure names were captured. Captured names: %s"
                % (reported_count, parsed_count, captured)
            )
        names = "; ".join(failure_names) if failure_names else "none"
        return "Pytest reports %d failed; listed %d: %s" % (
            reported_count, parsed_count, names,
        )
    if failure_names:
        return (
            "INCOMPLETE FAILURE REPORT: captured %d failure names, but pytest's "
            "failure count was not present. Names: %s"
            % (len(failure_names), "; ".join(failure_names))
        )
    if return_code:
        return (
            "Pytest exited with code %d, but its failure count and names could not "
            "be verified. Captured output: %s"
            % (return_code, output.strip()[-1400:] or "no output captured")
        )
    return (output.strip()[-1400:] or "no output captured")


def _make_workflow(live: LiveState, server_url: str) -> Tuple[ClosedLoopOrchestrator, ProcessState]:
    pytest_temp = ROOT / (".pytest-live-browser-" + uuid.uuid4().hex)
    commands = _commands(pytest_temp)
    dispatcher = ActionDispatcher(set(ALL_STEP_IDS))

    def run_step(request: Any, current: ProcessState) -> ActionResult:
        step_id = request.action
        if step_id == "browser_opened":
            live.update_step(
                step_id,
                status="completed",
                attempts=1,
                command="Browser requested " + server_url,
                output="The local HTTP server received the browser page request.",
            )
            return ActionResult(
                ActionStatus.SUCCEEDED,
                "The local browser requested the live status page.",
                current,
            )

        if step_id == CONTACT_STEP_ID:
            snapshot = live.snapshot()
            observed_problems = [
                item for item in snapshot["steps"]
                if item["status"] in ("failed", "indeterminate")
            ]
            problem = observed_problems[-1] if observed_problems else None
            contact_message = (
                "UPMP could not verify step '%s'. Review its node and diagnosis, "
                "then enter what this process should do next."
                % (problem["label"] if problem else "the current step")
            )
            with live.lock:
                live.data["contact_requested"] = True
                live.data["contact_message"] = contact_message
            live.update_step(
                step_id,
                status="running",
                attempts=1,
                command="Wait for direction in the local browser page",
                output="The workflow is paused until a direction is submitted.",
            )
            live.direction_submitted.wait()
            direction = live.direction_text
            observations = dict(current.observations)
            observations["user_direction_received"] = Observation(True, source="local-user")
            observations["user_direction"] = Observation(direction, source="local-user")
            next_state = ProcessState(current.schema_id, current.schema_version, observations)
            live.update_step(
                step_id,
                status="completed",
                output="Direction received: " + direction,
            )
            with live.lock:
                live.data["direction_received"] = True
            return ActionResult(
                ActionStatus.SUCCEEDED,
                "The local user supplied direction; UPMP recorded it.",
                next_state,
                output={"direction": direction},
            )

        command = commands[step_id]
        live.update_step(
            step_id,
            status="running",
            attempts=1,
            command=subprocess.list2cmdline(command),
            output="",
            exit_code=None,
            pid=None,
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            message = "Could not start the local command: %s" % exc
            live.update_step(step_id, status="failed", output=message)
            return ActionResult(ActionStatus.FAILED, message, current)

        live.update_step(step_id, pid=process.pid)

        def stream_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                live.append_output(step_id, line)

        reader = threading.Thread(target=stream_output, daemon=True)
        reader.start()
        try:
            return_code = process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
            live.append_output(
                step_id,
                "\nUPMP stopped this check after %d seconds.\n" % PROCESS_TIMEOUT_SECONDS,
            )
            reader.join()
            live.update_step(step_id, status="indeterminate", exit_code=return_code)
            return ActionResult(
                ActionStatus.INDETERMINATE,
                "The command exceeded its %d second limit." % PROCESS_TIMEOUT_SECONDS,
                current,
            )

        reader.join()
        step_output = live.complete_output(step_id)
        observations = dict(current.observations)
        observations["exit_code"] = Observation(return_code, source=step_id)
        observations["last_command"] = Observation(subprocess.list2cmdline(command), source=step_id)
        next_state = ProcessState(current.schema_id, current.schema_version, observations)

        if return_code == 0:
            live.update_step(step_id, status="completed", exit_code=0)
            return ActionResult(
                ActionStatus.SUCCEEDED,
                "Command completed with exit code 0.",
                next_state,
                output={"return_code": 0},
            )

        summary = _failure_summary(step_output, return_code)
        live.update_step(step_id, status="failed", exit_code=return_code)
        return ActionResult(
            ActionStatus.FAILED,
            "Command exited with code %d. %s" % (return_code, summary),
            next_state,
            output={"return_code": return_code, "summary": summary},
        )

    for action in STEP_IDS:
        dispatcher.register(action, run_step)
    dispatcher.register(CONTACT_STEP_ID, run_step)

    initial = ProcessState(
        "upmp.live-browser-process", "1",
        {
            "browser_connected": Observation(True, source="loopback-browser-request"),
            "exit_code": Observation(0, source="runner-ready"),
            "user_direction_received": Observation(False, source="waiting-for-user"),
        },
    )
    return ClosedLoopOrchestrator(
        live.workflow_steps,
        dispatcher,
        audit_sink=_LiveAuditSink(live),
    ), initial


def _run_workflow(live: LiveState, server_url: str) -> None:
    try:
        orchestrator, initial = _make_workflow(live, server_url)
        result = orchestrator.run(initial)
        diagnosis = diagnose_workflow(result)
        with live.lock:
            live.audit_events = list(result.audit)
            live.diagnosis = diagnosis
            live.data["done"] = True
            live.data["run_status"] = result.status.value
            live.data["needs_attention"] = diagnosis.needs_attention
            report = diagnosis.format_report()
            if live.direction_text:
                report += "\n\nUser direction received: " + live.direction_text
            live.data["report"] = report
    except Exception as exc:  # Keep failures visible in the local browser.
        with live.lock:
            live.data["done"] = True
            live.data["run_status"] = "runner_error"
            live.data["needs_attention"] = True
            live.data["report"] = "Live runner error: %s" % exc
    finally:
        live.finished.set()


def _open_installed_browser(url: str) -> bool:
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    candidates = (
        program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
    )
    for executable in candidates:
        if executable.is_file():
            try:
                subprocess.Popen(
                    [str(executable), "--new-window", url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                return True
            except OSError:
                continue
    return False


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], live: LiveState, server_url_holder: Dict[str, str]):
        self.live = live
        self.server_url_holder = server_url_holder
        super().__init__(address, _RequestHandler)


class _RequestHandler(BaseHTTPRequestHandler):
    server: _LocalServer

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            with self.server.live.lock:
                self.server.live.data["browser_connected"] = True
            self.server.live.browser_connected.set()
            self._reply(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if path == "/status":
            snapshot = self.server.live.snapshot()
            if snapshot["done"]:
                self.server.live.final_seen.set()
            self._reply(
                200,
                "application/json; charset=utf-8",
                json.dumps(snapshot).encode("utf-8"),
            )
            return
        self._reply(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/direction":
            if not self.server.live.data["contact_requested"]:
                self._reply(409, "text/plain; charset=utf-8", b"UPMP is not waiting for direction")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reply(400, "text/plain; charset=utf-8", b"Invalid request length")
                return
            if length <= 0 or length > 10000:
                self._reply(413, "text/plain; charset=utf-8", b"Direction must be between 1 and 10000 bytes")
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                direction = str(body.get("direction", "")).strip()
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                self._reply(400, "text/plain; charset=utf-8", b"Direction must be valid JSON text")
                return
            if not direction:
                self._reply(400, "text/plain; charset=utf-8", b"Direction cannot be empty")
                return
            with self.server.live.lock:
                if self.server.live.direction_submitted.is_set():
                    self._reply(409, "text/plain; charset=utf-8", b"Direction was already submitted")
                    return
                self.server.live.direction_text = direction
                self.server.live.direction_submitted.set()
            self._reply(202, "application/json; charset=utf-8", b'{"received":true}')
            return
        if path != "/start":
            self._reply(404, "text/plain; charset=utf-8", b"Not found")
            return
        if not self.server.live.browser_connected.is_set():
            self._reply(409, "text/plain; charset=utf-8", b"Open the local page first")
            return
        with self.server.live.lock:
            if not self.server.live.started_once:
                self.server.live.started_once = True
                self.server.live.started.set()
                worker = threading.Thread(
                    target=_run_workflow,
                    args=(self.server.live, self.server.server_url_holder["url"]),
                    daemon=True,
                )
                worker.start()
        self._reply(202, "application/json; charset=utf-8", b'{"started":true}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual-browser",
        action="store_true",
        help="print the local URL and wait for you to open it in a browser",
    )
    args = parser.parse_args()

    live = LiveState()
    url_holder: Dict[str, str] = {}
    server = _LocalServer(("127.0.0.1", 0), live, url_holder)
    host, port = server.server_address
    url = "http://%s:%d/" % (host, port)
    url_holder["url"] = url
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("UPMP local browser process")
    print("Repository: %s" % ROOT)
    print("The workflow runs locally; the server binds to 127.0.0.1 only.")
    if args.manual_browser:
        print("Open this URL in your browser: %s" % url)
        connected = live.browser_connected.wait(120)
    else:
        print("Opening the default browser at %s" % url)
        opened = webbrowser.open(url, new=2)
        connected = live.browser_connected.wait(5) if opened else False
        if not connected:
            opened = _open_installed_browser(url)
            connected = opened and live.browser_connected.wait(20)
    if not connected:
        print("The browser did not request the local page within 20 seconds.")
        server.shutdown()
        server.server_close()
        return 2
    # A failed or unverifiable step routes to a user-contact step. Keep the
    # local server alive until the user submits a direction in that page.
    live.finished.wait()

    # Give the browser one final poll so the last diagnosis remains visible.
    live.final_seen.wait(5)
    report = live.snapshot()["report"]
    print("\n" + report)
    needs_attention = live.snapshot()["needs_attention"]
    server.shutdown()
    server.server_close()
    return 1 if needs_attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
