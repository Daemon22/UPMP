# UPMP Process Observatory

This self-contained visual concept explores UPMP as a layered process observatory. It starts with a system-scale river map, then lets you enter subsystems and selected child processes without laying every detail across one crowded screen. The supplied sapphire-and-gold brand artwork anchors the interface.

## Open and explore

Open `index.html` in a modern desktop browser. No install or network connection is required.

- **River map** starts at the full ecosystem: intent and entry, orchestration, parallel workstreams, evidence, assurance, and outcomes.
- Select a stream to inspect its expected behavior, actual progress, checkpoint results, evidence, deviations, and mutation links. Double-click it or choose **Enter this process** to open its branches.
- Follow deeper lanes such as retry and recovery, integration schema validation, evidence provenance, mutation impact, and button lineage. Breadcrumbs and the zoom-out control return to parent processes.
- **Architecture** groups the proposed services into orchestration, execution, evidence/event, and assurance planes. Select a service card to open its process lane.
- **Activity**, **Mutations**, and **Output surfaces** show run events, expected-versus-actual changes, and user-facing results.
- Choose **Open simple example** to switch to the original face-verification journey. Its button can still be traced from product intent through component, action binding, workflow, and result state.

The run values and events are illustrative. This is a visual proposal, not a live UPMP runtime. In a deployed view, progress, pass/fail/hold states, evidence, deviations, and mutation records would come from instrumented process events and the declared outcome policy.

## Files

- `index.html` is the interactive prototype.
- `assets/` contains the supplied brand artwork adapted for the interface.
- `previews/` contains static visual plates used in the concept PDF.
- `render_previews.py` recreates the static visual plates.
- `assemble_pdf.py` rebuilds the PDF with both the original simple views and the complex ecosystem views.
