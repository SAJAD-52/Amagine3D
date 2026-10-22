"""Create request-bound visual observations from an existing STEP artifact."""
from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from build123d import Plane

from agent_protocol import MAX_WIRE_BYTES, binding, encode, read_report, scoped, wire_size
from cpu_z_buffer import DEFAULT_MAX_RESOLUTION, DEFAULT_MAX_TRIANGLES, MeshInput, RenderLimits, render_view, triangle_count


MANIFEST_SCHEMA = "a3d-agent-preview/v1"
RENDERER_VERSION = "cpu-z-buffer/v1"
MAX_REQUESTS = 4
MAX_OUTPUT_IMAGES = 8
MAX_TOTAL_PIXELS = 4_000_000
MAX_SELECTED_OBJECTS = 64
MAX_SOURCE_STEP_BYTES = 256 * 1024 * 1024
MAX_TOTAL_STEP_INPUT_BYTES = 256 * 1024 * 1024
MAX_COORDINATE_MM = 1_000_000_000_000.0
MAX_INLINE_NAME_BYTES = 120
DEFAULT_SIZE = 640
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_REGISTERED_MANIFESTS = 32


def _vec(value, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must be a 3-number array")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain finite numbers")
    return list(result)


def _unit_vec(value, name: str) -> list[float]:
    vector = _vec(value, name)
    scale = max(abs(component) for component in vector)
    if scale <= 1e-12:
        raise ValueError(f"{name} must be nonzero")
    scaled = [component / scale for component in vector]
    length = math.sqrt(sum(component * component for component in scaled))
    return [component / length for component in scaled]


def _coordinate(value, name: str) -> list[float]:
    vector = _vec(value, name)
    if any(abs(component) > MAX_COORDINATE_MM for component in vector):
        raise ValueError(f"{name} coordinates must be within +/-{MAX_COORDINATE_MM} mm")
    return vector


def _selected_names(request: dict) -> list[str] | None:
    names = request.get("partIds")
    if names is None:
        return None
    if (not isinstance(names, list) or not names or len(names) > MAX_SELECTED_OBJECTS
            or any(not isinstance(name, str) or not name.strip() or len(name.encode()) > 200 for name in names)
            or len(names) != len(set(names))):
        raise ValueError("partIds must be a nonempty array of unique object names")
    return names


def _normalize_request(request: dict, index: int) -> dict:
    if not isinstance(request, dict):
        raise ValueError("each preview request must be an object")
    kind = request.get("kind")
    if kind == "camera":
        allowed = {"kind", "id", "partIds", "view", "direction", "up", "target", "scope", "size"}
        if set(request) - allowed:
            raise ValueError("camera request has unknown fields: " + ", ".join(sorted(set(request) - allowed)))
        if ("view" in request) == ("direction" in request or "up" in request):
            raise ValueError("camera request needs either view or both direction and up")
        if ("direction" in request) != ("up" in request):
            raise ValueError("camera direction and up must be supplied together")
        size = request.get("size", DEFAULT_SIZE)
        if type(size) is not int or not 320 <= size <= DEFAULT_MAX_RESOLUTION:
            raise ValueError(f"camera size must be an integer from 320 to {DEFAULT_MAX_RESOLUTION}")
        result = {"kind": kind, "id": request.get("id", f"view-{index + 1}"), "size": size,
                  "partIds": _selected_names(request)}
        if not isinstance(result["id"], str) or not result["id"].strip() or len(result["id"].encode()) > 80:
            raise ValueError("camera id must be a nonempty string of at most 80 bytes")
        if "view" in request:
            from cpu_z_buffer import SUPPORTED_VIEWS
            if request["view"] not in SUPPORTED_VIEWS:
                raise ValueError("view must be one of " + ", ".join(SUPPORTED_VIEWS))
            result["view"] = request["view"]
        else:
            result["direction"] = _unit_vec(request["direction"], "direction")
            result["up"] = _unit_vec(request["up"], "up")
            direction, up = result["direction"], result["up"]
            cross = [direction[1] * up[2] - direction[2] * up[1],
                     direction[2] * up[0] - direction[0] * up[2],
                     direction[0] * up[1] - direction[1] * up[0]]
            if math.hypot(*cross) <= 1e-10:
                raise ValueError("camera up must not be parallel to camera direction")
        if "target" in request:
            result["target"] = _coordinate(request["target"], "target")
        if "scope" in request:
            scope = request["scope"]
            if not isinstance(scope, dict) or set(scope) != {"min", "max"}:
                raise ValueError("camera scope needs exactly min and max 3D corners")
            low, high = _coordinate(scope["min"], "scope.min"), _coordinate(scope["max"], "scope.max")
            if any(high[index] <= low[index] for index in range(3)):
                raise ValueError("camera scope max must exceed min on every axis")
            result["scope"] = {"min": low, "max": high}
        return result
    if kind == "section":
        allowed = {"kind", "id", "partIds", "plane", "label"}
        if set(request) - allowed:
            raise ValueError("section request has unknown fields: " + ", ".join(sorted(set(request) - allowed)))
        plane = request.get("plane")
        if not isinstance(plane, dict) or set(plane) != {"origin", "normal", "u"}:
            raise ValueError("section plane needs exactly origin, normal and u")
        origin = _coordinate(plane["origin"], "plane.origin")
        normal = _unit_vec(plane["normal"], "plane.normal")
        u = _unit_vec(plane["u"], "plane.u")
        # Plane validates that the supplied axes define an orthogonal frame.
        Plane(origin=origin, x_dir=u, z_dir=normal)
        result = {"kind": kind, "id": request.get("id", f"section-{index + 1}"),
                  "partIds": _selected_names(request), "plane": {"origin": origin, "normal": normal, "u": u}}
        if not isinstance(result["id"], str) or not result["id"].strip() or len(result["id"].encode()) > 80:
            raise ValueError("section id must be a nonempty string of at most 80 bytes")
        if "label" in request:
            if not isinstance(request["label"], str) or len(request["label"]) > 120:
                raise ValueError("section label must be a string of at most 120 characters")
            result["label"] = request["label"]
        return result
    raise ValueError("preview kind must be camera or section")


def _source_artifact(parent: dict, parent_ref: dict, key: str | None, root: Path) -> tuple[str, dict, dict | None]:
    artifacts = parent.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    # Draft and compile reports persist their derived output bindings in the
    # exact run record. Never borrow outputs from a different latest run.
    from agent_api import state
    records = [state(root).get("latest", {}), *state(root).get("runs", [])]
    run = next((entry for entry in records if isinstance(entry, dict) and entry.get("report") == parent_ref), None)
    run_outputs = run.get("outputs", {}) if run and isinstance(run.get("outputs"), dict) else {}
    if run_outputs:
        artifacts = {**run_outputs, **artifacts}
    if isinstance(parent.get("cadStage"), dict):
        artifacts.setdefault("cadStage", parent["cadStage"])
    if not artifacts:
        raise ValueError("parent report has no bound artifacts")
    if key is None:
        if isinstance(artifacts.get("step:display"), dict):
            key = "step:display"
        elif isinstance(artifacts.get("step"), dict):
            key = "step"
        elif isinstance(artifacts.get("step:assembly"), dict):
            key = "step:assembly"
        else:
            candidates = [name for name, ref in artifacts.items()
                          if name.startswith("step:") and isinstance(ref, dict)]
            if len(candidates) != 1:
                raise ValueError("sourceArtifact is ambiguous; choose a STEP artifact from the parent report")
            key = candidates[0]
    if key not in artifacts:
        raise ValueError(f"parent report has no artifact named {key!r}")
    ref = artifacts[key]
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
        raise ValueError("sourceArtifact must be a report-bound file artifact")
    # A state output from this exact report may retain a nested scene binding
    # omitted by older report bodies, but only for the exact same STEP bytes.
    run_ref = run_outputs.get(key)
    if (not isinstance(ref.get("scene"), dict) and isinstance(run_ref, dict)
            and run_ref.get("path") == ref.get("path") and run_ref.get("sha256") == ref.get("sha256")
            and isinstance(run_ref.get("scene"), dict)):
        ref = {**ref, "scene": run_ref["scene"]}
    path = scoped(root, ref["path"])
    if path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError("sourceArtifact must refer to STEP/STP geometry")
    if path.stat().st_size > MAX_SOURCE_STEP_BYTES:
        raise ValueError(f"source STEP exceeds the {MAX_SOURCE_STEP_BYTES}-byte preview input limit")
    from agent_api import _paths_current
    if not _paths_current(root, {key: {"path": ref["path"], "sha256": ref["sha256"]}}):
        raise ValueError("source STEP changed; recover the current report")
    stage_ref = artifacts.get("cadStage")
    return key, ref, stage_ref if isinstance(stage_ref, dict) else None


def _renderer_version() -> str:
    """Invalidate cached images when a renderer or measurement implementation changes."""
    names = ("agent_preview.py", "preview_resource_limits.py", "cpu_z_buffer.py", "display_step.py",
             "brep_measurements.py", "brep_tessellation.py")
    digests = [sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names]
    return RENDERER_VERSION + ":" + sha256(encode(digests).encode()).hexdigest()[:16]


def _scene_names(step_path: Path) -> list[str] | None:
    from display_step import SCHEMA, scene_path
    descriptor_path = scene_path(step_path)
    if not descriptor_path.is_file():
        return None
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    names = [item.get("name") for item in descriptor.get("objects", []) if isinstance(item, dict)]
    if (descriptor.get("schema") != SCHEMA or descriptor.get("step") != binding(step_path)
            or not names or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(names) != len(set(names))):
        raise ValueError("source STEP scene descriptor is stale or has invalid object IDs")
    return names


def _selected_step_input_budget(root: Path, source_path: Path, selected_names: set[str] | None,
                                *, allow_legacy_stage: bool = True) -> dict:
    """Preflight the unique parent and selected object STEP files before OCC loads them."""
    paths = {source_path.resolve()}
    from display_step import step_object_dependencies
    dependencies = step_object_dependencies(source_path, selected_names, allow_legacy_stage=allow_legacy_stage)
    refs = []
    seen_refs = set()
    for ref in dependencies["objects"]:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValueError("selected STEP object has no bound source file")
        identity = (ref["path"], ref.get("sha256"))
        if identity not in seen_refs:
            seen_refs.add(identity)
            refs.append(ref)
    for ref in refs:
        paths.add(scoped(root, ref["path"]).resolve())
    total = 0
    for path in paths:
        if not path.is_file():
            raise ValueError("selected STEP input is missing")
        total += path.stat().st_size
    if total > MAX_TOTAL_STEP_INPUT_BYTES:
        raise ValueError(
            f"selected STEP inputs total {total} bytes; limit is {MAX_TOTAL_STEP_INPUT_BYTES} bytes"
        )
    bindings = ([dependencies["stage"]] if dependencies["stage"] else []) + refs
    return {"files": len(paths), "bytes": total, "maxBytes": MAX_TOTAL_STEP_INPUT_BYTES,
            "sourceDependencies": bindings}


def _bound_legacy_stage_dependencies(root: Path, source_path: Path, parent_stage_ref: dict | None) -> dict | None:
    """Read a legacy sidecar only after its exact parent-report binding is current."""
    if parent_stage_ref is None:
        return None
    from agent_api import _paths_current
    if not _paths_current(root, {"cadStage": parent_stage_ref}):
        raise ValueError("legacy CAD stage is missing or differs from the parent report binding")
    from display_step import step_object_dependencies
    dependencies = step_object_dependencies(source_path, allow_legacy_stage=True)
    if dependencies["stage"] != parent_stage_ref:
        raise ValueError("legacy CAD stage is missing or differs from the parent report binding")
    return dependencies


def _source_dependencies_current(root: Path, refs) -> bool:
    """Check exact manifest-bound scene/stage and object STEP dependencies."""
    if not isinstance(refs, list):
        return False
    if not refs:
        return True
    from agent_api import _paths_current
    return _paths_current(root, {str(index): ref for index, ref in enumerate(refs)})


def _image_result(path: Path, final_path: Path | None = None) -> dict:
    ref = {"path": str((final_path or path).resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}
    return ref


def _scope_label(kind: str, selected: list[str], hidden: list[str], width: int, measure) -> str:
    names = ", ".join(name.encode("ascii", "replace").decode("ascii") for name in selected[:2])
    if len(selected) > 2:
        names += f" +{len(selected) - 2}"
    prefix, suffix = f"LOCAL {kind}", f"hidden {len(hidden)}"
    name_budget = max(0, width - 12 - measure(f"{prefix} |  | {suffix}"))
    original_names = names
    while names and measure(names) > name_budget:
        names = names[:-1]
    if names != original_names and names:
        while names and measure(names + "...") > name_budget:
            names = names[:-1]
        names += "..."
    return f"{prefix} | {names} | {suffix}" if names else f"{prefix} | {suffix}"


def _annotate_scope(path: Path, kind: str, selected: list[str], hidden: list[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default()
    with Image.open(path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    measure = lambda value: draw.textbbox((0, 0), value, font=font)[2]
    label = _scope_label(kind, selected, hidden, image.width, measure)
    banner_height = 42 if kind == "section" else 20
    draw.rectangle((0, 0, image.width, banner_height), fill=(24, 32, 42))
    draw.text((6, 4), label, fill=(255, 255, 255), font=font)
    if kind == "section":
        # Preserve the measurement renderer's coordinate and unit title below
        # the scope label; geometry starts below this reserved margin.
        draw.text((6, 22), "Specified section - input-part coordinates - mm", fill=(255, 255, 255), font=font)
    image.save(path, format="PNG")


def _result(manifest_path: Path, manifest: dict, *, cached: bool, currentness: dict | None = None) -> dict:
    observations = []
    for index, item in enumerate(manifest["observations"]):
        view = {key: item[key] for key in ("id", "kind", "scope", "cameraScope", "image") if key in item}
        for key in ("selectedObjects", "hiddenObjects"):
            names = item.get(key, [])
            if len(encode(names).encode()) <= MAX_INLINE_NAME_BYTES:
                view[key] = names
            else:
                view[key + "Count"] = len(names)
                view[key + "Field"] = f"/observations/{index}/{key}"
        if "measurement" in item:
            measurement = item["measurement"]
            view["measurementSummary"] = {key: measurement.get(key) for key in
                ("status", "outer_envelope", "material_island_count", "hole_count", "sum_material_area_mm2")}
            view["measurementField"] = f"/observations/{index}/measurement"
        observations.append(view)
    limits = dict(manifest["limits"])
    from preview_resource_limits import process_memory_boundary
    active_boundary = process_memory_boundary()
    if active_boundary is not None:
        # A cache hit is protected by this process's limit; retain generation
        # provenance separately in the manifest rather than implying it applies.
        limits["processMemoryBoundary"] = active_boundary
    result = {"schema": MANIFEST_SCHEMA, "status": "completed", "cached": cached,
            "report": binding(manifest_path), "parentReport": manifest["parentReport"],
            "sourceArtifact": manifest["sourceArtifact"], "sourceStatus": manifest["sourceStatus"],
            "currentness": currentness or manifest["currentness"], "observations": observations,
            "limits": limits,
            "scope": "Derived visual/measurement evidence only; CAD acceptance and latest CAD run are unchanged."}
    if wire_size(result) > MAX_WIRE_BYTES - 512:
        result["sourceArtifactField"] = "/sourceArtifact"
        result.pop("sourceArtifact", None)
    if wire_size(result) > MAX_WIRE_BYTES:
        raise ValueError("preview response exceeds the output budget while preserving direct image paths; shorten report/workspace paths")
    return result


def _currentness(root: Path, parent: dict, parent_ref: dict) -> dict:
    from agent_api import _active_matches, _paths_current, state
    current_state = state(root)
    records = [current_state.get("latest", {}), *current_state.get("runs", [])]
    run = next((entry for entry in records if isinstance(entry, dict) and entry.get("report") == parent_ref), None)
    inputs = (run.get("inputs", {}) if isinstance(run, dict) else {}) or parent.get(
        "taskInputBindings", parent.get("inputBindings", {}))
    if not inputs and isinstance(parent.get("source"), dict):
        inputs = {"source": parent["source"]}
    inputs_current = _paths_current(root, inputs) if inputs else False
    active = inputs_current and _active_matches(root, current_state, inputs)
    if inputs and (parent.get("pass") or parent.get("status") == "awaiting-preview"):
        from agent_api import _intent_lineage_current
        inputs_current = inputs_current and _intent_lineage_current(root, inputs)
        active = inputs_current and _active_matches(root, current_state, inputs)
    latest = current_state.get("latest", {})
    same_run = latest.get("report") == parent_ref
    source_current = _paths_current(root, {"sourceArtifact": parent.get("sourceArtifact", {}).get("binding", {})}) if parent.get("sourceArtifact") else True
    current = inputs_current and active and same_run and source_current
    return {"inputsCurrent": inputs_current, "activeInputsMatch": active,
            "latestRunMatches": same_run, "sourceCurrent": source_current, "historical": not current}


def _register_manifest(root: Path, manifest_path: Path, parent_ref: dict) -> None:
    from agent_api import save, state
    current_state = state(root)
    ref = binding(manifest_path)
    registrations = [entry for entry in current_state.get("visualPreviews", [])
                     if isinstance(entry, dict) and entry.get("manifest", {}).get("path") != str(manifest_path)]
    registrations.append({"manifest": ref, "parentReport": parent_ref})
    registrations = registrations[-MAX_REGISTERED_MANIFESTS:]

    # Keep only feature-owned manifests and cache directories. The registry is
    # also the allowlist consumed by the workbench, so disk retention follows
    # the same bounded set and never scans or deletes arbitrary hidden files.
    previews_dir = root / ".a3d-agent" / "previews"
    cache_dir = previews_dir / "cache"

    def owned_key(entry: dict) -> str | None:
        manifest = entry.get("manifest")
        path = manifest.get("path") if isinstance(manifest, dict) else None
        if not isinstance(path, str):
            return None
        try:
            relative = Path(path).resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
        leaf = Path(relative).name
        if (not relative.startswith(".a3d-agent/previews/") or len(leaf) != 69
                or not leaf.endswith(".json") or any(char not in "0123456789abcdef" for char in leaf[:-5])):
            return None
        return leaf[:-5]

    def is_owned_manifest(key: str) -> bool:
        manifest_path = previews_dir / (key + ".json")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            if document.get("schema") != MANIFEST_SCHEMA or document.get("cacheKey") != key:
                return False
            digest = document.get("manifestSha256")
            unsigned = {name: value for name, value in document.items() if name != "manifestSha256"}
            if digest != sha256(encode(unsigned).encode()).hexdigest():
                return False
            for observation in document.get("observations", []):
                image = observation.get("image", {})
                image_path = Path(image.get("path", "")).resolve()
                if image_path.parent != (cache_dir / key).resolve() or not image_path.name.startswith("observation-"):
                    return False
            return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def cache_bytes(key: str) -> int:
        total = 0
        manifest = previews_dir / (key + ".json")
        try:
            total += manifest.stat().st_size
            document = json.loads(manifest.read_text(encoding="utf-8"))
            for observation in document.get("observations", []):
                image = observation.get("image", {})
                image_path = Path(image.get("path", ""))
                if image_path.parent == cache_dir / key and image_path.is_file():
                    total += image_path.stat().st_size
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return total

    keys = [owned_key(entry) for entry in registrations]
    keys = [key for key in keys if key is not None]
    while len(registrations) > 1 and sum(cache_bytes(key) for key in keys) > MAX_CACHE_BYTES:
        oldest = registrations.pop(0)
        key = owned_key(oldest)
        if key is not None:
            keys.remove(key)
            if is_owned_manifest(key):
                (previews_dir / (key + ".json")).unlink(missing_ok=True)
                shutil.rmtree(cache_dir / key, ignore_errors=True)

    current_state["visualPreviews"] = registrations
    save(root, ".a3d-agent/state.json", current_state)
    keep = {key for entry in registrations if (key := owned_key(entry)) is not None}
    for child in previews_dir.glob("*.json"):
        if (len(child.stem) == 64 and all(char in "0123456789abcdef" for char in child.stem)
                and child.stem not in keep and is_owned_manifest(child.stem)):
            child.unlink(missing_ok=True)
            shutil.rmtree(cache_dir / child.stem, ignore_errors=True)
    if cache_dir.is_dir():
        for child in cache_dir.iterdir():
            if (child.is_dir() and len(child.name) == 64
                    and all(char in "0123456789abcdef" for char in child.name)
                    and child.name not in keep and is_owned_manifest(child.name)):
                shutil.rmtree(child, ignore_errors=True)
            elif (child.is_dir() and len(child.name) == 102 and child.name.startswith(".")
                  and child.name[1:65].lower() == child.name[1:65]
                  and all(char in "0123456789abcdef" for char in child.name[1:65])
                  and child.name[65] == "." and all(char in "0123456789abcdef" for char in child.name[66:98])
                  and child.name.endswith(".tmp")):
                shutil.rmtree(child, ignore_errors=True)


def preview(root: Path, args: dict) -> dict:
    from agent_api import _paths_current, save
    from agent_protocol import read_report
    from brep_measurements import measure_section
    from cpu_z_buffer import BufferPool
    from display_step import load_step_objects, mesh_step_objects

    parent_ref, parent = read_report(root, args["report"], args.get("sha256"))
    if parent.get("schema") not in {"a3d-draft-result/v2", "evidence-cad-compile-result/v2"}:
        raise ValueError("preview needs a bound draft or compile report")
    requests = args["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        raise ValueError(f"requests must contain 1..{MAX_REQUESTS} observations")
    normalized = [_normalize_request(request, index) for index, request in enumerate(requests)]
    camera_pixels = sum(request["size"] ** 2 for request in normalized if request["kind"] == "camera")
    if camera_pixels > MAX_TOTAL_PIXELS:
        raise ValueError(f"batch exceeds image budget ({MAX_OUTPUT_IMAGES} images, {MAX_TOTAL_PIXELS} total pixels)")
    source_key, source_ref, parent_stage_ref = _source_artifact(parent, parent_ref, args.get("sourceArtifact"), root)
    source_path = scoped(root, source_ref["path"])
    from display_step import scene_path
    bound_scene = scene_path(source_path)
    scene_ref = source_ref.get("scene")
    if isinstance(scene_ref, dict):
        current_scene_ref = binding(bound_scene) if bound_scene.is_file() else None
        if scene_ref != current_scene_ref:
            raise ValueError("source STEP scene differs from the report-bound scene")
        if not _paths_current(root, {"sourceArtifact": source_ref}):
            raise ValueError("source STEP scene or object binding is stale; recover the current CAD report")
        object_names = _scene_names(source_path)
    elif bound_scene.is_file():
        raise ValueError("source STEP has an unbound scene descriptor; choose a report-bound display STEP")
    else:
        object_names = None
    allow_legacy_stage = True
    if not bound_scene.is_file():
        legacy_dependencies = _bound_legacy_stage_dependencies(root, source_path, parent_stage_ref)
        if legacy_dependencies is None:
            # Do not infer object identities from an adjacent, unbound legacy
            # sidecar. A plain report-bound STEP can still be observed whole.
            allow_legacy_stage = False
    if source_path.stat().st_size > MAX_SOURCE_STEP_BYTES:
        raise ValueError(f"source STEP exceeds the {MAX_SOURCE_STEP_BYTES}-byte preview input limit")
    if object_names is None and any(request["partIds"] is not None for request in normalized):
        raise ValueError("partIds require a bound STEP scene descriptor; use whole-artifact observation")
    all_names = object_names
    unions = ({name for request in normalized for name in (request["partIds"] or all_names or ())}
              if all_names is not None else set())
    if all_names is not None and unions - set(all_names):
        raise ValueError("partIds must match names in the source STEP scene descriptor")
    if all_names is not None and len(unions) > MAX_SELECTED_OBJECTS:
        raise ValueError(f"a batch may load at most {MAX_SELECTED_OBJECTS} STEP objects")
    if all_names is None and len(unions) > MAX_SELECTED_OBJECTS:
        raise ValueError(f"a batch may load at most {MAX_SELECTED_OBJECTS} STEP objects")
    if all_names is not None and not unions:
        raise ValueError("source STEP has no selectable objects")
    if all_names is not None:
        image_count = sum(len(request["partIds"] or all_names) if request["kind"] == "section" else 1
                          for request in normalized)
        total_pixels = sum((request["size"] ** 2 if request["kind"] == "camera" else 800 * 640) *
                           (len(request["partIds"] or all_names) if request["kind"] == "section" else 1)
                           for request in normalized)
        if image_count > MAX_OUTPUT_IMAGES or total_pixels > MAX_TOTAL_PIXELS:
            raise ValueError(f"batch exceeds image budget ({MAX_OUTPUT_IMAGES} images, {MAX_TOTAL_PIXELS} total pixels)")
    input_budget = _selected_step_input_budget(root, source_path, unions if all_names is not None else None,
                                               allow_legacy_stage=allow_legacy_stage)
    source_dependencies = input_budget["sourceDependencies"]
    if (not _paths_current(root, {"sourceArtifact": source_ref})
            or not _source_dependencies_current(root, source_dependencies)):
        raise ValueError("source STEP scene or object binding is stale; recover the current CAD report")

    currentness = _currentness(root, parent, parent_ref)
    if not currentness["inputsCurrent"] or not currentness["activeInputsMatch"]:
        raise ValueError("parent report inputs are expired or no longer active; recover a current CAD report")
    currentness["sourceCurrent"] = (_paths_current(root, {"sourceArtifact": source_ref})
                                     and _source_dependencies_current(root, source_dependencies))
    if not currentness["sourceCurrent"]:
        raise ValueError("source STEP scene or object binding is stale; recover the current CAD report")
    renderer_version = _renderer_version()
    cache_key = sha256(encode([MANIFEST_SCHEMA, renderer_version, parent_ref, source_key, source_ref,
                               source_dependencies, normalized]).encode()).hexdigest()
    manifest_path = scoped(root, f".a3d-agent/previews/{cache_key}.json", exists=False)
    cache_root = scoped(root, f".a3d-agent/previews/cache/{cache_key}", exists=False)
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        image_refs = {str(index): item["image"] for index, item in enumerate(existing.get("observations", []))
                      if isinstance(item.get("image"), dict)}
        valid = (existing.get("schema") == MANIFEST_SCHEMA and existing.get("cacheKey") == cache_key
                 and existing.get("parentReport") == parent_ref and existing.get("sourceArtifact", {}).get("binding") == source_ref
                 and existing.get("sourceDependencyBindings") == source_dependencies
                 and existing.get("requests") == normalized and len(image_refs) == len(existing.get("observations", []))
                 and existing.get("manifestSha256") == sha256(encode({key: value for key, value in existing.items() if key != "manifestSha256"}).encode()).hexdigest()
                 and _paths_current(root, image_refs)
                 and _source_dependencies_current(root, source_dependencies))
        if valid:
            _register_manifest(root, manifest_path, parent_ref)
            latest_currentness = _currentness(root, parent, parent_ref)
            latest_currentness["sourceCurrent"] = (_paths_current(root, {"sourceArtifact": source_ref})
                                                    and _source_dependencies_current(root, source_dependencies))
            if not latest_currentness["inputsCurrent"] or not latest_currentness["activeInputsMatch"]:
                raise ValueError("parent report inputs are expired or no longer active; recover a current CAD report")
            return _result(manifest_path, existing, cached=True, currentness=latest_currentness)
    except (OSError, ValueError, TypeError, KeyError):
        pass

    loaded, provenance = load_step_objects(source_path, object_names=sorted(unions) if all_names is not None else None,
                                           allow_legacy_stage=allow_legacy_stage)
    selected_map = {record["name"]: shape for record, shape in loaded}
    if all_names is None:
        all_names = sorted(selected_map)
        unions = set(all_names)
    if set(selected_map) != unions:
        raise ValueError("STEP loader did not return the complete selected object set")
    image_count = sum(len(request["partIds"] or all_names) if request["kind"] == "section" else 1
                      for request in normalized)
    total_pixels = sum((request["size"] ** 2 if request["kind"] == "camera" else 800 * 640) *
                       (len(request["partIds"] or all_names) if request["kind"] == "section" else 1)
                       for request in normalized)
    if len(unions) > MAX_SELECTED_OBJECTS:
        raise ValueError(f"a batch may load at most {MAX_SELECTED_OBJECTS} STEP objects")
    if image_count > MAX_OUTPUT_IMAGES or total_pixels > MAX_TOTAL_PIXELS:
        raise ValueError(f"batch exceeds image budget ({MAX_OUTPUT_IMAGES} images, {MAX_TOTAL_PIXELS} total pixels)")
    camera_names = {name for request in normalized if request["kind"] == "camera"
                    for name in (request["partIds"] or all_names)}
    camera_objects = [(record, shape) for record, shape in loaded if record["name"] in camera_names]
    mesh_records, _ = mesh_step_objects(camera_objects, provenance) if camera_objects else ([], provenance)
    meshes: dict[str, MeshInput] = {}
    for record, mesh, style in mesh_records:
        color = style.get("baseColor", "#7AA3C7")
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        meshes[record] = MeshInput(record, mesh, rgb, source_path)
    limits = RenderLimits()
    triangles = triangle_count(list(meshes.values()))
    if triangles > DEFAULT_MAX_TRIANGLES:
        raise ValueError(f"selected objects have {triangles} triangles; limit is {DEFAULT_MAX_TRIANGLES}")

    cache_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_root.parent / f".{cache_key}.{uuid4().hex}.tmp"
    temporary.mkdir(parents=False, exist_ok=False)
    observations = []
    output_index = 0
    pool = BufferPool()
    try:
        for request in normalized:
            selected = request["partIds"] or sorted(unions)
            hidden = sorted(set(all_names or selected) - set(selected))
            if request["kind"] == "camera":
                selected_meshes = [meshes[name] for name in selected]
                view = request.get("view", "custom")
                camera = None if "view" in request else (request["direction"], request["up"])
                rendered = render_view(selected_meshes, view, request["size"], limits=limits, pool=pool,
                                       camera=camera, target=request.get("target"),
                                       crop_bounds=((request["scope"]["min"], request["scope"]["max"])
                                                    if request.get("scope") else None))
                image_path = temporary / f"observation-{output_index:02d}.png"
                rendered.image.save(image_path, format="PNG")
                _annotate_scope(image_path, "camera", selected, hidden)
                observations.append({"id": request["id"], "kind": "camera", "request": request,
                    "selectedObjects": selected, "hiddenObjects": hidden,
                    "scope": ("Selected STEP objects; other scene objects are hidden. Scope is a camera-space framing/depth clip from projected box corners, not strict world-space clipping at arbitrary angles."
                              if request.get("scope") else "Selected STEP objects; other scene objects are hidden."),
                    **({"cameraScope": request["scope"]} if request.get("scope") else {}),
                    "image": _image_result(image_path, cache_root / image_path.name),
                    "render": {"renderer": renderer_version, "triangles": rendered.stats.triangles_input,
                               "coveredPixels": rendered.stats.covered_pixels, "elapsedSeconds": rendered.stats.elapsed_seconds}})
                output_index += 1
            else:
                plane_data = request["plane"]
                plane = Plane(origin=plane_data["origin"], x_dir=plane_data["u"], z_dir=plane_data["normal"])
                for name in selected:
                    image_path = temporary / f"observation-{output_index:02d}.png"
                    measured = measure_section(selected_map[name], plane, label=request.get("label", request["id"]), preview=image_path)
                    _annotate_scope(image_path, "section", [name], sorted(set(all_names or selected) - {name}))
                    image_ref = _image_result(image_path, cache_root / image_path.name)
                    measured["preview"] = image_ref
                    observations.append({"id": f"{request['id']}:{name}", "kind": "section", "request": request,
                        "selectedObjects": [name], "hiddenObjects": sorted(set(all_names or selected) - {name}),
                        "scope": "Exact BRep section for the named object; image is a sampled outline and does not prove global wall thickness.",
                        "measurement": measured, "image": image_ref})
                    output_index += 1
        from agent_api import _paths_current
        if (not _paths_current(root, {"sourceArtifact": source_ref})
                or not _source_dependencies_current(root, source_dependencies)):
            raise ValueError("source STEP changed while preview observations were being generated")
        final_currentness = _currentness(root, parent, parent_ref)
        if not final_currentness["inputsCurrent"] or not final_currentness["activeInputsMatch"]:
            raise ValueError("parent report inputs expired while preview observations were being generated")
        if cache_root.exists():
            shutil.rmtree(cache_root)
        os.replace(temporary, cache_root)
        manifest = {"schema": MANIFEST_SCHEMA, "cacheKey": cache_key, "createdAt": time.time(),
            "parentReport": parent_ref, "parentStatus": parent.get("status"), "parentPass": parent.get("pass", False),
            "sourceArtifact": {"key": source_key, "binding": source_ref, "scene": provenance.get("scene")},
            "sourceDependencyBindings": source_dependencies,
            "sourceStatus": "diagnostic" if parent.get("status") in {"diagnostic", "failed"} or "diagnosticPreview" in parent.get("artifacts", {}) else parent.get("status", "unknown"),
            "currentness": currentness, "requests": normalized, "observations": observations,
            "limits": {"renderer": renderer_version, "maxRequests": MAX_REQUESTS, "maxImages": MAX_OUTPUT_IMAGES,
                       "maxTotalPixels": MAX_TOTAL_PIXELS, "maxSelectedObjects": MAX_SELECTED_OBJECTS,
                       "maxTriangles": DEFAULT_MAX_TRIANGLES, "trianglesLoaded": triangles,
                       "stepInputFiles": input_budget["files"], "stepInputBytes": input_budget["bytes"],
                       "maxStepInputBytes": input_budget["maxBytes"]},
            "limitations": ["Observation does not modify geometry or update latest CAD run.",
                            "Visual and section evidence does not change CAD acceptance or prove unobserved regions."]}
        from preview_resource_limits import process_memory_boundary
        memory_boundary = process_memory_boundary()
        if memory_boundary is not None:
            manifest["limits"]["generationProcessMemoryBoundary"] = memory_boundary
        manifest["manifestSha256"] = sha256(encode(manifest).encode()).hexdigest()
        saved_manifest = save(root, f".a3d-agent/previews/{cache_key}.json", manifest)
        if (not _paths_current(root, {"parentReport": parent_ref, "sourceArtifact": source_ref})
                or not _source_dependencies_current(root, source_dependencies)):
            raise ValueError("parent report or source artifact changed while observations were being saved")
        _register_manifest(root, saved_manifest, parent_ref)
        return _result(saved_manifest, manifest, cached=False)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
