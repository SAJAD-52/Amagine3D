#!/usr/bin/env python3
"""Benchmark small, medium, and larger STL inputs on the CPU renderer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "a3d-public"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

from cpu_z_buffer import (  # noqa: E402
    DEFAULT_MATERIAL,
    MeshInput,
    RenderLimits,
    load_mesh,
    render_contact_sheet,
    render_view,
)
import trimesh  # noqa: E402
from build123d import Box, Compound, Pos, export_step  # noqa: E402
import agent_preview  # noqa: E402
import display_step  # noqa: E402
from agent_protocol import binding  # noqa: E402


def _models(large_subdivisions: int) -> dict[str, trimesh.Trimesh]:
    return {
        "small": trimesh.creation.box(extents=(4.0, 3.0, 2.0)),
        "medium": trimesh.creation.icosphere(subdivisions=3, radius=2.0),
        "large": trimesh.creation.icosphere(
            subdivisions=large_subdivisions, radius=2.0
        ),
    }


def _benchmark(path: Path, resolution: int, limits: RenderLimits) -> dict[str, object]:
    tracemalloc.reset_peak()
    loaded = load_mesh(path)
    inputs = [MeshInput(path.stem, loaded, DEFAULT_MATERIAL, path)]

    single_started = time.perf_counter()
    single = render_view(inputs, "isometric", resolution, limits=limits)
    single_seconds = time.perf_counter() - single_started

    contact = render_contact_sheet(
        inputs,
        resolution,
        title=path.stem,
        limits=limits,
    )
    _, traced_peak = tracemalloc.get_traced_memory()
    return {
        "contact_sheet_seconds": round(contact.elapsed_seconds, 6),
        "peak_memory_bytes": max(
            int(traced_peak), single.stats.buffer_bytes, contact.peak_buffer_bytes
        ),
        "single_view_seconds": round(single_seconds, 6),
        "triangle_count": len(loaded.faces),
        "view_count": len(contact.stats),
        "views": {stat.view: stat.to_dict() for stat in contact.stats},
    }


def _benchmark_step(root: Path, resolution: int, limits: RenderLimits) -> dict[str, object]:
    """Measure the complete STEP-backed mixed preview and its cache hit."""
    from agent_api import save

    step = root / "mixed-fixture.step"
    source = root / "fixture.py"
    source.write_text("# benchmark input\n", encoding="utf-8")
    fixture = Compound(children=[Box(10, 8, 6), Pos(14, 0, 0) * Box(4, 4, 4)])
    if not export_step(fixture, step):
        raise RuntimeError("could not create benchmark STEP fixture")
    report_path = save(root, ".a3d-agent/runs/benchmark-parent.json", {
        "schema": "a3d-draft-result/v2", "runId": "renderer-benchmark",
        "status": "draft", "source": binding(source), "taskInputBindings": {"source": binding(source)},
        "artifacts": {"step": binding(step)}, "issues": []})
    request = {"report": str(report_path), "sha256": binding(report_path)["sha256"], "requests": [
        {"kind": "camera", "view": "back", "size": resolution},
        {"kind": "section", "plane": {"origin": [0, 0, 0], "normal": [0, 0, 1], "u": [1, 0, 0]}},
    ]}
    timings: dict[str, float] = {}
    counts = {"step_load_calls": 0, "tessellation_calls": 0}
    original_load, original_mesh = display_step.load_step_objects, display_step.mesh_step_objects

    def timed_load(*args, **kwargs):
        started = time.perf_counter()
        counts["step_load_calls"] += 1
        try:
            return original_load(*args, **kwargs)
        finally:
            timings["step_load_seconds"] = timings.get("step_load_seconds", 0.0) + time.perf_counter() - started

    def timed_mesh(*args, **kwargs):
        started = time.perf_counter()
        counts["tessellation_calls"] += 1
        try:
            return original_mesh(*args, **kwargs)
        finally:
            timings["tessellation_seconds"] = timings.get("tessellation_seconds", 0.0) + time.perf_counter() - started

    display_step.load_step_objects = timed_load
    display_step.mesh_step_objects = timed_mesh
    try:
        started = time.perf_counter()
        first = agent_preview.preview(root, request)
        mixed_seconds = time.perf_counter() - started
        started = time.perf_counter()
        cached = agent_preview.preview(root, request)
        cache_hit_seconds = time.perf_counter() - started
    finally:
        display_step.load_step_objects, display_step.mesh_step_objects = original_load, original_mesh
    kinds = {item["kind"] for item in first["observations"]}
    if first["cached"] or not cached["cached"] or kinds != {"camera", "section"}:
        raise RuntimeError(f"mixed preview/cache benchmark did not exercise a miss followed by a hit: first={first['cached']} second={cached['cached']}")
    return {**timings, **counts, "mixed_camera_section_seconds": round(mixed_seconds, 6),
            "cache_hit_seconds": round(cache_hit_seconds, 6), "cache_hit": cached["cached"],
            "image_count": len(first["observations"]), "triangles_loaded": first["limits"]["trianglesLoaded"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolution", type=int, default=480)
    parser.add_argument("--large-subdivisions", type=int, default=5)
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.resolution < 320:
        parser.error("--resolution must be at least 320")
    if args.large_subdivisions < 4 or args.large_subdivisions > 6:
        parser.error("--large-subdivisions must be between 4 and 6")

    limits = RenderLimits()
    results: dict[str, object] = {
        "default_parallel_views": False,
        "default_processes": 1,
        "resolution": args.resolution,
        "renderer": "cpu-z-buffer/v1",
        "supersample": 1,
        "workloads": {},
    }
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="amagine-cpu-render-") as temporary:
        temporary_root = Path(temporary).resolve()
        for name, mesh in _models(args.large_subdivisions).items():
            path = temporary_root / f"{name}.stl"
            mesh.export(path)
            results["workloads"][name] = _benchmark(path, args.resolution, limits)
        step_root = temporary_root / "step-preview"
        step_root.mkdir()
        results["step_mixed_preview"] = _benchmark_step(step_root, args.resolution, limits)
    tracemalloc.stop()

    payload = json.dumps(results, indent=2)
    if args.out:
        destination = Path(args.out).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
