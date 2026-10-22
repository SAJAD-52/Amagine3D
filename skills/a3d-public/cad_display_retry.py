"""Rebuild only display artifacts from an audited, input-bound CAD stage."""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from uuid import uuid4


def retry_display(root, args):
    from agent_api import (RequestError, _active_matches, _intent_lineage_current, _paths_current,
                           _report_artifacts, _summary_document, capability_version, save, state)
    from agent_protocol import binding, read_report, scoped, summary
    from build123d import import_step
    from cad_stages import artifact_stage, read_cad_stage, stage_issue, write
    from cpu_z_buffer import DEFAULT_MATERIAL, render_contact_sheet, render_view
    from display_step import export_display_step, load_display_components, load_step_objects
    from export_audit import _geometry_errors, geometry_record
    from geometry_binding import shape_to_mesh
    from render_preview import _render_inputs

    parent_ref, parent = read_report(root, args["report"], args.get("sha256"))
    if parent.get("schema") not in {"a3d-draft-result/v2", "evidence-cad-compile-result/v2"} or not parent.get("cadStage"):
        raise RequestError("display retry needs a CAD run report with a bound CAD stage")
    inputs = parent.get("taskInputBindings", parent.get("inputBindings", {}))
    if not inputs and parent.get("source"): inputs = {"source": parent["source"]}
    current, version = state(root), capability_version()
    if not _paths_current(root, inputs) or not _active_matches(root, current, inputs):
        raise RequestError("display retry inputs changed; recover context for the current revision")
    stage_ref, _ = read_report(root, parent["cadStage"]["path"], parent["cadStage"]["sha256"])
    document = read_cad_stage(stage_ref["path"], root, parent["runId"])
    if document["stages"]["cadExport"]["status"] != "passed":
        raise RequestError("display retry requires a passed STEP readback stage")
    reusable_manufacturing = False
    full_inputs = dict(inputs)
    prior_outputs = parent.get("artifacts", {})
    full_outputs = dict(prior_outputs)
    parent_record = next((record for record in [current.get("latest", {}), *current.get("runs", [])]
                          if record.get("report") == parent_ref and record.get("capabilityVersion") == version), None)
    if parent.get("schema") == "evidence-cad-compile-result/v2" and parent.get("status") == "awaiting-preview" and parent_record:
        try:
            _, build_report = read_report(root, prior_outputs["buildReport"]["path"], prior_outputs["buildReport"]["sha256"])
            full_inputs.update(build_report["inputs"])
            full_outputs.update(build_report["artifacts"])
            reusable_manufacturing = (document["stages"]["manufacturing"]["status"] == "passed"
                and _paths_current(root, full_inputs) and _paths_current(root, full_outputs) and _intent_lineage_current(root, inputs)
                and not any(issue.get("severity") == "error" and issue.get("stage") not in {"render", "preview", "display", "tessellation"} for issue in parent.get("issues", [])))
        except (OSError, ValueError, KeyError): pass
    latest = current.get("latest", {})
    if latest.get("operation") == "display" and latest.get("parentReport") == parent_ref and latest.get("capabilityVersion") == version:
        try:
            ref, previous = read_report(root, latest["report"]["path"], latest["report"]["sha256"])
            proof_current = (previous.get("status") != "awaiting-visual-review" or
                             (reusable_manufacturing and _paths_current(root, full_inputs) and _paths_current(root, full_outputs)))
            if previous.get("status") in {"draft", "awaiting-visual-review"} and proof_current and _paths_current(root, latest.get("outputs", {})):
                projected, image_metadata = _summary_document(root, _report_artifacts(root, ref, previous))
                return summary(projected, ref, metadata_extra={"reused": True, "inputsCurrent": True, **image_metadata})
        except (OSError, ValueError, KeyError): pass
    run_id = str(uuid4())
    output = scoped(root, ".amagine3d-drafts/" + run_id, exists=False)
    output.mkdir(parents=True, exist_ok=False)
    stage_path = output / "draft_cad-stage.json"
    document = deepcopy(document)
    document.update(runId=run_id, parentRunId=parent["runId"], issues=[])
    if parent.get("status") == "awaiting-preview" and not reusable_manufacturing:
        document["stages"]["manufacturing"] = {"status": "pending", "reason": "Parent manufacturing evidence is no longer current."}
    for phase in ("tessellation", "display", "preview", "visualReview"):
        document["stages"][phase] = {"status": "pending"}
    write(stage_path, document)
    result = {"schema": "a3d-draft-result/v2", "runId": run_id, "status": "failed", "source": inputs.get("source"),
              "taskInputBindings": inputs, "parentReport": parent_ref,
              "artifacts": {key: value for key, value in document["artifacts"].items() if not key.startswith("object:")},
              "issues": [], "limitations": ["Display retry only; geometry was not redesigned and manufacturing QA was not rerun."]}
    if reusable_manufacturing:
        result = {**deepcopy(parent), **result, "schema": "evidence-cad-compile-result/v2", "status": "awaiting-preview",
                  "pass": False, "visualReviewRequired": True, "inputBindings": deepcopy(parent.get("inputBindings", {})),
                  "issues": [issue for issue in deepcopy(parent.get("issues", [])) if issue.get("stage") not in {"render", "preview", "display", "tessellation"}],
                  "artifacts": deepcopy(prior_outputs),
                  "manufacturingEvidence": {"parentReport": parent_ref, "reused": True, "inputBindings": full_inputs}}
    current["activeAttempt"] = {"phase": "display", "pid": os.getpid(), "runId": run_id}
    save(root, ".a3d-agent/state.json", current)
    started = time.monotonic()
    phase = "tessellation"
    try:
        physical, references = [], []
        with artifact_stage(stage_path, phase):
            display_source = prior_outputs.get("step:display", prior_outputs.get("step"))
            if display_source is None and prior_outputs.get("buildReport"):
                _, saved_build = read_report(root, prior_outputs["buildReport"]["path"], prior_outputs["buildReport"]["sha256"])
                display_source = saved_build.get("artifacts", {}).get("step:display")
            if display_source:
                for obj, shape in load_step_objects(scoped(root, display_source["path"]))[0]:
                    item = (obj["name"], shape, obj["appearance"])
                    if obj.get("physicalFeatureRef"): shape._a3d_display_reference = obj["physicalFeatureRef"]
                    if obj.get("ownerPart"): shape._a3d_display_owner_part = obj["ownerPart"]
                    if obj.get("regionId"): shape._a3d_display_region_id = obj["regionId"]
                    (references if obj["role"] == "display-only" else physical).append(item)
            else:
                for obj in document["objects"]:
                    step = document["artifacts"][obj["stepArtifact"]]
                    shape = import_step(scoped(root, step["path"]))
                    shape.parent = None
                    errors = _geometry_errors(geometry_record(shape), obj["geometry"], obj["name"])
                    if errors: raise ValueError("; ".join(errors))
                    item = (obj["name"], shape, obj.get("appearance", {"baseColor": obj.get("color", "#6891B5")}))
                    (references if obj["role"] == "component-reference" else physical).append(item)
        phase = "display"
        display_step, preview = output / "display.step", output / "draft-preview.png"
        with artifact_stage(stage_path, phase):
            if not display_source and inputs.get("scene"):
                scene_path = scoped(root, inputs["scene"]["path"])
                references.extend(load_display_components(json.loads(scene_path.read_text(encoding="utf-8")), scene_path))
            display_info = export_display_step(physical, display_step, display_items=references, metadata={"status": "draft", "runId": run_id})
            result["artifacts"]["step:display"] = {**binding(display_step), "scene": display_info["scene"]}
        phase = "preview"
        with artifact_stage(stage_path, phase):
            render_inputs = _render_inputs(display_step, DEFAULT_MATERIAL)
            contact = render_contact_sheet(render_inputs, 640, title="STEP preview")
            contact.image.save(preview, format="PNG")
            result["artifacts"]["preview"] = binding(preview)
        if not _paths_current(root, inputs): raise ValueError("display retry inputs changed during execution")
        if reusable_manufacturing:
            if not _paths_current(root, full_inputs) or not _paths_current(root, full_outputs) or not _intent_lineage_current(root, inputs) or capability_version() != version:
                raise ValueError("Manufacturing evidence changed during preview retry")
            _, intent = read_report(root, full_inputs["intent"]["path"], full_inputs["intent"]["sha256"])
            reference_view = intent["visual"]["reference_view"]
            reference_image = output / (reference_view + ".png")
            render_view(render_inputs, reference_view, 640).image.save(reference_image, format="PNG")
            evidence_path = output / "render.json"
            write(evidence_path, {"schema": "evidence-render/v3", "parentReport": parent_ref, "inputs": full_inputs,
                "preview": binding(preview), "matched_view": {**binding(reference_image), "name": reference_view},
                "sourceScenes": [__import__("display_step").load_step_objects(display_step)[1]],
                "meshes": [binding(display_step)], "purpose": "automated-checks-passed; manufacturing reused"})
            if not _paths_current(root, full_inputs) or not _paths_current(root, full_outputs) or not _intent_lineage_current(root, inputs) or capability_version() != version:
                raise ValueError("Manufacturing evidence changed during preview retry")
            result["artifacts"].update(renderEvidence=binding(evidence_path), referencePreview=binding(reference_image))
            result.update({"status": "awaiting-visual-review", "pass": True})
        else:
            result["status"] = "draft"
    except Exception as error:
        result["issues"] = [*result.get("issues", []), stage_issue(error, phase)]
        if not _paths_current(root, full_inputs): result["artifacts"] = {}; result["pass"] = False; result["status"] = "failed"
    final_stage = json.loads(stage_path.read_text(encoding="utf-8"))
    result.update(cadStage=binding(stage_path), artifactStages=final_stage["stages"], elapsedMs=round((time.monotonic() - started) * 1000))
    path = save(root, ".a3d-agent/runs/" + run_id + ".json", result)
    current = state(root)
    current.pop("activeAttempt", None)
    outputs = {**result["artifacts"], "cadStage": result["cadStage"]}
    current["latest"] = {"operation": "display", "runId": run_id, "report": binding(path), "inputs": inputs,
                         "outputs": outputs, "capabilityVersion": version, "parentReport": parent_ref,
                         **({"design": parent["design"]} if parent.get("design") else {})}
    current["runs"] = [*current.get("runs", []), current["latest"]][-20:]
    save(root, ".a3d-agent/state.json", current)
    report_ref = binding(path)
    projected, image_metadata = _summary_document(root, _report_artifacts(root, report_ref, result))
    return summary(projected, report_ref, metadata_extra={"reused": False, "inputsCurrent": _paths_current(root, inputs),
                                                          **image_metadata})
