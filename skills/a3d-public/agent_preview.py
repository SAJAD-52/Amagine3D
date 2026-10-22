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
MAX_DIRECT_PATH_BYTES = 300
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
            result["direction"] = _vec(request["direction"], "direction")
            result["up"] = _vec(request["up"], "up")
        if "target" in request:
            result["target"] = _vec(request["target"], "target")
        if "scope" in request:
            scope = request["scope"]
            if not isinstance(scope, dict) or set(scope) != {"min", "max"}:
                raise ValueError("camera scope needs exactly min and max 3D corners")
            low, high = _vec(scope["min"], "scope.min"), _vec(scope["max"], "scope.max")
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
        origin = _vec(plane["origin"], "plane.origin")
        normal = _vec(plane["normal"], "plane.normal")
        u = _vec(plane["u"], "plane.u")
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


def _source_artifact(parent: dict, parent_ref: dict, key: str | None, root: Path) -> tuple[str, dict]:
    artifacts = parent.get("artifacts")
    artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
    # Draft and compile reports persist their derived output bindings in the
    # exact run record. Never borrow outputs from a different latest run.
    from agent_api import state
    records = [state(root).get("latest", {}), *state(root).get("runs", [])]
    run = next((entry for entry in records if isinstance(entry, dict) and entry.get("report") == parent_ref), None)
    if run and isinstance(run.get("outputs"), dict):
        artifacts = {**run["outputs"], **artifacts}
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
    path = scoped(root, ref["path"])
    if path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError("sourceArtifact must refer to STEP/STP geometry")
    from agent_api import _paths_current
    if not _paths_current(root, {key: ref}):
        raise ValueError("source STEP or its bound scene changed; recover the current report")
    return key, ref


def _renderer_version() -> str:
    """Invalidate cached images when a renderer or measurement implementation changes."""
    names = ("cpu_z_buffer.py", "display_step.py", "brep_measurements.py", "brep_tessellation.py")
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


def _image_result(path: Path, final_path: Path | None = None) -> dict:
    ref = {"path": str((final_path or path).resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}
    if len(ref["path"].encode()) > MAX_DIRECT_PATH_BYTES:
        raise ValueError("preview output path exceeds the direct image handoff limit")
    return ref


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
    result = {"schema": MANIFEST_SCHEMA, "status": "completed", "cached": cached,
            "report": binding(manifest_path), "parentReport": manifest["parentReport"],
            "sourceArtifact": manifest["sourceArtifact"], "sourceStatus": manifest["sourceStatus"],
            "currentness": currentness or manifest["currentness"], "observations": observations,
            "limits": manifest["limits"],
            "scope": "Derived visual/measurement evidence only; CAD acceptance and latest CAD run are unchanged."}
    if wire_size(result) > MAX_WIRE_BYTES - 512:
        result["sourceArtifactField"] = "/sourceArtifact"
        result.pop("sourceArtifact", None)
    if wire_size(result) > MAX_WIRE_BYTES:
        raise ValueError("preview response exceeds the output budget while preserving direct image paths; shorten report/workspace paths")
    return result


def _currentness(root: Path, parent: dict, parent_ref: dict) -> dict:
    from agent_api import _active_matches, _paths_current, state
    inputs = parent.get("taskInputBindings", parent.get("inputBindings", {}))
    if not inputs and isinstance(parent.get("source"), dict):
        inputs = {"source": parent["source"]}
    inputs_current = _paths_current(root, inputs) if inputs else False
    active = inputs_current and _active_matches(root, state(root), inputs)
    latest = state(root).get("latest", {})
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
    source_key, source_ref = _source_artifact(parent, parent_ref, args.get("sourceArtifact"), root)
    source_path = scoped(root, source_ref["path"])
    object_names = _scene_names(source_path)
    from display_step import scene_path
    bound_scene = scene_path(source_path)
    if bound_scene.is_file():
        scene_ref = binding(bound_scene)
        if source_ref.get("scene") and source_ref["scene"] != scene_ref:
            raise ValueError("source STEP scene differs from the report-bound scene")
        source_ref = {**source_ref, "scene": scene_ref}
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

    currentness = _currentness(root, parent, parent_ref)
    if not currentness["inputsCurrent"] or not currentness["activeInputsMatch"]:
        raise ValueError("parent report inputs are expired or no longer active; recover a current CAD report")
    currentness["sourceCurrent"] = _paths_current(root, {"sourceArtifact": source_ref})
    renderer_version = _renderer_version()
    cache_key = sha256(encode([MANIFEST_SCHEMA, renderer_version, parent_ref, source_key, source_ref, normalized]).encode()).hexdigest()
    manifest_path = scoped(root, f".a3d-agent/previews/{cache_key}.json", exists=False)
    cache_root = scoped(root, f".a3d-agent/previews/cache/{cache_key}", exists=False)
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        image_refs = {str(index): item["image"] for index, item in enumerate(existing.get("observations", []))
                      if isinstance(item.get("image"), dict)}
        valid = (existing.get("schema") == MANIFEST_SCHEMA and existing.get("cacheKey") == cache_key
                 and existing.get("parentReport") == parent_ref and existing.get("sourceArtifact", {}).get("binding") == source_ref
                 and existing.get("requests") == normalized and len(image_refs) == len(existing.get("observations", []))
                 and existing.get("manifestSha256") == sha256(encode({key: value for key, value in existing.items() if key != "manifestSha256"}).encode()).hexdigest()
                 and _paths_current(root, image_refs))
        if valid:
            _register_manifest(root, manifest_path, parent_ref)
            latest_currentness = _currentness(root, parent, parent_ref)
            latest_currentness["sourceCurrent"] = _paths_current(root, {"sourceArtifact": source_ref})
            if not latest_currentness["inputsCurrent"] or not latest_currentness["activeInputsMatch"]:
                raise ValueError("parent report inputs are expired or no longer active; recover a current CAD report")
            return _result(manifest_path, existing, cached=True, currentness=latest_currentness)
    except (OSError, ValueError, TypeError, KeyError):
        pass

    loaded, provenance = load_step_objects(source_path, object_names=sorted(unions) if all_names is not None else None)
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
                observations.append({"id": request["id"], "kind": "camera", "request": request,
                    "selectedObjects": selected, "hiddenObjects": hidden,
                    "scope": "Selected STEP objects; other scene objects are hidden.",
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
                    image_ref = _image_result(image_path, cache_root / image_path.name)
                    observations.append({"id": f"{request['id']}:{name}", "kind": "section", "request": request,
                        "selectedObjects": [name], "hiddenObjects": sorted(set(all_names or selected) - {name}),
                        "scope": "Exact BRep section for the named object; image is a sampled outline and does not prove global wall thickness.",
                        "measurement": measured, "image": image_ref})
                    output_index += 1
        from agent_api import _paths_current
        if not _paths_current(root, {"sourceArtifact": source_ref}):
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
            "sourceStatus": "diagnostic" if parent.get("status") in {"diagnostic", "failed"} or "diagnosticPreview" in parent.get("artifacts", {}) else parent.get("status", "unknown"),
            "currentness": currentness, "requests": normalized, "observations": observations,
            "limits": {"renderer": renderer_version, "maxRequests": MAX_REQUESTS, "maxImages": MAX_OUTPUT_IMAGES,
                       "maxTotalPixels": MAX_TOTAL_PIXELS, "maxSelectedObjects": MAX_SELECTED_OBJECTS,
                       "maxTriangles": DEFAULT_MAX_TRIANGLES, "trianglesLoaded": triangles},
            "limitations": ["Observation does not modify geometry or update latest CAD run.",
                            "Visual and section evidence does not change CAD acceptance or prove unobserved regions."]}
        manifest["manifestSha256"] = sha256(encode(manifest).encode()).hexdigest()
        saved_manifest = save(root, f".a3d-agent/previews/{cache_key}.json", manifest)
        if not _paths_current(root, {"parentReport": parent_ref, "sourceArtifact": source_ref}):
            raise ValueError("parent report or source artifact changed while observations were being saved")
        _register_manifest(root, saved_manifest, parent_ref)
        return _result(saved_manifest, manifest, cached=False)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
