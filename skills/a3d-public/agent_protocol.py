"""Version-bound navigation over CAD evidence for the task CLI.

The projection selects useful evidence; it never edits the saved report. Large
values become JSON-pointer references, not clipped strings. The budget includes
JSON escaping in a shell tool's output envelope, with room for its metadata.
"""
from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

SCHEMA = "a3d-agent/v1"
MAX_WIRE_BYTES = 9_000
MAX_DIRECT_PREVIEW_BINDING_BYTES = 1_200


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def wire_size(value: Any) -> int:
    return len(encode({"output": encode(value)}).encode()) + 512


def sha256_file(path: Path) -> str:
    """Hash an artifact incrementally so large STEP bindings do not fill RAM."""
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binding(path: Path) -> dict:
    return {"path": str(path), "sha256": sha256_file(path)}


def scoped(root: Path, value: str, *, exists: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a nonempty workspace path")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path must stay inside the session workspace")
    if exists and not path.is_file():
        raise ValueError(f"file does not exist: {value}")
    return path


def read_report(root: Path, reference: str, expected_hash: str | None = None) -> tuple[dict, Any]:
    path = scoped(root, reference)
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError("report changed; obtain a new report reference before reading more")
    return {"path": str(path), "sha256": digest}, json.loads(raw)


def token(part: Any) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def at_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("field must be a JSON pointer, e.g. /inputBindings/source")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("invalid JSON pointer escape")
    value = document
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not part.isdigit():
                    raise ValueError("array index must be nonnegative")
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise ValueError("field traverses a scalar")
    except (KeyError, IndexError) as error:
        raise ValueError(f"unknown field: {pointer}") from error
    return value


def reference(value: Any, pointer: str) -> dict:
    return {"field": pointer, "type": type(value).__name__,
            **({"count": len(value)} if isinstance(value, (dict, list, str)) else {})}


def compact(value: Any, pointer: str, *, budget: int = 1_800) -> Any:
    if len(encode(value).encode()) <= budget:
        return value
    if isinstance(value, dict):
        result = {key: (item if len(encode(item).encode()) < 160 else reference(item, f"{pointer}/{token(key)}"))
                  for key, item in value.items()}
        if len(encode(result).encode()) <= budget:
            return result
    return reference(value, pointer)


def _direct_preview(value: Any, pointer: str) -> Any:
    """Keep the active image path usable by view_image within the bounded reply."""
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not isinstance(value.get("sha256"), str):
        return reference(value, pointer)
    selected = {key: value[key] for key in ("path", "sha256")}
    if len(encode(selected).encode()) > MAX_DIRECT_PREVIEW_BINDING_BYTES:
        raise ValueError("preview binding exceeds the direct image handoff budget")
    return selected


def issue_view(issue: dict, index: int) -> dict:
    pointer = f"/issues/{index}"
    value = dict(issue)
    observed = value.get("observed")
    # These producers explicitly distinguish offenders from complete observations.
    # No heuristic based on a feature's name or position may invent a root cause.
    if (value.get("check") in {"semantic_feature_placement", "intent_report_feature_ownership"}
            and isinstance(observed, dict) and isinstance(observed.get("offenders"), list)
            and isinstance(observed.get("observations"), list)):
        value["observed"] = {k: v for k, v in observed.items() if k != "observations"}
        value["observed"]["observationCounts"] = {
            "total": len(observed["observations"]), "offenders": len(observed["offenders"])}
        value["observed"]["allObservations"] = reference(observed["observations"], pointer + "/observed/observations")
    if (value.get("check") in {"printability_wall_thickness", "printability_local_thin_region"}
            and isinstance(observed, dict)):
        projected = dict(observed)
        sampling = observed.get("sampling")
        if isinstance(sampling, dict):
            projected["sampling"] = reference(sampling, pointer + "/observed/sampling")
            if "minimum_sample" in sampling:
                projected["minimumSample"] = compact(sampling["minimum_sample"], pointer + "/observed/sampling/minimum_sample", budget=650)
        if "affected_feature_attribution" in observed:
            projected["affected_feature_attribution"] = reference(observed["affected_feature_attribution"], pointer + "/observed/affected_feature_attribution")
        value["observed"] = projected
    if isinstance(observed, dict) and str(value.get("check", "")).startswith("printability_"):
        projected = dict(value["observed"])
        attribution = observed.get("affected_feature_attribution")
        if isinstance(attribution, dict):
            projected["affected_feature_attribution"] = reference(attribution, pointer + "/observed/affected_feature_attribution")
            if "basis" in attribution:
                projected["attributionBasis"] = compact(attribution["basis"], pointer + "/observed/affected_feature_attribution/basis", budget=300)
        candidates = observed.get("affected_feature_ids")
        if isinstance(candidates, list) and len(candidates) > 8:
            projected["affected_feature_ids"] = reference(candidates, pointer + "/observed/affected_feature_ids")
        value["observed"] = projected
    # Derived view keys have provenance in the original evidence. Do not run a
    # generic compactor over them: /observed/minimumSample is not a saved field.
    if value.get("observed") is not observed and isinstance(value.get("observed"), dict):
        projected = value["observed"]
        value["observed"] = {key: item if key in {"minimumSample", "allObservations", "observationCounts", "attributionBasis"}
                             else compact(item, pointer + "/observed/" + token(key), budget=800)
                             for key, item in projected.items()}
        if len(encode(value["observed"]).encode()) > 3_000:
            value["observed"] = reference(observed, pointer + "/observed")
    return {**{key: item if key == "observed" and item is not observed else compact(item, f"{pointer}/{token(key)}")
               for key, item in value.items()}, "evidenceField": pointer}


def _cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip("=")


def paginate(items: list, *, report: dict, selector: str, cursor: str | None = None,
             metadata: dict | None = None) -> dict:
    identity = sha256(encode([report, selector]).encode()).hexdigest()
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if decoded["identity"] != identity or type(decoded["offset"]) is not int:
                raise ValueError()
            offset = decoded["offset"]
            if not 0 <= offset < len(items):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError("cursor belongs to a different report/field or is invalid") from error
    result = {"schema": SCHEMA, "report": report, **(metadata or {}),
              "readMore": "a3d agent inspect; field references and page.next are explicit",
              "items": [], "page": {"total": len(items), "offset": offset, "returned": 0, "next": None, "nextRequest": None}}
    def page(returned, next_offset):
        cursor = _cursor({"identity": identity, "offset": next_offset}) if next_offset < len(items) else None
        result["page"].update(returned=returned, next=cursor, nextRequest={
            "report": report["path"], "sha256": report["sha256"],
            **({"field": selector} if selector != "issues" else {}), "cursor": cursor,
        } if cursor else None)
    if wire_size(result) > MAX_WIRE_BYTES - 1_000:
        raise ValueError("report metadata exceeds the transport budget; inspect the report directly")
    for index in range(offset, len(items)):
        item = items[index]
        page(len(result["items"]) + 1, index + 1)
        result["items"].append(item)
        if wire_size(result) > MAX_WIRE_BYTES:
            result["items"].pop()
            if not result["items"]:
                # An entire issue is still addressable even when its identity is enormous.
                result["items"].append(reference(item, item.get("evidenceField", item.get("field", ""))))
                if wire_size(result) > MAX_WIRE_BYTES:
                    raise ValueError("field reference exceeds the transport budget")
            else:
                page(len(result["items"]), index)
            break
    return result


def summary(document: dict, report: dict, cursor: str | None = None, *, metadata_extra: dict | None = None) -> dict:
    issues = [(index, item) for index, item in enumerate(document.get("issues", [])) if isinstance(item, dict)]
    issues.sort(key=lambda pair: pair[1].get("severity") == "warning")
    metadata = {key: compact(document[key], "/" + key, budget=300)
                for key in ("runId", "status", "pass", "model", "visualReviewRequired") if key in document}
    metadata.setdefault("visualReviewRequired", True)
    metadata.update(metadata_extra or {})
    metadata["issueCounts"] = {
        "errors": sum(item.get("severity") != "warning" for _, item in issues) + document.get("omittedErrorCount", 0),
        "warnings": sum(item.get("severity") == "warning" for _, item in issues),
        "producerOmitted": document.get("omittedIssueCount", 0),
    }
    artifacts = document.get("artifacts", {})
    # Preview/report paths get priority over lengthy intermediate artifact lists.
    active_preview = ("preview" if isinstance(artifacts.get("preview"), dict)
                      else "diagnosticPreview" if isinstance(artifacts.get("diagnosticPreview"), dict) else None)
    metadata["artifacts"] = {
        key: (_direct_preview(artifacts[key], "/artifacts/" + key) if key == active_preview
              else reference(artifacts[key], "/artifacts/" + key) if key in {"preview", "diagnosticPreview"}
              else compact(artifacts[key], "/artifacts/" + key, budget=100))
        for key in ("step", "step:display", "preview", "diagnosticPreview", "buildReport", "log") if key in artifacts
    }
    if "artifacts" in document:
        metadata["allArtifacts"] = reference(artifacts, "/artifacts")
    for key in ("repairDelta", "warningComparisons", "warningMeasurements", "printOrientationEvidence", "inputBindings", "source", "intent", "limitations", "previewScope", "log", "sourceDiagnostics", "cadStage", "artifactStages", "retryDisplay"):
        if key in document:
            metadata[key] = reference(document[key], "/" + key) if key in {"warningComparisons", "warningMeasurements", "inputBindings", "printOrientationEvidence"} else compact(document[key], "/" + key, budget=450)
    # Metadata must leave space for at least one issue, even with huge paths or
    # large repair ledgers. Every displaced field remains explicitly navigable.
    for key in reversed(list(metadata)):
        if wire_size(metadata) <= 3_500:
            break
        if key not in {"runId", "status", "pass", "visualReviewRequired", "issueCounts", "artifacts"}:
            metadata[key] = reference(document.get(key), "/" + key) if key in document else metadata[key]
    views, shared = [], {}
    comparisons = {item.get("id"): (i, item) for i, item in enumerate(document.get("warningComparisons", [])) if isinstance(item, dict)}
    for index, item in issues:
        view = issue_view(item, index)
        # Share identical explanations only. This never equates different frames
        # or measurements with a common geometric root cause.
        for key in ("message", "repairHint"):
            if isinstance(view.get(key), str):
                identity = (key, view[key])
                if identity in shared:
                    view[key] = {"sameAs": shared[identity]}
                else:
                    shared[identity] = f"/issues/{index}/{key}"
        if item.get("severity") == "warning" and item.get("id") in comparisons:
            comparison_index, comparison = comparisons[item["id"]]
            view["change"] = {key: compact(comparison[key], f"/warningComparisons/{comparison_index}/{key}", budget=450)
                              for key in ("status", "measurements", "changes") if key in comparison}
        views.append(view)
    return paginate(views, report=report,
                    selector="issues", cursor=cursor, metadata=metadata)


def inspect(document: Any, report: dict, *, field: str = "", issue_id: str | None = None,
            cursor: str | None = None, metadata: dict | None = None) -> dict:
    if issue_id is not None:
        if not isinstance(document, dict):
            raise ValueError("issueId needs a report object")
        matches = [(i, item) for i, item in enumerate(document.get("issues", []))
                   if isinstance(item, dict) and item.get("id") == issue_id]
        if len(matches) != 1:
            raise ValueError("issue ID must name exactly one issue in this report; use its field pointer otherwise")
        field = f"/issues/{matches[0][0]}" + field
    value = at_pointer(document, field)
    if isinstance(value, dict):
        items = [{"field": f"{field}/{token(k)}", "value": compact(v, f"{field}/{token(k)}", budget=3_000)} for k, v in value.items()]
    elif isinstance(value, list):
        items = [{"field": f"{field}/{i}", "value": compact(v, f"{field}/{i}", budget=3_000)} for i, v in enumerate(value)]
    elif isinstance(value, str) and len(value.encode()) > 2_000:
        # Explicit text chunks, with offsets, are reassemblable. Never present them as the whole field.
        items = [{"field": field, "textOffset": i, "textLength": len(value), "textChunk": value[i:i + 500]}
                 for i in range(0, len(value), 500)]
    else:
        items = [{"field": field, "value": value}]
    return paginate(items, report=report, selector=field, cursor=cursor, metadata=metadata)
