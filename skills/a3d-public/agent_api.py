"""Workspace-scoped task CLI. Each operation returns one bounded JSON result.

a3d agent context
a3d agent capabilities '{"symbols":["BuildSession","SelfTappingScrewPair.bind"]}'
a3d agent build --args-file build-request.json
a3d agent inspect '{"report":"model_compile-result.json","field":"/inputBindings/source"}'
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from agent_protocol import SCHEMA, binding, compact, encode, inspect, read_report, scoped, summary, wire_size
from design_api import design
from design_language import DesignError

# One public argument contract, without a second transport-specific schema.
ARGUMENTS = {
    "context": {},
    "capabilities": {"symbols": list, "designOps": list, "cursor": str},
    "design": {"action": str, "design": str, "baseRevision": object, "revision": str,
               "changes": list, "view": str, "ids": list, "include": list},
    "build": {"phase": str, "source": str, "design": str, "revision": str, "intent": str, "scene": str, "dependencies": list, "timeoutSeconds": float},
    "inspect": {"report": str, "sha256": str, "field": str, "issueId": str, "cursor": str},
    "validate": {"intent": str, "scene": str, "source": str, "design": str, "revision": str},
    "checkpoint": {"report": str, "sha256": str, "observations": list, "todo": list, "functionalReady": bool},
    "display": {"report": str, "sha256": str},
    "preview": {"report": str, "sha256": str, "sourceArtifact": str, "requests": list},
}
REQUIRED = {"build": {"phase"}, "inspect": {"report"}, "design": {"action", "design"},
            "checkpoint": {"report"}, "display": {"report"}, "preview": {"report", "sha256", "requests"}}


class RequestError(ValueError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


@contextmanager
def mutation_lock(root: Path, *, name: str = ".a3d-agent/operation.lock"):
    """Prevent concurrent state/build writers; OS locks release on cancellation."""
    path = scoped(root, name, exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("another state/build operation is running; use context before retrying") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def save(root: Path, name: str, value: dict) -> Path:
    path = scoped(root, name, exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        temporary.write_text(encode(value) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def state(root: Path) -> dict:
    path = scoped(root, ".a3d-agent/state.json", exists=False)
    if not path.exists():
        return {"schema": SCHEMA}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("invalid agent state; inspect .a3d-agent/state.json")
    return value


def capability_version() -> str:
    from capability_manifest import build_manifest
    runtime = [sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
               for name in ("agent_api.py", "agent_protocol.py", "agent_preview.py", "cpu_z_buffer.py", "brep_measurements.py", "design_language.py", "design_api.py", "design_build.py", "design_connections.py", "design_roles.py", "cad_stages.py", "cad_display_retry.py", "display_step.py", "display_normals.py",
                            "intent_contract.py", "intent_revision.py", "cad_diagnostics.py")]
    return sha256(encode([build_manifest()["fingerprint"], *runtime]).encode()).hexdigest()


def _paths_current(root: Path, bindings: dict) -> bool:
    if not bindings:
        return False
    try:
        for ref in bindings.values():
            path = scoped(root, ref["path"])
            if binding(path) != {"path": ref["path"], "sha256": ref["sha256"]}:
                return False
            if ref.get("scene"):
                if not _paths_current(root, {"scene": ref["scene"]}): return False
                descriptor = json.loads(scoped(root, ref["scene"]["path"]).read_text(encoding="utf-8"))
                if descriptor.get("schema") != "a3d-step-display/v1" or descriptor.get("step") != binding(path): return False
                if not _paths_current(root, {str(i): obj["step"] for i, obj in enumerate(descriptor.get("objects", []))}): return False
        return True
    except (KeyError, TypeError, OSError, ValueError):
        return False


def _preview_binding(root: Path, document: dict) -> tuple[str | None, dict | None]:
    artifacts = document.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return None, None
    stale = None
    for key in ("preview", "diagnosticPreview"):
        ref = artifacts.get(key)
        if isinstance(ref, dict) and isinstance(ref.get("path"), str) and isinstance(ref.get("sha256"), str):
            if _paths_current(root, {key: ref}):
                return key, {"path": ref["path"], "sha256": ref["sha256"]}
            stale = stale or key
    return stale, None


def _report_artifacts(root: Path, report_ref: dict, document: dict) -> dict:
    """Join the exact run's output bindings for reports that keep them in state."""
    result = dict(document)
    artifacts = dict(document.get("artifacts", {})) if isinstance(document.get("artifacts"), dict) else {}
    current = state(root)
    records = [current.get("latest", {}), *current.get("runs", [])]
    run = next((entry for entry in records if isinstance(entry, dict) and entry.get("report") == report_ref), None)
    if run and isinstance(run.get("outputs"), dict):
        artifacts = {**run["outputs"], **artifacts}
    result["artifacts"] = artifacts
    return result


def _summary_document(root: Path, document: dict) -> tuple[dict, dict]:
    """Project only a current bound image path into a direct agent response."""
    projected = dict(document)
    artifacts = document.get("artifacts", {})
    key, ref = _preview_binding(root, document)
    if isinstance(artifacts, dict):
        projected["artifacts"] = {name: value for name, value in artifacts.items()
                                   if name not in {"preview", "diagnosticPreview"} or name == key and ref}
    inputs = document.get("taskInputBindings", document.get("inputBindings", {}))
    if not inputs and isinstance(document.get("source"), dict):
        inputs = {"source": document["source"]}
    current_inputs = _paths_current(root, inputs) if inputs else False
    active = _active_matches(root, state(root), inputs) if inputs else False
    historical = not (current_inputs and active)
    metadata = ({"previewState": "historical" if ref and historical else "current" if ref else "stale-or-missing",
                 "previewCurrentness": {"inputsCurrent": current_inputs, "activeInputsMatch": active,
                                        "historical": historical},
                 **({"previewArtifact": key} if key and ref else {})} if key or artifacts else {})
    return projected, metadata
def _active_matches(root, current, inputs):
    try:
        intent = current.get("activeIntent")
        if intent and inputs.get("intent") and scoped(root, intent["path"], exists=False) != scoped(root, inputs["intent"]["path"], exists=False):
            return False
        active = current.get("activeInput")
        if not active:
            pointer = scoped(root, ".a3d-agent/design.json", exists=False)
            if pointer.exists(): active = {"kind": "design", "path": json.loads(pointer.read_text(encoding="utf-8"))["path"]}
        if not active: return True
        ref = inputs.get(active["kind"])
        return bool(ref and scoped(root, ref["path"], exists=False) == scoped(root, active["path"], exists=False))
    except (KeyError, TypeError, OSError, ValueError):
        return False


def _intent_lineage_current(root: Path, inputs: dict) -> bool:
    from intent_revision import audit_lineage
    try:
        path = scoped(root, inputs["intent"]["path"])
        document = json.loads(path.read_text(encoding="utf-8"))
        audit_lineage(root, path, document, persist_snapshots=False)
        return True
    except (KeyError, TypeError, OSError, ValueError):
        return False


def source_dependencies(root: Path, source: Path, declared: list[str]) -> dict:
    """Bind workspace Python imports and explicitly declared input data.

    No model execution or external package discovery is needed. Arbitrary Python
    file reads must be declared by the caller; automatic dependency inference is
    limited to local imports, including common controls.py parameter modules.
    """
    files = {scoped(root, name) for name in declared}

    def local_modules(path: Path, module: str, *, level: int = 0, children=()):
        """Yield statically named workspace modules and package initializers."""
        if level:
            package = path.parent
            for _ in range(level - 1):
                if package == root:
                    return
                package = package.parent
            if not package.is_relative_to(root):
                return
            ancestor = package
            while ancestor.is_relative_to(root):
                initializer = ancestor / "__init__.py"
                if initializer.is_file():
                    yield initializer
                if ancestor == root:
                    break
                ancestor = ancestor.parent
            base_dirs = [package]
        else:
            base_dirs = [path.parent, root]
        parts = module.split(".") if module else []
        for directory in dict.fromkeys(base_dirs):
            for count in range(0, len(parts) + 1):
                if count == 0:
                    base = directory
                else:
                    base = directory.joinpath(*parts[:count])
                if count:
                    candidates = (base.with_suffix(".py"), base / "__init__.py")
                    for candidate in candidates:
                        if candidate.is_file():
                            yield candidate
                # `from package import child` may refer to a submodule that
                # Python resolves after loading the package initializer.
                if count == len(parts) and base.is_dir():
                    for child in children:
                        child_base = base / child
                        for candidate in (child_base.with_suffix(".py"), child_base / "__init__.py"):
                            if candidate.is_file():
                                yield candidate
    queue, visited = [source], set()
    while queue:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            continue  # The source preflight reports invalid code, not this scanner.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = ((alias.name, 0, ()) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports = ((node.module or "", node.level,
                            tuple(alias.name for alias in node.names if alias.name != "*")) ,)
            else:
                continue
            for module, level, children in imports:
                for candidate in local_modules(path, module, level=level, children=children):
                    dependency = scoped(root, str(candidate))
                    if dependency != source:
                        files.add(dependency)
                    if dependency != source and dependency not in visited:
                        queue.append(dependency)
    return {"dependency:" + str(path.relative_to(root)): binding(path) for path in sorted(files)}


def _latest(root: Path, current: dict) -> dict | None:
    latest = current.get("latest")
    if latest is not None:
        return latest
    # Compatibility with sessions created before the task API. Discover only
    # known report locations inside this session; never scan neighbouring sessions.
    candidates = list(root.glob("*_compile-result.json")) + list(root.glob(".amagine3d-drafts/*/draft-result.json"))
    for path in sorted(candidates, key=lambda value: value.stat().st_mtime_ns, reverse=True):
        try:
            ref, report = read_report(root, str(path))
            if isinstance(report, dict) and report.get("schema") in {"evidence-cad-compile-result/v2", "a3d-draft-result/v2"}:
                return {"report": ref, "operation": "draft" if "draft" in report["schema"] else "compile",
                        "runId": report.get("runId"), "legacy": True}
        except (ValueError, OSError):
            continue
    return None


def remember_failure(root, operation, args, result):
    """Retain one exact failure for recovery; never replace a successful CAD run."""
    ref = result.get("report")
    if ref:
        _, document = read_report(root, ref["path"], ref["sha256"])
    else:
        document = result
        ref = binding(save(root, ".a3d-agent/errors/" + sha256(encode(result).encode()).hexdigest() + ".json", result))
    inputs = document.get("taskInputBindings", document.get("inputBindings", {}))
    if not inputs:
        inputs = {}
        for name in ("design", "intent", "source", "scene"):
            if name in args:
                path = scoped(root, args[name], exists=False)
                if path.is_file():
                    inputs[name] = binding(path)
    current = state(root)
    version = capability_version()
    key = sha256(encode([version, operation, args, inputs, [i.get("id") for i in document.get("issues", [])]]).encode()).hexdigest()
    previous = current.get("lastFailure", {})
    current["lastFailure"] = {"operation": operation, "report": ref, "inputs": inputs, "requestHash": key, "capabilityVersion": version,
                              "request": {k: args[k] for k in ("action", "design", "revision", "baseRevision", "source", "intent", "scene") if k in args},
                              "repeatedAttempts": previous.get("repeatedAttempts", 0) + 1 if previous.get("requestHash") == key else 1}
    _activate_inputs(root, current, inputs)
    save(root, ".a3d-agent/state.json", current)
    return ref


def _activate_inputs(root, current, inputs):
    for kind in ("design", "source"):
        if kind in inputs:
            active = {"kind": kind, "path": inputs[kind]["path"]}
            if current.get("activeInput") != active: current.pop("activeIntent", None)
            current["activeInput"] = active
            if kind == "design": save(root, ".a3d-agent/design.json", {"path": inputs[kind]["path"]})
            break
    if "intent" in inputs: current["activeIntent"] = inputs["intent"]


def _archive_resolved_failure(root, operation, args, result):
    """A lower-phase success cannot erase a current higher-phase failure."""
    current = state(root)
    previous = current.get("lastFailure")
    if not previous: return
    resolved = (previous.get("capabilityVersion") != capability_version() or
                not _paths_current(root, previous.get("inputs", {})) or not _active_matches(root, current, previous.get("inputs", {})))
    if not resolved:
        try:
            _, before = read_report(root, previous["report"]["path"], previous["report"]["sha256"])
            _, after = read_report(root, result["report"]["path"], result["report"]["sha256"])
        except (KeyError, OSError, ValueError):
            return  # Missing evidence cannot establish that the failure was fixed.
        if operation == "build" and args.get("phase") == "compile" and after.get("pass") is True:
            resolved = True
        elif operation == "validate" and (previous["operation"] in {"validate", "design"} or before.get("schema") == SCHEMA):
            required = set(previous.get("inputs", {})) & {"design", "source", "intent", "scene"}
            resolved = required <= set(after.get("inputBindings", {}))
        elif operation == "build" and args.get("phase") == "draft" and before.get("schema") == "a3d-draft-result/v2":
            resolved = after.get("status") == "draft"
        elif operation == "display" and after.get("parentReport", {}).get("sha256") == previous["report"]["sha256"]:
            stages = before.get("artifactStages", {})
            resolved = (stages.get("geometry", {}).get("status") == "passed" and stages.get("cadExport", {}).get("status") == "passed" and
                        stages.get("manufacturing", {}).get("status") != "failed" and
                        any(stages.get(phase, {}).get("status") == "failed" for phase in ("tessellation", "display", "preview")))
    if resolved:
        current.pop("lastFailure", None)
        current["failureHistory"] = [*current.get("failureHistory", []), previous][-8:]
        save(root, ".a3d-agent/state.json", current)


def remember_capabilities(root, page, version, group):
    current = state(root)
    if current.get("capabilityVersion") != version:
        current.update(symbols=[], designOps=[], capabilityReports={})
    reports = current.setdefault("capabilityReports", {}).setdefault(group, {})
    provided = []
    for item in page["items"]:
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        if group == "designOps":
            if value.get("status") != "supported" or not isinstance(value.get("required"), dict) or "field" in value["required"]:
                continue
            name = item["field"].rsplit("/", 1)[1]
        else:
            if not all(isinstance(value.get(k), str) for k in ("name", "signature", "description")):
                continue
            name = value["name"]
        provided.append(name)
        reports[name] = {"report": page["report"]["path"], "sha256": page["report"]["sha256"], "field": item["field"]}
    current.update(capabilityVersion=version)
    current[group] = sorted(set(current.get(group, [])) | set(provided))
    save(root, ".a3d-agent/state.json", current)


def context(root: Path, args: dict) -> dict:
    current = state(root)
    version = capability_version()
    latest = _latest(root, current)
    response = {"schema": SCHEMA, "workspace": str(root), "capabilityVersion": version,
                "capabilitiesChanged": current.get("capabilityVersion") not in (None, version),
                "previouslyProvidedSymbols": current.get("symbols", []) if current.get("capabilityVersion") == version else [],
                "previouslyProvidedDesignOps": current.get("designOps", []) if current.get("capabilityVersion") == version else [],
                "latest": None, "visualReview": "unknown; returning a preview path does not mean it has been viewed",
                "readMore": "a3d agent inspect using latest.report.path and latest.report.sha256",
                "recovery": "Read this context after compaction. Reuse known signatures unless capabilityVersion changed."}
    response["workflow"] = {
        "reports": "Use the exact report path/hash and page.next with agent inspect. Do not guess filenames, pipe output through head/tail, or write report parsers.",
        "modeling": "Batch supported operations through design apply, then build its returned revision. Read only the needed contracts/nodes; explain the missing capability before Python fallback.",
        "warnings": "Sampling warnings remain unverified until a targeted geometric check proves their cause; p05 or a curved edge alone does not prove a sampling artifact.",
    }
    if current.get("activeIntent"):
        try: response["intent"] = binding(scoped(root, current["activeIntent"]["path"]))
        except (OSError, ValueError): response["intent"] = {"status": "missing"}
    if latest:
        brief = {key: latest[key] for key in ("report", "runId", "operation", "legacy", "design") if key in latest}
        try:
            ref, report = read_report(root, latest["report"]["path"], latest["report"]["sha256"])
            response["latest"] = {**brief, "status": report.get("status"), "pass": report.get("pass", False),
                                  "visualReviewRequired": report.get("visualReviewRequired", True),
                                  "issueCounts": summary(report, ref)["issueCounts"],
                                  "inputsCurrent": _paths_current(root, latest.get("inputs", {})) and _active_matches(root, current, latest.get("inputs", {}))}
            if report.get("pass") or report.get("status") == "awaiting-preview":
                response["latest"]["inputsCurrent"] &= _intent_lineage_current(root, latest.get("inputs", {}))
            bound_report = _report_artifacts(root, ref, report)
            preview_key, preview_ref = _preview_binding(root, bound_report)
            if preview_ref:
                preview_field = "preview" if response["latest"]["inputsCurrent"] else "historicalPreview"
                response["latest"][preview_field] = {**preview_ref, "artifact": preview_key}
                response["latest"]["previewState"] = "current" if preview_field == "preview" else "historical"
            elif preview_key:
                response["latest"]["previewState"] = "stale-or-missing"
            inspect_request = {"report": ref["path"], "sha256": ref["sha256"]}
            response["nextInspect" if response["latest"]["inputsCurrent"] else "historicalInspect"] = inspect_request
            response["suggestedPhase"] = ("draft-feedback" if report.get("status") == "draft" else
                "visual-review" if report.get("pass") else "display-retry" if report.get("status") == "awaiting-preview" else "evidence-repair") if response["latest"]["inputsCurrent"] else "validate-current-inputs"
            if report.get("status") == "awaiting-preview" and response["latest"]["inputsCurrent"]:
                response["nextDisplay"] = inspect_request
        except (OSError, ValueError, KeyError) as error:
            response["latest"] = {**brief, "stale": True, "reason": str(error)}
    active = current.get("activeAttempt")
    if isinstance(active, dict):
        try:
            os.kill(active["pid"], 0)
            running = True
        except (OSError, KeyError, TypeError):
            running = False
        response["activeAttempt"] = {**active, "status": active.get("status", "running" if running else "interrupted")}
    active_design = None
    design_pointer = scoped(root, ".a3d-agent/design.json", exists=False)
    if design_pointer.exists() and current.get("activeInput", {}).get("kind") != "source":
        from design_api import read
        try:
            design = read(root, {"action": "read", "design": json.loads(design_pointer.read_text(encoding="utf-8"))["path"]})
            active_design = design
            response["design"] = {k: design[k] for k in ("design", "revision", "parts", "parameterIds", "requirementNodes", "operationsUsed", "report")}
            response["design"]["build"] = {k: design["build"][k] for k in ("status", "current")}
        except (DesignError, OSError, ValueError) as error:
            response["design"] = {"status": "invalid", "reason": str(error)}
    if active_design and not _active_matches(root, current, (latest or {}).get("inputs", {})):
        response["suggestedPhase"] = "functional-draft" if not active_design["build"]["current"] else "validate-current-inputs"
    if current.get("activeInput", {}).get("kind") == "source":
        try:
            response["source"] = binding(scoped(root, current["activeInput"]["path"]))
        except (OSError, ValueError): response["source"] = {"status": "missing"}
    validation = current.get("validation")
    if validation:
        try:
            ref, document = read_report(root, validation["report"]["path"], validation["report"]["sha256"])
            fresh = validation["capabilityVersion"] == version and _paths_current(root, document["inputBindings"]) and _active_matches(root, current, document["inputBindings"])
            response["validation"] = {"report": ref, "status": document["status"], "inputsCurrent": fresh}
            if fresh and document["status"] == "valid" and not (response["latest"] or {}).get("inputsCurrent"):
                validated = document["inputBindings"]
                if "design" in validated and "intent" in validated:
                    response["nextBuild"] = {"phase": "compile", "design": validated["design"]["path"],
                        "revision": validated["design"]["sha256"], "intent": validated["intent"]["path"]}
                    response["suggestedPhase"] = "compile-current-inputs"
                elif all(key in validated for key in ("source", "intent", "scene")):
                    response["nextBuild"] = {"phase": "compile", **{key: validated[key]["path"] for key in ("source", "intent", "scene")}}
                    response["suggestedPhase"] = "compile-current-inputs"
        except (OSError, ValueError) as error:
            response["validation"] = {"stale": True, "reason": str(error)}
    failure = current.get("lastFailure")
    if failure:
        try:
            ref, document = read_report(root, failure["report"]["path"], failure["report"]["sha256"])
            fresh = failure["capabilityVersion"] == version and _paths_current(root, failure["inputs"]) and _active_matches(root, current, failure["inputs"])
            response["lastFailure"] = {"operation": failure["operation"], "report": ref,
                "inputsCurrent": fresh, "repeatedAttempts": failure["repeatedAttempts"],
                "issueCounts": summary(document, ref)["issueCounts"]}
            if fresh:
                response["nextInspect"] = {"report": ref["path"], "sha256": ref["sha256"]}
                response["suggestedPhase"] = "contract-repair" if failure["operation"] in {"validate", "design"} or not document.get("runId") else "evidence-repair"
                request = failure["request"]
                if request.get("design") and request.get("intent"):
                    response["nextValidate"] = {k: request[k] for k in ("design", "revision", "intent") if k in request}
                design = response.get("design", {})
                addresses = set((active_design or {}).get("nodeIds", [])) | set(design.get("parameterIds", []))
                node_ids = [issue.get("node", issue.get("nodeId")) or design.get("requirementNodes", {}).get(issue.get("featureId"))
                            for issue in document.get("issues", [])]
                node_ids.extend(node for issue in document.get("issues", []) for node in issue.get("relatedNodes", []))
                ids = list(dict.fromkeys(node for node in node_ids if node in addresses))[:20]
                if ids:
                    response["nextDesignRead"] = {"action": "read", "design": design["design"], "revision": design["revision"],
                        "view": "nodes", "ids": ids, "include": ["directDependencies", "directDependents"]}
        except (OSError, ValueError) as error:
            response["lastFailure"] = {"stale": True, "reason": str(error)}
    if current.get("capabilityVersion") == version:
        response["capabilityReports"] = current.get("capabilityReports", {})
    response.setdefault("suggestedPhase", "functional-draft" if active_design else "classify")
    if ((response.get("latest") or {}).get("status") == "awaiting-preview" and
            response["latest"].get("inputsCurrent")):
        response["suggestedPhase"] = "display-retry"
    if current.get("failureHistory"):
        response["historicalFailures"] = [{"operation": entry["operation"], "report": entry["report"], "scope": "history",
            "inputsCurrent": _paths_current(root, entry["inputs"]) and _active_matches(root, current, entry["inputs"])}
            for entry in current["failureHistory"][-3:]]
    checkpoint = current.get("checkpoint")
    if checkpoint:
        try:
            read_report(root, checkpoint["report"]["path"], checkpoint["report"]["sha256"])
            checkpoint_bound = True
        except (OSError, ValueError): checkpoint_bound = False
        response["checkpoint"] = {**checkpoint, "inputsCurrent": checkpoint_bound and checkpoint["capabilityVersion"] == version and
            _paths_current(root, checkpoint["inputs"]) and _active_matches(root, current, checkpoint["inputs"]),
            "scope": "agent-reported observations; not machine acceptance or proof of visual review"}
        if response["checkpoint"]["inputsCurrent"] and checkpoint.get("functionalReady") and response["suggestedPhase"] == "draft-feedback":
            response["suggestedPhase"] = "contract"
    response["nextAction"] = {
        "display-retry": "Manufacturing checks passed. Run agent display with nextDisplay to regenerate the STEP preview without executing the model source.",
        "compile-current-inputs": "Run nextBuild for these already validated current inputs before further cosmetic edits.",
        "functional-draft": "Build the current revision's functional skeleton using only needed contracts.",
        "draft-feedback": "Review this draft once, then declare independent intent and validate/compile the functional assembly before cosmetic refinements.",
        "contract": "Use the minimal matching intent example, then validate and compile current inputs before further cosmetic edits.",
        "evidence-repair": "Inspect the current issue owner and location before another edit; repeated failures need localization, not broader geometry changes.",
        "contract-repair": "Use the exact contract issue and nextDesignRead; repair bindings without adding product geometry or relaxing intent.",
        "visual-review": "Open the actual current preview and record review scope; automatic pass does not complete visual review.",
    }.get(response["suggestedPhase"], "Validate current inputs; reuse unchanged capability reports and inspect only missing evidence.")
    # Persist full context so a large symbol history can be referenced exactly.
    path = save(root, ".a3d-agent/contexts/" + sha256(encode(response).encode()).hexdigest() + ".json", response)
    response["report"] = binding(path)
    response["previouslyProvidedSymbols"] = compact(response["previouslyProvidedSymbols"], "/previouslyProvidedSymbols", budget=1_000)
    for key in ("capabilityReports",):
        if key in response:
            response[key] = compact(response[key], "/" + key, budget=1_200)
    if active_design:
        for key in ("requirementNodes", "parameterIds", "operationsUsed"):
            response["design"][key] = compact(response["design"][key], "/design/" + key, budget=1_200)
    # Context itself does not mark capability documentation or images as read.
    return response


def capabilities(root: Path, args: dict) -> dict:
    version = capability_version()
    if "designOps" in args:
        from design_language import capabilities as design_capabilities
        payload = design_capabilities(args["designOps"])
        payload["capabilityVersion"] = version
        path = save(root, ".a3d-agent/capabilities/" + sha256(encode(payload).encode()).hexdigest() + ".json", payload)
        page = inspect(payload, binding(path), field="/operations", cursor=args.get("cursor"),
                       metadata={"capabilityVersion": version, "designSchema": payload["schema"], "limits": payload["limits"],
                                 "fieldTypes": compact(payload["fieldTypes"], "/fieldTypes", budget=3_000),
                                 "parameterDefinition": compact(payload["parameterDefinition"], "/parameterDefinition", budget=800),
                                 "operationNames": compact(payload["operationNames"], "/operationNames", budget=1_000)})
        remember_capabilities(root, page, version, "designOps")
        return page
    from capability_manifest import build_manifest
    manifest = build_manifest(args.get("symbols", []))
    payload = {key: manifest[key] for key in ("schema", "fingerprint", "runtime")}
    payload["capabilityVersion"] = version
    if args.get("symbols"):
        payload["query"] = manifest["query"]
    else:
        payload["symbols"] = {"build123d": manifest["build123d"]["publicSymbols"],
                              "helpers": [item["name"] for name, items in manifest["authoring"].items()
                                          if name.endswith(("Helpers", "Recipes")) and isinstance(items, list)
                                          for item in items if "name" in item]}
        payload["contract"] = manifest["authoring"]["contract"]
        payload["queryHint"] = "Supply symbols in one batch for signatures, parameter semantics and legal values."
    content_hash = sha256(encode(payload).encode()).hexdigest()
    path = save(root, f".a3d-agent/capabilities/{content_hash}.json", payload)
    page = inspect(payload, binding(path), field="/query" if args.get("symbols") else "", cursor=args.get("cursor"),
                   metadata={"capabilityVersion": version})
    remember_capabilities(root, page, version, "symbols")
    return page


def build(root: Path, args: dict) -> dict:
    from cad_compile import CompileOptions, compile_cad
    from cad_draft import run_draft
    design_info, output_dir = None, root
    if "design" in args:
        from design_api import prepare
        args, design_info, output_dir = prepare(root, args)
    source = scoped(root, args["source"])
    intent = scoped(root, args["intent"]) if args.get("intent") else None
    phase = args["phase"]
    if phase == "compile" and (intent is None or not args.get("scene")):
        raise RequestError("Python compile needs source, intent and a scene path; source may generate that file",
                           requiredFields=["phase", "source", "intent", "scene"])
    scene = scoped(root, args["scene"], exists=False) if args.get("scene") else None
    requested_scene = str(scene) if scene else None
    current = state(root)
    version = capability_version()
    inputs = {"source": binding(source), **({"intent": binding(intent)} if intent else {}),
              **source_dependencies(root, source, args.get("dependencies", []))}
    if design_info:
        inputs["design"] = {"path": design_info["path"], "sha256": design_info["revision"]}
    if intent:
        contract = json.loads(intent.read_text(encoding="utf-8"))
        profile = contract.get("printability", {}).get("profile", {})
        if isinstance(profile, dict) and isinstance(profile.get("path"), str):
            inputs["profile"] = binding(scoped(root, str(intent.parent / profile["path"])))
    latest = current.get("latest", {})
    _activate_inputs(root, current, inputs)
    # Reuse only completely bound success from this API/version. Failed or legacy
    # evidence never suppresses a retry. Checking artifacts catches deleted exports.
    if (latest.get("operation") == phase and latest.get("capabilityVersion") == version
            and latest.get("scene") == requested_scene):
        try:
            ref, previous = read_report(root, latest["report"]["path"], latest["report"]["sha256"])
            comparable = {k: v for k, v in latest.get("inputs", {}).items() if k != "scene"}
            if (inputs == comparable and _paths_current(root, latest.get("inputs", {}))
                    and previous.get("schema") == ("a3d-draft-result/v2" if phase == "draft" else "evidence-cad-compile-result/v2")
                    and previous.get("status") in {"draft", "awaiting-visual-review"}
                    and (phase == "draft" or previous.get("pass") is True)
                    and _paths_current(root, latest.get("outputs", {}))):
                if phase == "draft" or _intent_lineage_current(root, inputs):
                    save(root, ".a3d-agent/state.json", current)
                    return summary(previous, ref, metadata_extra={"reused": True, "inputsCurrent": True,
                                   **({"design": design_info} if design_info else {})})
        except (OSError, ValueError, KeyError):
            pass
    timeout = args.get("timeoutSeconds", 120 if phase == "draft" else 5_400)
    attempt_id = str(uuid4())
    if phase == "compile":
        output_dir = output_dir / "attempts" / attempt_id if design_info else root / "cad-builds" / attempt_id
        output_dir.mkdir(parents=True, exist_ok=False)
        if design_info:
            scene = output_dir / Path(args["scene"]).name
    operation_log = scoped(root, f".a3d-agent/operations/{attempt_id}.log", exists=False)
    operation_log.parent.mkdir(parents=True, exist_ok=True)
    current["activeAttempt"] = {"attemptId": attempt_id, "phase": phase, "pid": os.getpid(),
                                "log": str(operation_log), "startedAt": datetime.now(timezone.utc).isoformat()}
    if design_info:
        from design_api import record_build
        current["activeAttempt"]["design"] = design_info
        record_build(root, design_info, {"status": "running", "pid": os.getpid(), "phase": phase})
        source.with_name("design-diagnostics.json").unlink(missing_ok=True)
        source.with_name("node-evidence.json").unlink(missing_ok=True)
    save(root, ".a3d-agent/state.json", current)
    # Keep progress/diagnostic logs out of model-facing output. Context exposes
    # the running attempt; interruption is recoverable without repeating a build.
    try:
        with operation_log.open("w", buffering=1) as stream, redirect_stdout(stream), redirect_stderr(stream):
            if phase == "draft":
                result = run_draft(source, workspace=root, intent=intent, timeout_seconds=timeout)
            else:
                result = compile_cad(CompileOptions(workspace=root, marker=None, source=source, intent=intent,
                                                   scene=scene, output_dir=output_dir, compile_timeout_seconds=timeout,
                                                   source_timeout_seconds=min(1_800, timeout),
                                                   check_timeout_seconds=min(600, timeout)))
                # compile_cad's compatibility return is itself a projection.
                # Snapshot the full saved evidence, including input bindings and
                # warning history; references must point into that full document.
                _, saved = read_report(root, result["result"]["path"])
                if saved.get("runId") != result["runId"]:
                    raise ValueError("compile result was replaced before it could be read")
                result = saved
    except Exception:
        current["activeAttempt"]["status"] = "api-error"
        save(root, ".a3d-agent/state.json", current)
        if design_info:
            record_build(root, design_info, {"status": "api-error", "phase": phase})
        raise
    if design_info:
        result["design"] = design_info
        diagnostics = source.with_name("design-diagnostics.json")
        if diagnostics.exists():
            detail = json.loads(diagnostics.read_text(encoding="utf-8"))
            from cad_diagnostics import add_repair_hint, generic_diagnostic_hint
            result.setdefault("issues", []).extend(
                add_repair_hint(
                    {"stage": "design", **issue, "designRevision": design_info["revision"]},
                    generic_diagnostic_hint(),
                )
                for issue in detail["issues"]
            )
            result["designEvidence"] = detail["evidence"]
            result.update(status="failed", **{"pass": False})
        evidence = source.with_name("node-evidence.json")
        if evidence.exists():
            result["designEvidence"] = json.loads(evidence.read_text(encoding="utf-8"))
    # Keep immutable API snapshots: compile's conventional latest report may change
    # while a previous issue or cursor is still in the model's context.
    from cad_compile import _issue_identity
    for issue in result.get("issues", []):
        issue.setdefault("severity", "error")
        issue.setdefault("stage", phase)
        if "partId" in issue:
            issue.setdefault("part", issue["partId"])
        issue.setdefault("id", _issue_identity(issue))
    if scene and scene.is_file():
        inputs["scene"] = binding(scene)
    result["taskInputBindings"] = inputs
    if result.get("cadStage") and result.get("artifactStages", {}).get("cadExport", {}).get("status") == "passed":
        # The CLI snapshot below is the stable request target for a display retry.
        result["retryDisplay"] = {"operation": "display", "args": {"report": str(root / f".a3d-agent/runs/{result['runId']}.json")}}
    path = save(root, f".a3d-agent/runs/{result['runId']}.json", result)
    outputs = {}
    artifact_refs = {}
    build_report_ref = result.get("artifacts", {}).get("buildReport")
    if isinstance(build_report_ref, dict) and isinstance(build_report_ref.get("path"), str):
        try:
            _, build_report = read_report(root, build_report_ref["path"], build_report_ref.get("sha256"))
            if isinstance(build_report, dict) and isinstance(build_report.get("artifacts"), dict):
                artifact_refs.update(build_report["artifacts"])
        except (OSError, ValueError):
            pass
    for key, ref in result.get("artifacts", {}).items():
        previous = artifact_refs.get(key)
        same_binding = (isinstance(previous, dict) and isinstance(ref, dict)
                        and previous.get("path") == ref.get("path")
                        and previous.get("sha256") == ref.get("sha256"))
        if key not in artifact_refs or not (same_binding and previous.get("scene")
                                            and isinstance(ref, dict) and not ref.get("scene")):
            artifact_refs[key] = ref
    for key, ref in artifact_refs.items():
        if isinstance(ref, dict) and isinstance(ref.get("path"), str) and isinstance(ref.get("sha256"), str):
            outputs[key] = ref
        elif isinstance(ref, str):
            try:
                outputs[key] = binding(scoped(root, ref))
            except (OSError, ValueError):
                if result.get("pass") is True:
                    raise ValueError(f"successful build is missing its declared output: {key}")
    for key, ref in result.get("deliverables", {}).items():
        if key in outputs:
            continue
        if isinstance(ref, str):
            try:
                outputs[key] = binding(scoped(root, ref))
            except (OSError, ValueError):
                if result.get("pass") is True:
                    raise ValueError(f"successful build is missing its declared output: {key}")
        elif isinstance(ref, dict) and isinstance(ref.get("path"), str) and isinstance(ref.get("sha256"), str):
            outputs[key] = ref
    for key in ("log", "geometry", "cadStage"):
        if isinstance(result.get(key), dict):
            outputs[key] = result[key]
    current["latest"] = {"operation": phase, "report": binding(path), "runId": result["runId"],
                         "inputs": inputs, "outputs": outputs, "scene": requested_scene,
                         "capabilityVersion": version}
    if design_info:
        current["latest"]["design"] = design_info
        record_build(root, design_info, {"status": result["status"], "pass": result.get("pass", phase == "draft"),
                                       "report": binding(path), "outputs": outputs, "phase": phase})
    current["runs"] = [*current.get("runs", []), current["latest"]][-20:]
    current.pop("activeAttempt", None)
    save(root, ".a3d-agent/state.json", current)
    return summary(result, binding(path), metadata_extra={"reused": False, "inputsCurrent": _paths_current(root, inputs),
                   **({"design": design_info} if design_info else {})})


def inspect_report(root: Path, args: dict) -> dict:
    ref, document = read_report(root, args["report"], args.get("sha256"))
    if isinstance(document, dict) and document.get("schema") == "a3d-agent-preview/v1":
        from agent_preview import _currentness
        projected = dict(document)
        observations = []
        image_states = []
        workspace = root.resolve()
        for item in document.get("observations", []):
            projected_item = dict(item)
            image_ref = item.get("image", {}) if isinstance(item, dict) else {}
            image_path = Path(image_ref.get("path", "")) if isinstance(image_ref, dict) else Path()
            try:
                resolved = image_path.resolve(strict=True)
                image_current = (resolved.is_relative_to(workspace) and resolved.is_file()
                                 and sha256(resolved.read_bytes()).hexdigest() == image_ref.get("sha256"))
            except (OSError, RuntimeError, ValueError):
                image_current = False
            image_states.append({"id": item.get("id"), "current": image_current})
            if not image_current:
                projected_item.pop("image", None)
            observations.append(projected_item)
        projected["observations"] = observations
        parent_ref = document.get("parentReport", {})
        metadata = {"previewCurrentness": {"status": "stale"}}
        try:
            bound_parent, parent = read_report(root, parent_ref["path"], parent_ref["sha256"])
            if bound_parent != parent_ref:
                raise ValueError("parent report binding changed")
            status = _currentness(root, parent, parent_ref)
            source_binding = document.get("sourceArtifact", {}).get("binding", {})
            status["sourceCurrent"] = _paths_current(root, {"sourceArtifact": source_binding})
            status["historical"] = status["historical"] or not status["sourceCurrent"]
            status["imagesCurrent"] = all(item["current"] for item in image_states)
            status["images"] = image_states
            status["status"] = ("stale" if not status["imagesCurrent"] else
                                 "historical" if status["historical"] else "current")
            metadata = {"previewCurrentness": status}
        except (KeyError, OSError, TypeError, ValueError):
            pass
        page = inspect(projected, ref, field=args.get("field", ""), issue_id=args.get("issueId"),
                       cursor=args.get("cursor"), metadata=metadata)
        return page
    if "field" not in args and "issueId" not in args and isinstance(document, dict) and "issues" in document:
        projected, metadata = _summary_document(root, _report_artifacts(root, ref, document))
        return summary(projected, ref, args.get("cursor"), metadata_extra=metadata)
    page = inspect(document, ref, field=args.get("field", ""), issue_id=args.get("issueId"), cursor=args.get("cursor"))
    if (Path(ref["path"]).is_relative_to(root / ".a3d-agent/capabilities")
            and document.get("capabilityVersion") == capability_version()):
        remember_capabilities(root, page, document["capabilityVersion"], "designOps" if "operations" in document else "symbols")
    return page


def checkpoint(root, args):
    ref, document = read_report(root, args["report"], args.get("sha256"))
    if document.get("schema") not in {"a3d-draft-result/v2", "evidence-cad-compile-result/v2"}:
        raise RequestError("checkpoint needs a bound CAD run report")
    inputs = document.get("taskInputBindings", document.get("inputBindings", {}))
    if not inputs and document.get("source"): inputs = {"source": document["source"]}
    current = state(root)
    if not _paths_current(root, inputs) or not _active_matches(root, current, inputs):
        raise RequestError("checkpoint inputs are outdated; recover context for the active revision")
    for key in ("observations", "todo"):
        values = args.get(key, [])
        if not isinstance(values, list) or len(values) > 6 or any(not isinstance(value, str) or not 1 <= len(value) <= 300 for value in values):
            raise RequestError(key + " needs at most six concise strings")
    current["checkpoint"] = {"report": ref, "inputs": inputs, "capabilityVersion": capability_version(),
                             "observations": args.get("observations", []), "todo": args.get("todo", []),
                             "functionalReady": args.get("functionalReady", False)}
    save(root, ".a3d-agent/state.json", current)
    return {"schema": SCHEMA, "status": "saved", "scope": "agent-reported checkpoint; no CAD acceptance changed", "report": ref}


def validate(root: Path, args: dict) -> dict:
    from intent_contract import validate as intent_validate
    from scene_contract import validate as scene_validate
    from source_preflight import audit
    from cad_diagnostics import add_repair_hint, contract_issue, generic_diagnostic_hint
    issues, inputs, design_ir = [], {}, None
    if "design" in args:
        from design_api import load
        from design_language import normalize
        path, document, revision = load(root, args["design"], args.get("revision"))
        design_ir = normalize(document)
        inputs["design"] = {"path": str(path), "sha256": revision}
        save(root, ".a3d-agent/design.json", {"path": str(path)})
        current = state(root)
        _activate_inputs(root, current, inputs)
        save(root, ".a3d-agent/state.json", current)
    for kind in ("intent", "scene", "source"):
        if kind not in args:
            continue
        path = scoped(root, args[kind])
        inputs[kind] = binding(path)
        if kind == "source":
            issues.extend(audit(path).get("errors", []))
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError(f"{kind} must be a JSON object")
            if kind == "intent":
                printability = document.get("printability")
                profile = printability.get("profile") if isinstance(printability, dict) else None
                if isinstance(profile, dict) and isinstance(profile.get("path"), str):
                    profile_path = scoped(root, str(path.parent / profile["path"]), exists=False)
                    if profile_path.is_file():
                        inputs["profile"] = binding(profile_path)
            if kind == "intent" and design_ir is not None:
                from design_api import contract_issues
                issues.extend(contract_issues(design_ir, document, base_dir=path.parent, revision=revision))
            else:
                errors = (intent_validate if kind == "intent" else scene_validate)(document, base_dir=path.parent)
                issues.extend({"severity": "error", "code": "AUTHORING.INVALID", "message": str(message),
                               "path": kind, **(contract_issue(message) or {})} for message in errors)
            if kind == "intent" and isinstance(document.get("part"), str):
                from intent_revision import audit_lineage, lineage_issue
                # Check without freezing a draft contract or committing history.
                try:
                    history, _ = audit_lineage(root, path, document, persist_snapshots=False)
                    from intent_revision import history_path
                    recorded = history_path(root, document["part"])
                    if recorded.is_file():
                        inputs["intentHistory"] = binding(recorded)
                    for checksum, entry in history["entries"].items():
                        inputs["intentParent:" + checksum] = binding(scoped(root, entry["path"]))
                        if entry.get("byteSnapshot"):
                            inputs["intentSnapshot:" + checksum] = binding(scoped(root, entry["byteSnapshot"]["path"]))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    issues.append(lineage_issue(error))
    if not inputs:
        raise ValueError("validate needs at least one of design, intent, scene or source")
    if not _paths_current(root, inputs):
        raise ValueError("inputs changed during validation")
    result = {"schema": "a3d-validation/v1", "runId": str(uuid4()), "status": "failed" if issues else "valid",
              "pass": not issues, "issues": issues, "inputBindings": inputs,
              "limitations": ["Contract/source preflight only; no geometry or manufacturing acceptance."]}
    from cad_compile import _issue_identity
    for index, issue in enumerate(issues):
        issue.setdefault("code", "SOURCE.PREFLIGHT_FAILED")
        issue.setdefault("severity", "error")
        issue.setdefault("stage", "preflight")
        if issue.get("name"):
            issue.setdefault("subjectId", str(issue.get("module", "")) + "." + issue["name"])
        issue = add_repair_hint(issue, generic_diagnostic_hint())
        issues[index] = issue
        issue.setdefault("id", _issue_identity(issue))
    path = save(root, f".a3d-agent/validations/{result['runId']}.json", result)
    current = state(root)
    _activate_inputs(root, current, inputs)
    current["validation"] = {"report": binding(path), "capabilityVersion": capability_version()}
    save(root, ".a3d-agent/state.json", current)
    return summary(result, binding(path))


OPERATIONS = {"context": context, "capabilities": capabilities, "design": design, "build": build,
              "inspect": inspect_report, "validate": validate, "checkpoint": checkpoint}


def display(root, args):
    from cad_display_retry import retry_display
    return retry_display(root, args)


OPERATIONS["display"] = display


def preview(root, args):
    from agent_preview import preview as create_preview
    return create_preview(root, args)


OPERATIONS["preview"] = preview


def check_arguments(operation: str, args: dict) -> None:
    if operation not in ARGUMENTS:
        raise ValueError("unknown task operation; choose " + ", ".join(ARGUMENTS))
    spec = ARGUMENTS[operation]
    if not isinstance(args, dict) or set(args) - set(spec):
        raise RequestError("unexpected arguments; allowed: " + ", ".join(spec), allowedFields=list(spec))
    missing = REQUIRED.get(operation, set()) - set(args)
    if missing:
        raise RequestError("missing required arguments: " + ", ".join(sorted(missing)), requiredFields=sorted(missing))
    for key, value in args.items():
        if spec[key] is bool and type(value) is not bool:
            raise RequestError(key + " must be a boolean")
        if spec[key] is str and (not isinstance(value, str) or len(value) > 4_096):
            raise ValueError(f"{key} must be a string of at most 4096 characters")
        if spec[key] is float and (type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 5_400):
            raise ValueError(f"{key} is outside its allowed range")
        if key == "changes":
            if not isinstance(value, list) or len(value) > 64 or any(not isinstance(item, dict) for item in value):
                raise RequestError("changes must be an array of at most 64 edit objects; each smaller transaction must have valid dependencies",
                                   maximumChanges=64, actualChanges=len(value) if isinstance(value, list) else None,
                                   repairHint="Submit a valid dependency-complete batch, then use its returned revision as the next baseRevision.")
            continue
        if spec[key] is list:
            if operation == "preview" and key == "requests":
                if not isinstance(value, list) or not value or len(value) > 4 or any(not isinstance(item, dict) for item in value):
                    raise RequestError("requests must be an array of 1..4 observation objects", field=key, maximumItems=4)
                continue
            maximum_length = 4_096 if key == "dependencies" else 300 if operation == "checkpoint" else 200
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > maximum_length for item in value
            ):
                raise RequestError(f"{key} must be an array of nonempty names of at most {maximum_length} characters", field=key)
            if operation != "capabilities" and len(value) > 20:
                raise RequestError(f"{key} must contain 0..20 nonempty names", field=key, maximumItems=20)
    if operation == "build" and args["phase"] not in {"draft", "compile"}:
        raise ValueError("phase must be draft or compile")
    if operation in {"build", "validate"} and "source" in args and "design" in args:
        raise ValueError("source and design are mutually exclusive")
    if operation == "build":
        if not {"source", "design"} & set(args):
            raise ValueError("build needs source or design")
        if "design" in args and "scene" in args:
            raise ValueError("design builds generate scene; omit scene")
        if args["phase"] == "compile":
            required = {"phase", "design", "revision", "intent"} if "design" in args else {"phase", "source", "intent", "scene"}
            if required - set(args):
                raise RequestError("compile requires " + ", ".join(sorted(required))
                    + "; Python scene is a destination path, design scene is generated", requiredFields=sorted(required))
        if "revision" in args and "design" not in args:
            raise ValueError("revision requires design")
    if operation == "capabilities" and "symbols" in args and "designOps" in args:
        raise ValueError("query symbols or designOps in one request")
    if len(encode(args).encode()) > 65_536:
        raise ValueError("operation request exceeds 64 KiB")


def dispatch(root: Path, operation: str, args: dict) -> dict:
    started = time.monotonic()
    root = root.resolve()
    result = None
    status = "api-error"
    try:
        check_arguments(operation, args)
        if operation in {"capabilities", "build", "validate", "checkpoint", "display", "preview"} or operation == "design" and args.get("action") == "apply":
            with mutation_lock(root):
                result = OPERATIONS[operation](root, args)
        else:
            result = OPERATIONS[operation](root, args)
        if operation in {"build", "validate", "display"} and result.get("status") == "failed":
            remember_failure(root, operation, args, result)
        elif operation in {"build", "validate", "display"}:
            _archive_resolved_failure(root, operation, args, result)
        if wire_size(result) > 9_000:
            path = save(root, f".a3d-agent/responses/{uuid4()}.json", result)
            result = inspect(result, binding(path))
        status = result.get("status", "completed")
        return result
    except DesignError as error:
        from cad_diagnostics import add_repair_hint, generic_diagnostic_hint
        from cad_compile import _issue_identity
        error.issues = [add_repair_hint(issue, generic_diagnostic_hint()) for issue in error.issues]
        for issue in error.issues:
            issue.setdefault("id", _issue_identity(issue))
        result = {"schema": SCHEMA, "status": "failed", "pass": False, "issues": error.issues}
        if "inputBindings" in error.issue:
            result["inputBindings"] = error.issue["inputBindings"]
        if "currentRevision" in error.issue or "revision" in error.issue:
            result["revision"] = error.issue.get("currentRevision", error.issue.get("revision"))
        if operation in {"build", "validate"} or operation == "design" and args.get("action") == "apply":
            result["report"] = remember_failure(root, operation, args, result)
        if wire_size(result) > 9_000:
            path = save(root, f".a3d-agent/responses/{uuid4()}.json", result)
            result = summary(result, binding(path))
        status = "failed"
        return result
    except (KeyboardInterrupt, SystemExit):
        status = "interrupted"
        raise
    finally:
        try:
            metrics_path = scoped(root, ".a3d-agent/metrics.jsonl", exists=False)
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            with metrics_path.open("a") as stream:
                stream.write(encode({"operation": operation if operation in OPERATIONS else "unknown", "status": status,
                                     "at": datetime.now(timezone.utc).isoformat(),
                                     "elapsedMs": round((time.monotonic() - started) * 1_000),
                                     "inputBytes": len(encode(args).encode()), "wireBytes": wire_size(result) if result is not None else None,
                                     "reused": result.get("reused") if result else None,
                                     "runId": result.get("runId") if result else None}) + "\n")
        except (OSError, ValueError, TypeError):
            pass  # Telemetry never masks the real operation result or failure.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?")
    parser.add_argument("arguments", nargs="?", default="{}")
    parser.add_argument("--args-file", type=str, help="Read operation arguments from a JSON file in this workspace")
    parsed = parser.parse_args()
    if parsed.operation is None:
        parser.print_help()
        return 0
    try:
        if parsed.args_file and parsed.arguments != "{}":
            raise ValueError("use JSON arguments or --args-file, not both")
        raw_arguments = scoped(Path.cwd(), parsed.args_file).read_text(encoding="utf-8") if parsed.args_file else parsed.arguments
        request = {"operation": parsed.operation, "arguments": json.loads(raw_arguments)}
        result = dispatch(Path.cwd(), request["operation"], request.get("arguments", {}))
    except Exception as error:
        # Full exceptions belong in logs, while the tool gets a complete bounded
        # error message or a precise pointer to it, using the same navigator.
        result = {"schema": SCHEMA, "status": "api-error", "error": {"type": type(error).__name__, "message": str(error)}}
        if isinstance(error, RequestError):
            result["error"].update(error.details)
        if isinstance(error, json.JSONDecodeError):
            result["error"].update(line=error.lineno, column=error.colno, offset=error.pos,
                                  requestFile=parsed.args_file)
        from cad_diagnostics import api_error_repair_hint
        if not isinstance(result["error"].get("repairHint"), str) or not result["error"]["repairHint"].strip():
            result["error"]["repairHint"] = api_error_repair_hint(result["error"])
        if wire_size(result) > 8_000:
            path = save(Path.cwd(), f".a3d-agent/errors/{uuid4()}.json", result)
            result = {"schema": SCHEMA, "status": "api-error", "report": binding(path), "error": "Large error; inspect report /error"}
        print(encode(result), flush=True)
        return 2
    print(encode(result), flush=True)
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
