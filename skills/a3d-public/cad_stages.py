"""Bound CAD delivery survives a failure in derived display artifacts."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

SCHEMA = "a3d-cad-stage/v2"


def binding(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def write(path, document):
    path = Path(path)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True, allow_nan=False) + "\n")
    temporary.replace(path)


def stage_issue(error, phase):
    issue = dict(getattr(error, "issue", {}))
    phase_codes = {
        "cadExport": "CAD.STEP_EXPORT_FAILED",
        "tessellation": "DISPLAY.MESH_FAILED",
        "display": "DISPLAY.EXPORT_FAILED",
        "preview": "DISPLAY.PREVIEW_FAILED",
    }
    issue.setdefault("code", phase_codes.get(phase, "CAD.GEOMETRY_FAILED"))
    issue.setdefault("message", str(error))
    issue.setdefault("stage", phase)
    issue.setdefault("severity", "error")
    from cad_diagnostics import add_repair_hint
    if phase == "geometry":
        fallback = "For a draft, export_draft(parts) requires valid positive-volume BRep solids."
    elif phase == "cadExport":
        fallback = "Inspect the bound CAD export and source evidence."
    else:
        fallback = "Inspect the bound CAD stage and local display issue; retry display from unchanged CAD geometry."
    issue = add_repair_hint(issue, fallback)
    return issue


def persist_cad_geometry(output, name, steps, objects):
    """Audit STEP against pre-export BRep before display meshing.

    steps maps artifact keys to (shape, destination). XDE exports use copies
    detached from synthetic import parents; callers' geometry remains unchanged.
    """
    from build123d import Unit, export_step
    from export_audit import _audit_step, geometry_record
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / (name + "_cad-stage.json")
    inputs = {}
    for key, variable in (("source", "AMAGINE3D_CAD_SOURCE_PATH"), ("intent", "AMAGINE3D_INTENT_PATH")):
        value = os.environ.get(variable)
        if value: inputs[key] = binding(value)
    run_id = os.environ.get("AMAGINE3D_COMPILE_RUN_ID") if os.environ.get("AMAGINE3D_SOURCE_PHASE") == "compile" else os.environ.get("AMAGINE3D_DRAFT_RUN_ID")
    document = {"schema": SCHEMA, "runId": run_id,
                "units": "mm", "coordinateSystem": {"handedness": "right", "up": "Z"},
                "inputBindings": inputs, "objects": objects, "artifacts": {}, "audits": {}, "issues": [],
                "stages": {"geometry": {"status": "pending"}, "cadExport": {"status": "pending"},
                           "tessellation": {"status": "pending"}, "display": {"status": "pending"},
                           "preview": {"status": "pending"}, "manufacturing": {"status": "pending"},
                           "visualReview": {"status": "pending"}}}
    write(path, document)
    expected_records = {}
    with artifact_stage(path, "geometry"):
        for key, (original, _) in steps.items():
            expected = geometry_record(original)
            if not expected["valid"] or expected["bodyCount"] < 1: raise ValueError("CAD master must contain valid BRep solids")
            expected_records[key] = expected
    with artifact_stage(path, "cadExport"):
        for key, (original, destination) in steps.items():
            destination = Path(destination).resolve()
            if not destination.is_relative_to(output): raise ValueError("CAD export must stay in its run directory")
            expected = expected_records[key]
            shape = deepcopy(original)
            shape.parent = None
            if not export_step(shape, destination, unit=Unit.MM): raise ValueError("STEP writer did not report success")
            audit = _audit_step(destination, expected, key)
            if not audit["pass"]: raise ValueError("; ".join(audit["errors"]))
            current = json.loads(path.read_text(encoding="utf-8"))
            current["artifacts"][key] = binding(destination)
            current["audits"][key] = audit
            write(path, current)
    return path


@contextmanager
def artifact_stage(path, phase):
    started = time.monotonic()
    try:
        yield
    except Exception as error:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        document["issues"].append(stage_issue(error, phase))
        document["stages"][phase] = {"status": "failed", "elapsedMs": round((time.monotonic() - started) * 1000)}
        write(path, document)
        raise
    else:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        document["stages"][phase] = {"status": "passed", "elapsedMs": round((time.monotonic() - started) * 1000)}
        write(path, document)


def read_cad_stage(path, workspace, run_id):
    """Verify run, input and output bytes; delivery is never inferred from presence."""
    workspace, path = Path(workspace).resolve(), Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(workspace): raise ValueError("CAD stage is out of scope")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SCHEMA or document.get("runId") != run_id: raise ValueError("CAD stage belongs to another run")
    for refs in (document["inputBindings"], document["artifacts"]):
        for label, ref in refs.items():
            candidate = Path(ref["path"])
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(workspace) or binding(candidate) != ref:
                raise ValueError(f"CAD stage {label} changed or is out of scope")
    if not document["inputBindings"].get("source"): raise ValueError("CAD stage has no bound source")
    if document["stages"]["cadExport"]["status"] == "passed" and (not document["artifacts"] or
            any(not document["audits"].get(key, {}).get("pass") or document["audits"][key].get("sha256") != ref["sha256"]
                for key, ref in document["artifacts"].items())):
        raise ValueError("CAD stage has no matching STEP audits")
    return document


def attach_cad_stage(result, path, workspace, run_id):
    document = read_cad_stage(path, workspace, run_id)
    result.update(cadStage=binding(path), artifactStages=document["stages"])
    if document["stages"]["cadExport"]["status"] == "passed":
        result.setdefault("artifacts", {}).update({key: ref for key, ref in document["artifacts"].items() if not key.startswith("object:")})
    return document
