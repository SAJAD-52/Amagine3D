"""STEP-backed display scenes. Geometry stays CAD; meshes are derived in memory."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import re
import time

import numpy as np
import trimesh
from build123d import Color, Compound, Location, Unit, export_brep, export_step, import_step
from OCP.TopLoc import TopLoc_Location

from display_normals import mesh_with_display_normals

SCHEMA = "a3d-step-display/v1"
HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


class DisplayStepError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.issue = {"code": "DISPLAY.STEP_FAILED", "message": message, **details}


def invalid_normal_issue(geometry, node_name, transform=None):
    normals = geometry._cache.cache.get("vertex_normals")
    if normals is None or normals.shape != geometry.vertices.shape:
        bad = np.ones(len(geometry.vertices), dtype=bool)
    else:
        bad = ~np.isfinite(normals).all(axis=1) | ~np.isclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-5)
    vertices = np.flatnonzero(bad)
    points = np.asarray(geometry.vertices)[vertices]
    faces = np.flatnonzero(np.isin(geometry.faces, vertices).any(axis=1))
    return {"code": "DISPLAY.NORMALS_INVALID", "partId": node_name, "offenderId": node_name,
            "stage": "step-tessellation", "invalidNormalCount": len(vertices),
            "vertexIndices": vertices[:32].tolist(), "faceIndices": faces[:32].tolist(),
            "boundsMm": {"min": points.min(axis=0).tolist(), "max": points.max(axis=0).tolist()} if len(points) else None,
            "coordinateSystem": {"handedness": "right", "up": "Z", "units": "mm"},
            "coordinateFrame": "semantic-assembly", "transform": np.asarray(np.eye(4) if transform is None else transform).tolist()}



def appearance(base_color, *, metallic=0.0, roughness=0.58):
    if not isinstance(base_color, str) or not HEX_COLOR.fullmatch(base_color):
        raise DisplayStepError("base color must be #RRGGBB")
    for name, value in (("metallic", metallic), ("roughness", roughness)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise DisplayStepError(f"{name} must be between 0 and 1")
    return {"baseColor": base_color.upper(), "metallic": float(metallic), "roughness": float(roughness)}


def binding(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def scene_path(path):
    return Path(str(Path(path)) + ".scene.json")


def _write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _checked_ref(ref, root):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        raise DisplayStepError("STEP scene has an invalid file binding")
    path = Path(ref["path"])
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise DisplayStepError("STEP scene file is outside its artifact directory")
    if path.suffix.lower() not in {".step", ".stp"} or binding(path)["sha256"] != ref.get("sha256"):
        raise DisplayStepError("STEP scene file changed or is not STEP")
    return path.resolve()


def load_display_component(node, base_dir):
    """Load an explicitly bound STEP reference, without manufacturing it."""
    parameters = node["recipe"]["parameters"]
    raw = parameters.get("sourceStep")
    if raw is None:
        raise DisplayStepError(f"display component {node['id']!r} requires sourceStep")
    source = {"path": raw, "scale": 1.0} if isinstance(raw, str) else dict(raw)
    if source.get("scale", 1.0) != 1.0:
        raise DisplayStepError("display component scale must equal 1")
    path = Path(source["path"])
    if not path.is_absolute():
        path = Path(base_dir) / path
    if path.suffix.lower() not in {".step", ".stp"}:
        raise DisplayStepError("display component requires genuine STEP geometry")
    observed = binding(path)
    if source.get("sha256") is not None and source["sha256"] != observed["sha256"]:
        raise DisplayStepError("display component source sha256 does not match")
    shape = import_step(path)
    shape.parent = None
    transform = source.get("toCanonicalTransform")
    if transform is not None:
        from build123d import Matrix
        matrix = np.asarray(transform, dtype=float)
        if (matrix.shape != (4, 4) or not np.isfinite(matrix).all() or
                not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-8) or
                not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-7) or
                not np.isclose(np.linalg.det(matrix[:3, :3]), 1.0, atol=1e-7)):
            raise DisplayStepError("display component transform must be a rigid right-handed 4x4 matrix")
        shape = shape.transform_shape(Matrix(matrix.tolist()))
    shape._a3d_display_reference = node.get("physicalFeatureRef")
    visual = parameters["appearance"]
    return node["id"], shape, appearance(visual["baseColor"], metallic=visual.get("metallic", 0), roughness=visual.get("roughness", 0.58)), {**source, **observed, "scale": 1.0}


def load_display_components(scene_data, path):
    return [load_display_component(node, Path(path).resolve().parent)[:3]
            for node in scene_data.get("nodes", []) if node.get("role") == "display-only"]


def export_display_step(physical_items, path, *, display_items=(), metadata=None, reuse_step=False):
    """Persist real BRep objects and their identities; never convert mesh to CAD."""
    from export_audit import _audit_step, geometry_record
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    physical, references = list(physical_items), list(display_items)
    names = [name for name, _, _ in physical + references]
    if not names or len(names) != len(set(names)):
        raise DisplayStepError("STEP display object names must be nonempty and unique")
    objects, children = [], []
    directory = path.parent / ".display-objects" / path.stem
    directory.mkdir(parents=True, exist_ok=True)
    for role, items in (("manufactured", physical), ("display-only", references)):
        for name, original, style in items:
            if isinstance(original, trimesh.Trimesh):
                raise DisplayStepError("display geometry must be BRep, not a mesh")
            shape = deepcopy(original)
            shape.parent = None
            shape.label = name
            normalized = appearance(style["baseColor"], metallic=style.get("metallic", 0), roughness=style.get("roughness", 0.58))
            rgb = [int(normalized["baseColor"][i:i+2], 16) / 255 for i in (1, 3, 5)]
            shape.color = Color(*rgb)
            expected = geometry_record(shape)
            if not expected["valid"] or (role == "manufactured" and expected["bodyCount"] < 1):
                raise DisplayStepError(f"display object {name!r} is not valid CAD")
            target = directory / (sha256(name.encode()).hexdigest()[:20] + ".step")
            if not export_step(shape, target, unit=Unit.MM):
                raise DisplayStepError(f"could not export display object {name}")
            audit = _audit_step(target, expected, name)
            if not audit["pass"]:
                raise DisplayStepError("; ".join(audit["errors"]))
            record = {"name": name, "role": role, "step": binding(target), "geometry": expected, "appearance": normalized,
                      "coordinateFrame": "semantic", "transformApplied": True}
            reference = getattr(original, "_a3d_display_reference", None)
            if reference is not None:
                record["physicalFeatureRef"] = reference
            for attribute, key in (("_a3d_display_owner_part", "ownerPart"), ("_a3d_display_region_id", "regionId")):
                value = getattr(original, attribute, None)
                if value is not None: record[key] = value
            objects.append(record)
            children.append(shape)
    assembly = Compound(children=children)
    expected = geometry_record(assembly)
    if not reuse_step and not export_step(assembly, path, unit=Unit.MM):
        raise DisplayStepError("STEP display writer failed")
    audit = _audit_step(path, expected, "display")
    if not audit["pass"]:
        raise DisplayStepError("; ".join(audit["errors"]))
    descriptor = {"schema": SCHEMA, "step": binding(path), "objects": objects,
                  "units": "mm", "coordinateSystem": {"up": "Z", "handedness": "right"}, "metadata": metadata or {}}
    descriptor_path = scene_path(path)
    _write(descriptor_path, descriptor)
    return {"nodeNames": sorted(names), "physicalNodeNames": sorted(name for name, _, _ in physical),
            "displayOnlyNodeNames": sorted(name for name, _, _ in references),
            "readbackBaseColors": {r["name"]: r["appearance"]["baseColor"] for r in objects},
            "scene": binding(descriptor_path), "sha256": binding(path)["sha256"], "verified": True}


def _fallback_objects(path):
    """Read old standalone STEP without asserting unrecorded object roles."""
    shape = import_step(path)
    shape.parent = None
    shapes = []
    def visit(value, parent_location):
        world_location = parent_location * value.location
        if value.children:
            for child in value.children:
                visit(child, world_location)
        else:
            shapes.append((value, world_location))
    visit(shape, Location())
    objects = []
    for index, (value, world_location) in enumerate(shapes):
        # This tree was freshly imported for display. Detach after traversal so
        # flattening does not deepcopy an entire assembly once per leaf.
        value.parent = None
        value.location = world_location
        color = getattr(value, "color", None)
        rgb = tuple(color)[:3] if color is not None else (0.4, 0.57, 0.71)
        base = "#" + "".join(f"{round(float(c) * 255):02X}" for c in rgb)
        objects.append(({"name": f"{path.stem}/{index}:{value.label or 'object'}", "role": "unclassified", "appearance": appearance(base)}, value))
    return objects


def _legacy_stage_records(path):
    """Return records reconstructed by the historical stage fallback."""
    for stage_path in path.parent.glob("*_cad-stage.json"):
        stage = json.loads(stage_path.read_text(encoding="utf-8"))
        artifacts = stage.get("artifacts", {})
        keys = [key for key, ref in artifacts.items() if ref == binding(path)]
        if not keys:
            continue
        records = []
        for obj in stage.get("objects", []):
            key = obj.get("stepArtifact")
            if key not in artifacts or (keys[0].startswith("step:") and keys[0] != "step:assembly" and key not in keys):
                continue
            if keys[0] == "step:assembly" and obj.get("role") not in {"manufactured", "manufactured-part"}:
                continue
            records.append({**obj, "step": artifacts[key], "appearance": obj.get("appearance", appearance(obj.get("color", "#6891B5"))),
                            "role": "display-only" if obj.get("role") == "component-reference" else obj.get("role", "unclassified")})
        return records, stage_path
    return None, None


def step_object_dependencies(path, object_names=None, *, allow_legacy_stage=True):
    """Return bound scene/stage and STEP refs using the display loader's rules."""
    path = Path(path).resolve()
    descriptor_path = scene_path(path)
    if descriptor_path.is_file():
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        records = descriptor.get("objects", [])
        stage_ref = binding(descriptor_path)
    else:
        legacy = _legacy_stage_records(path) if allow_legacy_stage else (None, None)
        if legacy[0] is None:
            return {"stage": None, "objects": []}
        records = legacy[0]
        stage_ref = binding(legacy[1])
    requested = None if object_names is None else set(object_names)
    refs = [record.get("step") for record in records
            if isinstance(record, dict) and (requested is None or record.get("name") in requested)
            and isinstance(record.get("step"), dict)]
    return {"stage": stage_ref, "objects": refs}


def load_step_objects(path, *, object_names=None, allow_legacy_stage=True):
    """Return identity-bound objects in semantic coordinates and provenance."""
    from export_audit import _geometry_errors, geometry_record
    path = Path(path).resolve()
    if path.suffix.lower() not in {".step", ".stp"}:
        raise DisplayStepError("display input must be STEP")
    descriptor_path = scene_path(path)
    provenance = {"step": binding(path)}
    objects = []
    if descriptor_path.is_file():
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if (descriptor.get("schema") != SCHEMA or descriptor.get("step") != binding(path) or descriptor.get("units") != "mm"
                or descriptor.get("coordinateSystem") != {"up": "Z", "handedness": "right"}):
            raise DisplayStepError("STEP display descriptor is stale or unsupported")
        provenance["scene"] = binding(descriptor_path)
        records = descriptor["objects"]
        if (not isinstance(records, list) or not records or len(records) > 2000 or
                any(not isinstance(obj, dict) or not isinstance(obj.get("name"), str) or not obj["name"].strip() or
                    obj.get("role") not in {"manufactured", "display-only"} or obj.get("coordinateFrame") != "semantic" or
                    obj.get("transformApplied") is not True for obj in records)):
            raise DisplayStepError("STEP display object identities or coordinate frames are invalid")
    else:
        # Historical stage manifests can recover recorded roles without GLB.
        records, stage_path = (_legacy_stage_records(path) if allow_legacy_stage else (None, None))
        if records is not None:
            if stage_path is not None:
                provenance["scene"] = binding(stage_path)
        if records is None:
            fallback = _fallback_objects(path)
            requested = None if object_names is None else set(object_names)
            available = {record[0]["name"] for record in fallback}
            if requested is not None and requested - available:
                raise DisplayStepError("unknown STEP object name(s): " + ", ".join(sorted(requested - available)))
            return ([record for record in fallback if requested is None or record[0]["name"] in requested], provenance)
    names = set()
    requested = None if object_names is None else set(object_names)
    if requested is not None and any(not isinstance(name, str) or not name for name in requested):
        raise DisplayStepError("selected STEP object names must be nonempty strings")
    available = {record.get("name") for record in records if isinstance(record, dict)}
    if requested is not None and requested - available:
        raise DisplayStepError("unknown STEP object name(s): " + ", ".join(sorted(requested - available)))
    for record in records:
        name = record["name"]
        if name in names:
            raise DisplayStepError("STEP display identities are duplicated")
        names.add(name)
        if requested is not None and name not in requested:
            continue
        object_path = _checked_ref(record["step"], path.parent)
        shape = import_step(object_path)
        shape.parent = None
        errors = _geometry_errors(geometry_record(shape), record["geometry"], name)
        if errors:
            raise DisplayStepError("; ".join(errors))
        objects.append((record, shape))
    if not objects:
        raise DisplayStepError("STEP display has no bound objects")
    return objects, provenance


def step_meshes(path, *, normals=True, object_names=None):
    objects, provenance = load_step_objects(path, object_names=object_names)
    return mesh_step_objects(objects, provenance, normals=normals)


def mesh_step_objects(objects, provenance, *, normals=True):
    """Tessellate already loaded STEP objects, preserving object bindings."""
    from geometry_binding import shape_to_mesh
    items = []
    geometry_cache = {}
    for record, shape in objects:
        solids = list(shape.solids())
        base = solids[0] if len(solids) == 1 else shape
        transform = base.location.wrapped.Transformation()
        matrix = np.eye(4)
        matrix[:3, :4] = [[transform.Value(row, column) for column in range(1, 5)] for row in range(1, 4)]
        local = type(base).cast(base.wrapped.Located(TopLoc_Location()))
        snapshot = BytesIO()
        if not export_brep(local, snapshot): raise DisplayStepError("Could not fingerprint display BRep")
        key = sha256(snapshot.getvalue()).digest()
        if key not in geometry_cache:
            if solids:
                mesh = shape_to_mesh(local, "STEP preview " + record["name"])
            else:
                from brep_tessellation import tessellate_brep
                vertices, faces = tessellate_brep(local, 0.01, 0.1)
                mesh = trimesh.Trimesh(vertices=[[v.X, v.Y, v.Z] for v in vertices], faces=faces, process=False)
            if normals:
                try:
                    mesh = mesh_with_display_normals(mesh)
                except ValueError as error:
                    if hasattr(error, "issue"):
                        error.issue.update(partId=record["name"], coordinateFrame="object-local", units="mm", transform=matrix.tolist(), localToSemanticTransform=matrix.tolist())
                    raise
            geometry_cache[key] = mesh
        # Exact native geometry bytes establish reuse; roles, appearances and
        # placements remain per instance. No manufacturing result uses this cache.
        mesh = geometry_cache[key].copy(include_cache=True)
        mesh.apply_transform(matrix)
        if normals:
            issue = invalid_normal_issue(mesh, record["name"])
            if issue["invalidNormalCount"]:
                raise DisplayStepError("STEP display normals are invalid after placement", **issue)
        mesh.metadata["stepProvenance"] = provenance
        mesh.metadata["amagine3d"] = {"role": record.get("role", "unclassified"), **{key: record[key] for key in ("physicalFeatureRef", "ownerPart", "regionId") if record.get(key)}}
        items.append((record["name"], mesh, record["appearance"]))
    return items, provenance


def preview_payload(path):
    started = time.monotonic()
    items, provenance = step_meshes(path)
    return {"schema": "a3d-step-preview/v1", "units": "mm", "up": "Z", "inputs": provenance,
            "objects": [{"id": name, "positions": np.asarray(mesh.vertices, dtype=np.float32).reshape(-1).tolist(),
                         "indices": np.asarray(mesh.faces, dtype=np.uint32).reshape(-1).tolist(),
                         "normals": np.asarray(mesh.vertex_normals, dtype=np.float32).reshape(-1).tolist(),
                         "appearance": style, "metadata": mesh.metadata["amagine3d"]} for name, mesh, style in items],
            "elapsedMs": round((time.monotonic() - started) * 1000)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    payload = preview_payload(args.model)
    _write(args.out, payload)
    print(json.dumps({"schema": payload["schema"], "objects": len(payload["objects"]), "inputs": payload["inputs"], "elapsedMs": payload["elapsedMs"]}))
