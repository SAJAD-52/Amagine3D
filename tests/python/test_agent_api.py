from __future__ import annotations

from copy import deepcopy
import contextlib
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import signal
import shutil
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "a3d-public"))
import agent_api
import agent_protocol as protocol


class AgentProtocolTests(unittest.TestCase):
    def test_next_request_keeps_the_resolved_issue_field_and_returns_every_item(self):
        document = {"issues": [{"id": "problem", "observed": [{"index": i, "evidence": "x" * 400} for i in range(45)]}]}
        ref = self.report(document)
        page = agent_api.dispatch(self.root, "inspect", {"report": ref["path"], "sha256": ref["sha256"], "issueId": "problem", "field": "/observed"})
        values = []
        while True:
            self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
            values.extend(item["value"]["index"] for item in page["items"])
            request = page["page"]["nextRequest"]
            if not request:
                break
            self.assertEqual(request["field"], "/issues/0/observed")
            page = agent_api.dispatch(self.root, "inspect", request)
        self.assertEqual(values, list(range(45)))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def report(self, value):
        path = self.root / "report.json"
        path.write_text(json.dumps(value, ensure_ascii=False))
        return protocol.binding(path)

    def test_issue_pages_preserve_every_issue_and_report_bytes(self):
        document = {"runId": "run", "status": "failed", "issues": [
            {"id": str(i), "severity": "error" if i % 2 else "warning", "message": '"\\\n屏幕' * 100,
             "observed": {"value": i}, "expected": {"value": i + 1}} for i in range(50)]}
        ref = self.report(document)
        before = Path(ref["path"]).read_bytes()
        ids, cursor = [], None
        while True:
            page = protocol.summary(document, ref, cursor)
            self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
            ids.extend(item["id"] for item in page["items"])
            self.assertEqual(page["page"]["total"], 50)
            cursor = page["page"]["next"]
            if not cursor:
                break
        self.assertEqual(set(ids), {str(i) for i in range(50)})
        self.assertEqual(len(ids), 50)
        self.assertEqual(Path(ref["path"]).read_bytes(), before)

    def test_known_placement_keeps_offenders_and_counts_normal_observations(self):
        observed = {"offenders": [{"feature_id": "socket", "actual": 2, "expected": 0}],
                    "observations": [{"id": i, "measurements": [1] * 100} for i in range(29)]}
        issue = {"id": "locator", "check": "semantic_feature_placement", "observed": observed}
        original = deepcopy(issue)
        result = protocol.issue_view(issue, 0)
        self.assertEqual(result["observed"]["offenders"], observed["offenders"])
        self.assertEqual(result["observed"]["observationCounts"], {"total": 29, "offenders": 1})
        self.assertEqual(result["observed"]["allObservations"]["field"], "/issues/0/observed/observations")
        self.assertEqual(issue, original)

    def test_large_string_is_reassemblable_and_cursor_is_bound(self):
        value = '"\n\\屏幕' * 5_000
        document = {"detail": value}
        ref = self.report(document)
        cursor, chunks = None, []
        while True:
            page = protocol.inspect(document, ref, field="/detail", cursor=cursor)
            self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
            chunks.extend(page["items"])
            cursor = page["page"]["next"]
            if not cursor:
                break
        self.assertEqual("".join(item["textChunk"] for item in chunks), value)
        self.assertEqual([item["textOffset"] for item in chunks], list(range(0, len(value), 500)))
        first = protocol.inspect(document, ref, field="/detail")
        with self.assertRaisesRegex(ValueError, "cursor"):
            protocol.inspect(document, {**ref, "sha256": "b" * 64}, field="/detail", cursor=first["page"]["next"])
        Path(ref["path"]).write_text('{}')
        with self.assertRaisesRegex(ValueError, "changed"):
            protocol.read_report(self.root, ref["path"], ref["sha256"])

    def test_large_metadata_and_issue_fields_are_exact_references(self):
        document = {"runId": "run", "status": "failed", "inputBindings": {"source": "x" * 40_000},
                    "artifacts": {f"file{i}": {"path": "long" * 2_000, "sha256": "a" * 64} for i in range(50)},
                    "issues": [{"id": "large", "message": "large" * 20_000, "expected": [5] * 30_000}]}
        page = protocol.summary(document, self.report(document))
        self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
        self.assertEqual(page["items"][0]["message"]["field"], "/issues/0/message")
        self.assertEqual(page["items"][0]["expected"]["field"], "/issues/0/expected")

    def test_pointer_and_scope_errors_do_not_guess_fields(self):
        self.assertEqual(protocol.at_pointer({"a/b": {"~": [2]}}, "/a~1b/~0/0"), 2)
        for pointer in ("inputs.source.sha256", "/parts", "/a~1b/~0/-1"):
            with self.assertRaises(ValueError):
                protocol.at_pointer({"a/b": {"~": [2]}}, pointer)
        with self.assertRaisesRegex(ValueError, "workspace"):
            protocol.scoped(self.root, "../outside.json", exists=False)
        link = self.root / "link"
        link.symlink_to(self.root.parent)
        with self.assertRaisesRegex(ValueError, "workspace"):
            protocol.scoped(self.root, "link/file.json", exists=False)

    def test_contract_field_and_shared_cause_measurements_keep_their_identity(self):
        import cad_compile
        issues = [{"causeId": "cause", "code": "QA.FAILED", "stage": stage, "part": part,
                   "observed": {"value": value, "coordinateFrame": frame}, "expected": {"value": 0}}
                  for stage, part, value, frame in (("step-qa", "body", 4, "semantic"), ("mesh-qa", "plate", 7, "print-pose"))]
        grouped = cad_compile._aggregate_issues(issues)
        self.assertEqual(len(grouped), 1)
        self.assertEqual([item["observed"]["value"] for item in grouped[0]["impacts"]], [4, 7])
        item = protocol.issue_view({"field": "interfaces[0].kind", "expected": ["pin-socket"]}, 0)
        self.assertEqual(item["field"], "interfaces[0].kind")
        self.assertEqual(item["evidenceField"], "/issues/0")

    def test_derived_view_references_always_resolve_into_saved_evidence(self):
        document = {"issues": [{"check": "printability_local_thin_region", "observed": {
            "sampling": {"minimum_sample": {"point_mm": [1, 2, 3], "large_detail": "a" * 3_000}},
            "affected_feature_attribution": {"basis": "b" * 3_000},
            "measurement_context": {"coordinate_frame": {"name": "part-print"}, "other": "c" * 4_000}}}]}
        view = protocol.issue_view(document["issues"][0], 0)
        def verify(value):
            if isinstance(value, dict):
                if "field" in value and value.get("type") in {"str", "dict", "list"}:
                    protocol.at_pointer(document, value["field"])
                for item in value.values(): verify(item)
            elif isinstance(value, list):
                for item in value: verify(item)
        verify(view)
        empty = protocol.summary({"issues": []}, self.report({"issues": []}))
        self.assertNotIn("allArtifacts", empty)


class AgentOperationsTests(unittest.TestCase):
    def test_context_and_inspect_resolve_exact_run_preview_and_prefer_only_fresh_image(self):
        source = self.root / "model.py"
        image_parent = self.root
        while len(str(image_parent / "draft.png").encode()) < 374:
            remaining = 374 - len(str(image_parent).encode()) - 2 - len("draft.png")
            image_parent = image_parent / ("p" * min(100, remaining))
        image = image_parent / "draft.png"
        source.write_text("# source")
        image.parent.mkdir(parents=True)
        image.write_bytes(b"preview image")
        self.assertEqual(len(str(image).encode()), 374)
        source_ref, image_ref = protocol.binding(source), protocol.binding(image)
        report_path = self.root / ".a3d-agent" / "runs" / "draft.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"schema": "a3d-draft-result/v2", "runId": "draft-run",
            "status": "draft", "source": source_ref, "taskInputBindings": {"source": source_ref},
            "artifacts": {}, "issues": []}))
        report_ref = protocol.binding(report_path)
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": "a3d-agent/v1",
            "latest": {"operation": "draft", "report": report_ref, "inputs": {"source": source_ref},
                       "outputs": {"preview": image_ref}}})
        context = agent_api.context(self.root, {})
        self.assertEqual(context["latest"]["preview"]["path"], str(image))
        self.assertEqual(context["latest"]["preview"]["sha256"], image_ref["sha256"])
        inspected = agent_api.dispatch(self.root, "inspect", {"report": report_ref["path"],
            "sha256": report_ref["sha256"]})
        self.assertEqual(inspected["artifacts"]["preview"], image_ref)
        self.assertEqual(inspected["previewState"], "current")

        displayed = protocol.summary({"status": "awaiting-visual-review", "pass": True,
            "visualReviewRequired": True, "artifacts": {"preview": image_ref}}, report_ref)
        self.assertEqual(displayed["artifacts"]["preview"], image_ref)
        self.assertEqual(displayed["status"], "awaiting-visual-review")
        self.assertTrue(displayed["pass"])
        self.assertTrue(displayed["visualReviewRequired"])

        stale_preview = {"path": str(image), "sha256": "0" * 64}
        valid_diagnostic = image_ref
        document = {"artifacts": {"preview": stale_preview, "diagnosticPreview": valid_diagnostic},
                    "source": source_ref, "taskInputBindings": {"source": source_ref},
                    "previewScope": {"kind": "partial", "partIds": ["shell"]}}
        projected, metadata = agent_api._summary_document(self.root, document)
        self.assertNotIn("preview", projected["artifacts"])
        self.assertEqual(projected["artifacts"]["diagnosticPreview"], valid_diagnostic)
        self.assertEqual(metadata["previewArtifact"], "diagnosticPreview")
        self.assertEqual(metadata["previewState"], "diagnostic")
        summary = protocol.summary(projected, report_ref, metadata_extra=metadata)
        self.assertEqual(summary["previewScope"], {"kind": "partial", "partIds": ["shell"]})
        other_source = self.root / "other.py"
        other_source.write_text("# other source")
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA,
            "activeInput": {"kind": "source", "path": str(other_source)}})
        _, historical_metadata = agent_api._summary_document(self.root, document)
        self.assertEqual(historical_metadata["previewState"], "diagnostic-historical")
        image.write_bytes(b"tampered")
        projected, metadata = agent_api._summary_document(self.root, document)
        self.assertEqual(projected["artifacts"], {})
        self.assertEqual(metadata["previewState"], "stale-or-missing")
        _, stale_diagnostic = agent_api._summary_document(self.root, {
            "artifacts": {"diagnosticPreview": image_ref}, "source": source_ref,
            "taskInputBindings": {"source": source_ref}})
        self.assertEqual(stale_diagnostic["previewState"], "stale-or-missing")

    def test_long_image_binding_survives_over_budget_dispatch_with_status_and_scope(self):
        image_parent = self.root
        while len(str(image_parent / "diagnostic.png").encode()) < 374:
            remaining = 374 - len(str(image_parent).encode()) - 2 - len("diagnostic.png")
            image_parent = image_parent / ("d" * min(100, remaining))
        image = image_parent / "diagnostic.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"diagnostic")
        self.assertEqual(len(str(image).encode()), 374)
        cad_report_path = self.root / "cad.json"
        cad_report_path.write_text(json.dumps({"schema": "evidence-cad-compile-result/v2", "status": "failed",
                                  "pass": False, "visualReviewRequired": True, "issues": []}))
        cad_report = protocol.binding(cad_report_path)
        image_ref = protocol.binding(image)
        result = {"schema": protocol.SCHEMA, "report": cad_report, "status": "failed", "pass": False,
                  "visualReviewRequired": True, "issueCounts": {"errors": 1, "warnings": 0},
                  "artifacts": {"preview": {"path": image_ref["path"], "sha256": "0" * 64},
                                "diagnosticPreview": image_ref},
                  "previewScope": {"kind": "partial", "partIds": ["shell"]},
                  "items": [{"id": str(index), "message": "diagnostic detail " + "x" * 1_000}
                            for index in range(20)]}
        self.assertGreater(protocol.wire_size(result), 9_000)
        with mock.patch.dict(agent_api.OPERATIONS, {"display": lambda _root, _args: result}), \
             mock.patch.object(agent_api, "_archive_resolved_failure"), \
             mock.patch.object(agent_api, "remember_failure"):
            response = agent_api.dispatch(self.root, "display", {"report": cad_report["path"],
                "sha256": cad_report["sha256"]})
        self.assertLessEqual(protocol.wire_size(response), 9_000)
        self.assertEqual(response["report"], cad_report)
        self.assertEqual(response["readMore"],
                         "a3d agent inspect using response.path, response.sha256 and field /items")
        self.assertEqual(response["details"], {"field": "/items", "type": "list"})
        self.assertNotEqual(response["response"], cad_report)
        self.assertEqual(response["artifacts"], {"diagnosticPreview": image_ref})
        self.assertEqual(response["previewState"], "diagnostic")
        self.assertEqual(response["previewScope"], {"kind": "partial", "partIds": ["shell"]})
        self.assertEqual(response["status"], "failed")
        self.assertFalse(response["pass"])
        self.assertTrue(response["visualReviewRequired"])
        with self.assertRaisesRegex(ValueError, "direct image handoff budget"):
            protocol._direct_preview({"path": "/" + "x" * 1_300, "sha256": "a" * 64}, "/artifacts/preview")
        detail = agent_api.dispatch(self.root, "inspect", {"report": response["response"]["path"],
            "sha256": response["response"]["sha256"], "field": "/items"})
        self.assertEqual(detail["page"]["total"], 20)

    def test_oversized_context_keeps_latest_image_scope_and_report_binding(self):
        image_parent = self.root
        while len(str(image_parent / "context.png").encode()) < 374:
            remaining = 374 - len(str(image_parent).encode()) - 2 - len("context.png")
            image_parent = image_parent / ("c" * min(100, remaining))
        image = image_parent / "context.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"context preview")
        image_ref = protocol.binding(image)
        report_path = self.root / "latest.json"
        report_path.write_text("{}")
        report_ref = protocol.binding(report_path)
        result = {"schema": protocol.SCHEMA, "padding": "x" * 10_000,
                  "latest": {"report": report_ref, "status": "draft", "pass": False,
                             "visualReviewRequired": True, "inputsCurrent": True,
                             "previewState": "diagnostic-historical",
                             "previewCurrentness": {"inputsCurrent": False, "activeInputsMatch": False,
                                                    "historical": True},
                             "historicalPreview": {**image_ref, "artifact": "diagnosticPreview"},
                             "previewScope": {"kind": "partial", "partIds": ["shell"]}}}
        with mock.patch.dict(agent_api.OPERATIONS, {"context": lambda _root, _args: result}):
            response = agent_api.dispatch(self.root, "context", {})
        self.assertLessEqual(protocol.wire_size(response), 9_000)
        self.assertEqual(response["latest"]["report"], report_ref)
        self.assertEqual(response["latest"]["historicalPreview"], {
            "path": str(image), "sha256": image_ref["sha256"], "artifact": "diagnosticPreview"})
        self.assertEqual(response["latest"]["previewState"], "diagnostic-historical")
        self.assertEqual(response["latest"]["previewCurrentness"], result["latest"]["previewCurrentness"])
        self.assertEqual(response["latest"]["previewScope"], {"kind": "partial", "partIds": ["shell"]})

    def test_manufacturing_pass_survives_render_failure_and_preview_retry_reuses_checks(self):
        import cad_compile
        runtime = ROOT / "skills" / "a3d-public"
        examples = ROOT / "skills" / "a3d-text" / "examples"
        for name in ("simple_brep_build.py", "simple_brep_intent.py"):
            shutil.copyfile(examples / name, self.root / name)
        source = self.root / "simple_brep_build.py"
        source.write_text("from pathlib import Path\nPath('source-count').write_text('once')\n" + source.read_text())
        env = {**os.environ, "AMAGINE3D_RUNTIME_DIR": str(runtime), "PYTHONPATH": str(runtime)}
        setup = subprocess.run(["node", str(ROOT / "bin/a3d.mjs"), "profile", "--machine", "a1-mini", "--nozzle", "0.4", "--tool", "0", "--out", "simple_brep_printer-profile.json"], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        setup = subprocess.run([sys.executable, "simple_brep_intent.py"], cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        original_run = cad_compile.CommandRunner.run
        def run(runner, stage, argv, **kwargs):
            if stage == "render": return cad_compile.CommandResult(1, 0, "injected PNG failure")
            return original_run(runner, stage, argv, **kwargs)
        with mock.patch.object(cad_compile.CommandRunner, "run", run):
            first = agent_api.dispatch(self.root, "build", {"phase": "compile", "source": source.name,
                "intent": "simple_brep_intent.json", "scene": "simple_brep_scene.json"})
        ref, parent = protocol.read_report(self.root, first["report"]["path"], first["report"]["sha256"])
        self.assertEqual(parent["status"], "awaiting-preview", parent["issues"])
        self.assertFalse(parent["pass"])
        self.assertEqual(parent["artifactStages"]["manufacturing"]["status"], "passed")
        recovery = agent_api.context(self.root, {})
        self.assertEqual(recovery["suggestedPhase"], "display-retry")
        display_request = {"report": ref["path"], "sha256": ref["sha256"]}
        self.assertEqual(recovery["nextDisplay"], display_request)
        protected = {path: path.read_bytes() for path in self.root.rglob("*.step")}
        protected[source] = source.read_bytes()
        intent = self.root / "simple_brep_intent.json"
        protected[intent] = intent.read_bytes()
        with mock.patch.object(cad_compile.CommandRunner, "run", side_effect=AssertionError("QA and source must not rerun")):
            result = agent_api.dispatch(self.root, "display", display_request)
        display_preview = result["artifacts"]["preview"]
        self.assertEqual(len(display_preview["sha256"]), 64)
        context_preview = agent_api.context(self.root, {})["latest"]["preview"]
        self.assertEqual({key: context_preview[key] for key in ("path", "sha256")}, display_preview)
        inspected = agent_api.dispatch(self.root, "inspect", {"report": result["report"]["path"],
            "sha256": result["report"]["sha256"]})
        self.assertEqual(inspected["artifacts"]["preview"], display_preview)
        _, retried = protocol.read_report(self.root, result["report"]["path"], result["report"]["sha256"])
        self.assertEqual(retried["status"], "awaiting-visual-review", retried["issues"])
        self.assertTrue(retried["pass"])
        self.assertEqual(retried["artifactStages"]["manufacturing"]["status"], "passed")
        self.assertTrue(retried["manufacturingEvidence"]["reused"])
        self.assertEqual(retried["manufacturingEvidence"]["parentReport"], ref)
        self.assertEqual([i for i in retried["issues"] if i.get("severity") == "warning"], [i for i in parent["issues"] if i.get("severity") == "warning"])
        self.assertEqual(protected, {path: path.read_bytes() for path in protected})
        self.assertEqual((self.root / "source-count").read_text(), "once")
        cached = agent_api.dispatch(self.root, "display", display_request)
        self.assertTrue(cached["reused"])
        self.assertFalse(list(self.root.rglob("*.glb")))
        # A changed print export invalidates the reuse proof, including cache hits.
        _, build = protocol.read_report(self.root, parent["artifacts"]["buildReport"]["path"], parent["artifacts"]["buildReport"]["sha256"])
        stl = Path(next(value["path"] for key, value in build["artifacts"].items() if key.startswith("stl:")))
        stl.write_bytes(stl.read_bytes() + b"changed")
        rejected = agent_api.dispatch(self.root, "display", display_request)
        self.assertFalse(rejected.get("pass", False))
        self.assertFalse(rejected.get("reused", False))

    def test_api_error_preserves_producer_hint_and_adds_request_route_when_missing(self):
        for details, expected in (
            ({"repairHint": "Specific producer advice."}, "Specific producer advice."),
            ({}, "$AMAGINE3D_RUNTIME_DIR/references/error-diagnostics.md#request-errors"),
            ({"repairHint": "   "}, "$AMAGINE3D_RUNTIME_DIR/references/error-diagnostics.md#request-errors"),
        ):
            with self.subTest(details=details), \
                 mock.patch.object(agent_api, "dispatch", side_effect=agent_api.RequestError("bad request", **details)), \
                 mock.patch("sys.argv", ["a3d-agent", "validate", "{}"]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(agent_api.main(), 2)
            response = json.loads(output.getvalue())
            self.assertEqual(response["status"], "api-error")
            if details.get("repairHint", "").strip():
                self.assertEqual(response["error"]["repairHint"], expected)
            else:
                self.assertIn(expected, response["error"]["repairHint"])

    def test_another_designs_draft_and_failure_cannot_route_current_recovery(self):
        from tests.python.test_design_language import box_design
        old, new = self.root / "old.a3d.json", self.root / "new.a3d.json"
        old.write_text(json.dumps(box_design()))
        new.write_text(json.dumps(box_design()))
        report = agent_api.save(self.root, "old-result.json", {"schema": "a3d-draft-result/v2", "runId": "old-run",
            "status": "draft", "issues": [], "taskInputBindings": {"design": protocol.binding(old)}})
        latest = {"operation": "draft", "runId": "old-run", "report": protocol.binding(report), "inputs": {"design": protocol.binding(old)}}
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA, "latest": latest})
        agent_api.remember_failure(self.root, "validate", {"design": old.name}, {"report": protocol.binding(report)})
        agent_api.dispatch(self.root, "validate", {"design": new.name})
        result = agent_api.context(self.root, {})
        self.assertEqual(result["design"]["design"], str(new))
        self.assertFalse(result["latest"]["inputsCurrent"])
        self.assertFalse(result["historicalFailures"][0]["inputsCurrent"])
        self.assertNotIn("nextInspect", result)
        self.assertEqual(result["suggestedPhase"], "functional-draft")

    def test_checkpoint_is_bound_to_current_inputs_and_never_changes_acceptance(self):
        source = self.root / "model.py"
        source.write_text("# current body")
        inputs = {"source": protocol.binding(source)}
        report = agent_api.save(self.root, "draft-result.json", {"schema": "a3d-draft-result/v2", "runId": "draft-run",
            "status": "draft", "issues": [], "taskInputBindings": inputs})
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA,
            "latest": {"operation": "draft", "report": protocol.binding(report), "inputs": inputs},
            "activeInput": {"kind": "source", "path": str(source)}})
        result = agent_api.dispatch(self.root, "checkpoint", {"report": str(report), "sha256": protocol.binding(report)["sha256"],
            "observations": ["module opening visible", "中" * 250], "todo": ["check insertion path"], "functionalReady": True})
        self.assertEqual(result["status"], "saved")
        restored = agent_api.context(self.root, {})
        self.assertEqual(restored["suggestedPhase"], "contract")
        self.assertFalse(restored["latest"]["pass"])
        self.assertTrue(restored["latest"]["visualReviewRequired"])
        source.write_text("# changed body")
        self.assertFalse(agent_api.context(self.root, {})["checkpoint"]["inputsCurrent"])

    def test_display_retry_never_executes_source_or_changes_saved_step(self):
        source = self.root / "model.py"
        source.write_text("""from pathlib import Path
Path('source-count').write_text('ran once')
from build123d import Box
import display_step
def fail(*args, **kwargs):
    raise display_step.DisplayStepError('injected display failure')
display_step.export_display_step = fail
from cad_draft import export_draft
export_draft({'body': Box(10, 10, 10)})
""")
        command = self.cli("build", {"phase": "draft", "source": source.name})
        first = json.loads(command.stdout)
        self.assertEqual(command.returncode, 1, command.stdout + command.stderr)
        ref, full = protocol.read_report(self.root, first["report"]["path"], first["report"]["sha256"])
        step = Path(full["artifacts"]["step"]["path"])
        step_bytes, source_bytes = step.read_bytes(), source.read_bytes()
        command = self.cli("display", {"report": ref["path"], "sha256": ref["sha256"]})
        result = json.loads(command.stdout)
        self.assertEqual(command.returncode, 0, command.stdout + command.stderr)
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("pass", result)
        self.assertEqual(step.read_bytes(), step_bytes)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual((self.root / "source-count").read_text(), "ran once")
        _, full = protocol.read_report(self.root, result["report"]["path"], result["report"]["sha256"])
        self.assertEqual(full["artifactStages"]["display"]["status"], "passed")
        self.assertEqual(full["artifactStages"]["manufacturing"]["status"], "pending")
        self.assertNotIn("lastFailure", agent_api.state(self.root))
        source.write_text(source_bytes.decode() + "\n# new controls")
        rejected = json.loads(self.cli("display", {"report": ref["path"], "sha256": ref["sha256"]}).stdout)
        self.assertEqual(rejected["status"], "api-error")

    def test_validate_and_display_success_preserve_current_manufacturing_failure(self):
        source = self.root / "model.py"
        source.write_text("# unchanged current geometry")
        inputs = {"source": protocol.binding(source)}
        failed = agent_api.save(self.root, "failed.json", {"schema": "evidence-cad-compile-result/v2", "runId": "failed-run",
            "status": "failed", "pass": False, "taskInputBindings": inputs,
            "artifactStages": {"geometry": {"status": "passed"}, "cadExport": {"status": "passed"},
                "display": {"status": "passed"}, "manufacturing": {"status": "failed"}},
            "issues": [{"code": "INSTALLATION.BLOCKED", "severity": "error", "stage": "installation"}]})
        failure_ref = protocol.binding(failed)
        agent_api.remember_failure(self.root, "build", {"phase": "compile", "source": source.name}, {"report": failure_ref})
        validation = agent_api.save(self.root, "valid.json", {"schema": "a3d-validation/v1", "runId": "validation-run",
            "status": "valid", "pass": True, "inputBindings": inputs, "issues": []})
        with mock.patch.dict(agent_api.OPERATIONS, {"validate": lambda root, args: {"status": "valid", "report": protocol.binding(validation)}}):
            agent_api.dispatch(self.root, "validate", {"source": source.name})
        self.assertEqual(agent_api.state(self.root)["lastFailure"]["report"], failure_ref)
        display = agent_api.save(self.root, "display.json", {"schema": "a3d-draft-result/v2", "runId": "display-run",
            "status": "draft", "taskInputBindings": inputs, "parentReport": failure_ref,
            "artifactStages": {"display": {"status": "passed"}, "manufacturing": {"status": "pending"}}, "issues": []})
        with mock.patch.dict(agent_api.OPERATIONS, {"display": lambda root, args: {"status": "draft", "report": protocol.binding(display)}}):
            agent_api.dispatch(self.root, "display", {"report": failure_ref["path"], "sha256": failure_ref["sha256"]})
        self.assertEqual(agent_api.context(self.root, {})["nextInspect"], {"report": failure_ref["path"], "sha256": failure_ref["sha256"]})
        passed = agent_api.save(self.root, "passed.json", {"schema": "evidence-cad-compile-result/v2", "runId": "passed-run",
            "status": "awaiting-visual-review", "pass": True, "taskInputBindings": inputs, "issues": []})
        with mock.patch.dict(agent_api.OPERATIONS, {"build": lambda root, args: {"status": "awaiting-visual-review", "report": protocol.binding(passed)}}):
            agent_api.dispatch(self.root, "build", {"phase": "compile", "source": source.name, "intent": "intent.json", "scene": "scene.json"})
        self.assertNotIn("lastFailure", agent_api.state(self.root))
        self.assertEqual(agent_api.state(self.root)["failureHistory"][0]["report"], failure_ref)

    def test_new_intent_for_same_design_makes_old_acceptance_historical_and_supplies_compile(self):
        from tests.python.test_design_language import box_design
        design = self.root / "model.a3d.json"
        design.write_text(json.dumps(box_design()))
        old_intent, new_intent = self.root / "intent-v1.json", self.root / "intent-v2.json"
        old_intent.write_text(json.dumps({"part": "body", "assumptions": ["old"]}))
        new_intent.write_text(json.dumps({"part": "body", "assumptions": ["new"]}))
        inputs = {"design": protocol.binding(design), "intent": protocol.binding(old_intent)}
        old_report = agent_api.save(self.root, "accepted.json", {"schema": "evidence-cad-compile-result/v2", "runId": "old-run",
            "status": "awaiting-visual-review", "pass": True, "taskInputBindings": inputs, "issues": []})
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA, "latest": {
            "operation": "compile", "report": protocol.binding(old_report), "inputs": inputs},
            "activeInput": {"kind": "design", "path": str(design)}, "activeIntent": protocol.binding(old_intent)})
        self.assertTrue(agent_api.context(self.root, {})["latest"]["inputsCurrent"])
        current = agent_api.state(self.root)
        new_inputs = {"design": protocol.binding(design), "intent": protocol.binding(new_intent)}
        agent_api._activate_inputs(self.root, current, new_inputs)
        validation = agent_api.save(self.root, "validation.json", {"schema": "a3d-validation/v1", "runId": "validation-run",
            "status": "valid", "pass": True, "inputBindings": new_inputs, "issues": []})
        current["validation"] = {"report": protocol.binding(validation), "capabilityVersion": agent_api.capability_version()}
        agent_api.save(self.root, ".a3d-agent/state.json", current)
        context = agent_api.context(self.root, {})
        self.assertFalse(context["latest"]["inputsCurrent"])
        self.assertEqual(context["nextBuild"], {"phase": "compile", "design": str(design),
            "revision": new_inputs["design"]["sha256"], "intent": str(new_intent)})
        self.assertEqual(context["suggestedPhase"], "compile-current-inputs")
        self.assertNotIn("nextInspect", context)
        self.assertTrue(agent_api._active_matches(self.root, current, {"design": protocol.binding(design)}))
        agent_api._activate_inputs(self.root, current, {"source": {"path": str(self.root / "other.py"), "sha256": "a" * 64}})
        self.assertNotIn("activeIntent", current)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def cli(self, operation, arguments=None, *extra):
        return subprocess.run(["node", str(ROOT / "bin" / "a3d.mjs"), "agent", operation,
                               *([json.dumps(arguments)] if arguments is not None else []), *extra],
                              cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, text=True, timeout=60)

    def test_cli_context_and_argument_file(self):
        result = self.cli("context")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        document = json.loads(result.stdout)
        self.assertEqual(document["workspace"], str(self.root))
        self.assertIsNone(document["latest"])
        self.assertIn("unknown", document["visualReview"])
        self.assertEqual(result.stderr, "")
        (self.root / "args.json").write_text('{"source":"bad.py"}')
        (self.root / "bad.py").write_text('def broken(:\n')
        failed = self.cli("validate", None, "--args-file", "args.json")
        self.assertEqual(failed.returncode, 1, failed.stderr + failed.stdout)
        report = json.loads(failed.stdout)
        self.assertFalse(report["pass"])
        validated = agent_api.dispatch(self.root, "validate", {"source": "bad.py"})
        self.assertIn("repairHint", validated["items"][0])
        self.assertIn("error-diagnostics.md#", validated["items"][0]["repairHint"])

    def test_design_error_route_is_added_before_dispatch_response(self):
        from design_language import DesignError
        failure = DesignError("BINDING_MISSING", "missing binding", "nodes.cut.owner",
                              ruleId="owner-binding", owner="body")
        def fail_operation(root, args):
            raise failure
        with mock.patch.dict(agent_api.OPERATIONS, {"design": fail_operation}):
            result = agent_api.dispatch(self.root, "design", {"action": "read", "design": "model.a3d.json"})
        self.assertEqual(result["issues"][0]["field"], "nodes.cut.owner")
        self.assertEqual(result["issues"][0]["ruleId"], "owner-binding")
        self.assertIn("error-diagnostics.md#declaration-and-binding-errors", result["issues"][0]["repairHint"])
        self.assertNotIn("report", result)

    def test_large_api_error_pointer_binds_hint_without_mutating_saved_report(self):
        with mock.patch.object(agent_api, "dispatch", side_effect=ValueError("x" * 10_000)), \
             mock.patch.object(agent_api.Path, "cwd", return_value=self.root), \
             mock.patch("sys.argv", ["a3d-agent", "validate", "{}"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent_api.main(), 2)
        response = json.loads(output.getvalue())
        self.assertEqual(response["status"], "api-error")
        self.assertEqual(response["error"], "Large error; inspect report /error")
        ref = response["report"]
        path = Path(ref["path"])
        raw = path.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), ref["sha256"])
        saved = json.loads(raw)
        self.assertIn("error-diagnostics.md#", saved["error"]["repairHint"])
        inspected = agent_api.dispatch(self.root, "inspect", {"report": ref["path"], "sha256": ref["sha256"], "field": "/error"})
        error_value = next(item["value"] for item in inspected["items"] if item["field"].endswith("/repairHint"))
        self.assertIn("error-diagnostics.md#", error_value)
        self.assertEqual(path.read_bytes(), raw)

    def test_capabilities_preserve_bind_semantics_and_session_knowledge(self):
        page = agent_api.dispatch(self.root, "capabilities", {"symbols": ["SelfTappingScrewPair.bind"]})
        helper = page["items"][0]["value"]
        self.assertIn("boss_mode", helper["signature"])
        self.assertIn("observe", helper["description"])
        self.assertIn("add", helper["description"])
        self.assertEqual(helper["parameterChoices"]["boss_mode"], ["add", "observe"])
        restored = agent_api.context(self.root, {})
        self.assertEqual(restored["previouslyProvidedSymbols"], ["SelfTappingScrewPair.bind"])
        self.assertEqual(restored["capabilityVersion"], page["capabilityVersion"])

    def test_cli_queries_all_design_operations_and_pages_without_losing_contracts(self):
        from design_language import OPS
        names = list(OPS)
        self.assertGreater(len(names), 20)
        result = self.cli("capabilities", {"designOps": names})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        page = json.loads(result.stdout)
        self.assertEqual(page["operationNames"], names)
        self.assertIn("selector", page["fieldTypes"])
        self.assertIn("axis", page["fieldTypes"])
        self.assertIsNotNone(page["page"]["nextRequest"])
        path = Path(page["report"]["path"])
        original = path.read_bytes()
        contracts = {}
        while True:
            self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
            for item in page["items"]:
                name = item["field"].rsplit("/", 1)[1]
                self.assertNotIn(name, contracts)
                contracts[name] = item["value"]
            request = page["page"]["nextRequest"]
            if not request:
                break
            page = agent_api.dispatch(self.root, "inspect", request)
        self.assertEqual(contracts, json.loads(original)["operations"])
        self.assertEqual(list(contracts), names)
        self.assertEqual(agent_api.context(self.root, {})["previouslyProvidedDesignOps"], sorted(names))
        self.assertEqual(path.read_bytes(), original)

    def test_symbol_queries_accept_more_than_twenty_names(self):
        names = [f"unknown_symbol_{i}" for i in range(25)]
        page = agent_api.dispatch(self.root, "capabilities", {"symbols": names})
        returned = []
        while True:
            self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
            returned.extend(item["field"].rsplit("/", 1)[1] for item in page["items"])
            request = page["page"]["nextRequest"]
            if not request:
                break
            page = agent_api.dispatch(self.root, "inspect", request)
        self.assertEqual(returned, sorted(names))
        self.assertEqual(agent_api.context(self.root, {})["previouslyProvidedSymbols"], [])

    def test_capability_requests_keep_name_and_byte_limits_without_affecting_other_lists(self):
        for field in ("symbols", "designOps"):
            for invalid in ("box", [None], [""], ["  "], ["x" * 201]):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(agent_api.RequestError) as failure:
                        agent_api.check_arguments("capabilities", {field: invalid})
                    self.assertEqual(failure.exception.details["field"], field)
                    self.assertNotIn("maximumItems", failure.exception.details)
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                agent_api.check_arguments("capabilities", {field: ["box"] * 11_000})
        for operation, args in (
            ("build", {"phase": "draft", "source": "model.py", "dependencies": ["dep.py"] * 21}),
            ("design", {"action": "read", "design": "model.a3d.json", "ids": ["body"] * 21}),
        ):
            with self.assertRaises(agent_api.RequestError) as failure:
                agent_api.check_arguments(operation, args)
            self.assertEqual(failure.exception.details["maximumItems"], 20)

    def test_large_capability_metadata_uses_exact_inspectable_references(self):
        from design_language import capabilities
        payload = capabilities(["box"])
        large = '\"\\\n轴' * 2_000
        payload["fieldTypes"] = {"axis": large}
        payload["operationNames"] = [f"operation_{i}" for i in range(500)]
        with mock.patch("design_language.capabilities", return_value=payload):
            page = agent_api.dispatch(self.root, "capabilities", {"designOps": ["box"]})
        self.assertLessEqual(protocol.wire_size(page), protocol.MAX_WIRE_BYTES)
        self.assertEqual(page["fieldTypes"]["axis"]["field"], "/fieldTypes/axis")
        self.assertEqual(page["operationNames"]["field"], "/operationNames")
        report = page["report"]
        _, document = protocol.read_report(self.root, report["path"], report["sha256"])
        self.assertEqual(protocol.at_pointer(document, page["operationNames"]["field"]), payload["operationNames"])
        request = {"report": report["path"], "sha256": report["sha256"], "field": "/fieldTypes/axis"}
        chunks = []
        while request:
            detail = agent_api.dispatch(self.root, "inspect", request)
            self.assertLessEqual(protocol.wire_size(detail), protocol.MAX_WIRE_BYTES)
            chunks.extend(item["textChunk"] for item in detail["items"])
            request = detail["page"]["nextRequest"]
        self.assertEqual("".join(chunks), large)

    def test_malformed_request_file_returns_json_location(self):
        (self.root / "request.json").write_text('{"changes": [\n }')
        result = self.cli("design", None, "--args-file", "request.json")
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stdout)["error"]
        self.assertEqual(error["type"], "JSONDecodeError")
        self.assertEqual(error["requestFile"], "request.json")
        self.assertEqual(error["line"], 2)
        self.assertGreater(error["offset"], 0)

    def test_legacy_result_and_stale_result_are_explicit(self):
        (self.root / "model_compile-result.json").write_text(json.dumps({
            "schema": "evidence-cad-compile-result/v2", "runId": "old", "status": "failed", "issues": []}))
        context = agent_api.context(self.root, {})
        self.assertTrue(context["latest"]["legacy"])
        self.assertFalse(context["latest"]["inputsCurrent"])
        agent_api.save(self.root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA, "latest": context["latest"]})
        (self.root / "model_compile-result.json").write_text('{}')
        self.assertTrue(agent_api.context(self.root, {})["latest"]["stale"])

    def test_draft_reuse_requires_every_binding_and_does_not_block_failures(self):
        import cad_draft
        source = self.root / "model.py"
        source.write_text("# source")
        artifact = self.root / "preview.png"
        artifact.write_bytes(b"preview")
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            return {"schema": "a3d-draft-result/v2", "runId": str(len(calls)), "status": "draft", "source": protocol.binding(source),
                    "artifacts": {"preview": protocol.binding(artifact)}, "issues": []}
        args = {"phase": "draft", "source": source.name}
        with mock.patch.object(cad_draft, "run_draft", side_effect=run):
            first = agent_api.dispatch(self.root, "build", args)
            self.assertFalse(first["reused"])
            self.assertTrue(agent_api.dispatch(self.root, "build", args)["reused"])
            source.write_text("# changed")
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            artifact.write_bytes(b"changed preview")
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            with mock.patch.object(cad_draft, "run_draft", return_value={"runId": "failed", "status": "failed", "issues": []}):
                source.write_text("# failed")
                self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
                self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
        self.assertEqual(len(calls), 3)

    def test_source_dependencies_track_imported_package_children_and_relative_imports(self):
        source = self.root / "model.py"
        package = self.root / "dimensions"
        package.mkdir()
        (package / "__init__.py").write_text("")
        child = package / "controls.py"
        child.write_text("WIDTH = 10\n")
        source.write_text("from dimensions import controls\n")
        found = agent_api.source_dependencies(self.root, source, [])
        self.assertIn("dependency:dimensions/__init__.py", found)
        self.assertIn("dependency:dimensions/controls.py", found)
        source.write_text("from .dimensions import controls\n")
        (self.root / "__init__.py").write_text("")
        found = agent_api.source_dependencies(self.root, source, [])
        self.assertIn("dependency:dimensions/controls.py", found)

        namespace = self.root / "pkg" / "sub"
        namespace.mkdir(parents=True)
        (namespace.parent / "__init__.py").write_text("")
        (namespace / "controls.py").write_text("WIDTH = 12\n")
        nested_source = namespace / "model.py"
        nested_source.write_text("from . import controls\n")
        found = agent_api.source_dependencies(self.root, nested_source, [])
        self.assertIn("dependency:pkg/__init__.py", found)
        self.assertIn("dependency:pkg/sub/controls.py", found)

        # Namespace packages are valid local imports even without __init__.py.
        (package / "__init__.py").unlink()
        source.write_text("from dimensions import controls\n")
        found = agent_api.source_dependencies(self.root, source, [])
        self.assertIn("dependency:dimensions/controls.py", found)

    def test_declared_import_dependency_is_recursively_scanned(self):
        source = self.root / "model.py"
        source.write_text("import controls\n")
        controls = self.root / "controls.py"
        controls.write_text("import dimensions.controls\n")
        package = self.root / "dimensions"
        package.mkdir()
        (package / "controls.py").write_text("WIDTH = 10\n")
        found = agent_api.source_dependencies(self.root, source, ["controls.py"])
        self.assertIn("dependency:controls.py", found)
        self.assertIn("dependency:dimensions/controls.py", found)

    def test_compile_cache_binds_rich_display_scene_and_object_files(self):
        import cad_compile
        source, intent, semantic_scene = [self.root / name for name in ("build.py", "intent.json", "scene.json")]
        output, display_scene, object_step = [self.root / name for name in ("final.step", "display.scene.json", "object.step")]
        build_report_path = self.root / "build-evidence.json"
        source.write_text("# source")
        intent.write_text(json.dumps({"part": "model"}))
        calls = []

        def compile(_options):
            from intent_revision import audit_lineage, history_path
            calls.append(1)
            history, _ = audit_lineage(self.root, intent, json.loads(intent.read_text()))
            agent_api.save(self.root, history_path(self.root, "model").name, history)
            semantic_scene.write_text("{}")
            output.write_text("STEP")
            object_step.write_text("OBJECT STEP")
            descriptor = {"schema": "a3d-step-display/v1", "step": protocol.binding(output),
                          "objects": [{"step": protocol.binding(object_step)}]}
            display_scene.write_text(json.dumps(descriptor))
            rich_step = {**protocol.binding(output), "scene": protocol.binding(display_scene)}
            agent_api.save(self.root, "build-evidence.json",
                {"schema": "evidence-a3d-build/v2", "artifacts": {"step": rich_step}})
            value = {"schema": cad_compile.RESULT_SCHEMA, "runId": str(len(calls)),
                     "status": "awaiting-visual-review", "pass": True, "visualReviewRequired": True,
                     "inputBindings": {"source": protocol.binding(source)}, "issues": [],
                     "artifacts": {"buildReport": protocol.binding(build_report_path)}, "deliverables": {"step": str(output)}}
            path = agent_api.save(self.root, "model_compile-result.json", value)
            return {"runId": value["runId"], "result": {"path": str(path)}}

        args = {"phase": "compile", "source": source.name, "intent": intent.name, "scene": semantic_scene.name}
        with mock.patch.object(cad_compile, "compile_cad", side_effect=compile):
            first = agent_api.dispatch(self.root, "build", args)
            self.assertFalse(first["reused"])
            outputs = agent_api.state(self.root)["latest"]["outputs"]
            self.assertEqual(outputs["step"]["scene"], protocol.binding(display_scene))
            self.assertEqual(outputs["buildReport"], protocol.binding(build_report_path))
            self.assertTrue(agent_api.dispatch(self.root, "build", args)["reused"])
            self.assertEqual(len(calls), 1)
            build_report_path.write_text(build_report_path.read_text() + " ")
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            object_step.write_text("changed object")
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            object_step.unlink()
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            display_scene.write_text(display_scene.read_text() + " ")
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            display_scene.unlink()
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
        self.assertEqual(len(calls), 6)

    def test_compile_uses_full_saved_result_and_checks_profile_scene_and_outputs(self):
        import cad_compile
        source, intent, profile, scene, output = [self.root / name for name in
                                                 ("build.py", "intent.json", "profile.json", "scene.json", "final.step")]
        source.write_text("# source")
        profile.write_text('{}')
        intent.write_text(json.dumps({"part": "model", "printability": {"profile": {"path": str(profile)}}}))
        calls = []
        preview = self.root / "preview.png"
        def compile(options):
            from intent_revision import audit_lineage, history_path
            calls.append(1)
            history, _ = audit_lineage(self.root, intent, json.loads(intent.read_text()))
            agent_api.save(self.root, history_path(self.root, "model").name, history)
            scene.write_text(json.dumps({"run": len(calls)}))
            output.write_text("STEP")
            preview.write_bytes(b"preview")
            value = {"schema": cad_compile.RESULT_SCHEMA, "runId": str(len(calls)),
                     "status": "awaiting-visual-review", "pass": True, "visualReviewRequired": True,
                     "inputBindings": {"source": protocol.binding(source)}, "issues": [],
                     "artifacts": {"preview": protocol.binding(preview)}, "deliverables": {"step": str(output)}}
            path = agent_api.save(self.root, "model_compile-result.json", value)
            return {"runId": value["runId"], "result": {"path": str(path)}}
        args = {"phase": "compile", "source": source.name, "intent": intent.name, "scene": scene.name}
        with mock.patch.object(cad_compile, "compile_cad", side_effect=compile):
            first = agent_api.dispatch(self.root, "build", args)
            self.assertIn("inputBindings", first)
            self.assertTrue(agent_api.dispatch(self.root, "build", args)["reused"])
            for path in (profile, scene, source):
                path.write_text(path.read_text() + "\n")
                self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
            output.unlink()
            self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
        self.assertEqual(len(calls), 5)

    def test_invalid_arguments_and_concurrent_writes_fail_clearly(self):
        for operation, args in (("build", {"phase": "final", "source": "x"}),
                                ("build", {"phase": "draft", "source": "x", "timeoutSeconds": True}),
                                ("context", {"workspace": ".."}), ("capabilities", {"symbols": [None]})):
            with self.assertRaises(ValueError):
                agent_api.dispatch(self.root, operation, args)
        with agent_api.mutation_lock(self.root):
            with self.assertRaisesRegex(ValueError, "running"):
                agent_api.dispatch(self.root, "capabilities", {})
            self.assertIsNone(agent_api.context(self.root, {})["latest"])
        metrics = [json.loads(line) for line in (self.root / ".a3d-agent/metrics.jsonl").read_text().splitlines()]
        self.assertEqual(len(metrics), 5)
        self.assertTrue(all(item["status"] == "api-error" for item in metrics))
        self.assertTrue(all(item["wireBytes"] is None for item in metrics))
        self.assertTrue(all("arguments" not in item for item in metrics))

    def test_compile_missing_scene_returns_actionable_fields_without_running_source(self):
        result = self.cli("build", {"phase": "compile", "source": "unwritten.py", "intent": "unwritten.json"})
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stdout)["error"]
        self.assertEqual(error["requiredFields"], ["intent", "phase", "scene", "source"])
        self.assertFalse((self.root / ".a3d-agent/runs").exists())

    def test_local_parameter_import_and_declared_data_invalidate_reuse(self):
        import cad_draft
        source = self.root / "model.py"
        source.write_text("from controls import SIZE\n")
        controls = self.root / "controls.py"
        controls.write_text("SIZE = 10\n")
        data = self.root / "dimensions.json"
        data.write_text('{"width":10}')
        artifact = self.root / "preview.png"
        artifact.write_bytes(b"preview")
        args = {"phase": "draft", "source": source.name, "dependencies": [data.name]}
        def run(*unused, **kwargs):
            return {"schema": "a3d-draft-result/v2", "runId": str(time.monotonic_ns()), "status": "draft", "issues": [],
                    "artifacts": {"preview": protocol.binding(artifact)}}
        with mock.patch.object(cad_draft, "run_draft", side_effect=run):
            agent_api.dispatch(self.root, "build", args)
            self.assertTrue(agent_api.dispatch(self.root, "build", args)["reused"])
            for path in (controls, data):
                path.write_text(path.read_text() + "\n")
                self.assertFalse(agent_api.dispatch(self.root, "build", args)["reused"])
                self.assertTrue(agent_api.dispatch(self.root, "build", args)["reused"])

    def test_real_draft_returns_one_bounded_result_and_reuses_bound_preview(self):
        (self.root / "model.py").write_text('from build123d import Box\nfrom build_session import BuildSession\n'
                                             'build = BuildSession(__file__, part_names=("body",))\n'
                                             'build.add("body", Box(10, 10, 10))\nbuild.export()\n')
        first = self.cli("build", {"phase": "draft", "source": "model.py"})
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result["status"], "draft")
        self.assertFalse(result["reused"])
        self.assertEqual(first.stderr, "")
        self.assertLessEqual(protocol.wire_size(result), protocol.MAX_WIRE_BYTES)
        self.assertTrue(Path(result["artifacts"]["preview"]["path"]).is_file())
        second = json.loads(self.cli("build", {"phase": "draft", "source": "model.py"}).stdout)
        self.assertTrue(second["reused"])
        self.assertEqual(second["runId"], result["runId"])
        step_ref = agent_api.state(self.root)["latest"]["outputs"]["step"]
        self.assertIn("scene", step_ref)
        scene_path = Path(step_ref["scene"]["path"])
        scene_document = json.loads(scene_path.read_text())
        object_path = Path(scene_document["objects"][0]["step"]["path"])
        self.assertTrue(object_path.is_file())
        object_path.unlink()
        rebuilt = json.loads(self.cli("build", {"phase": "draft", "source": "model.py"}).stdout)
        self.assertFalse(rebuilt["reused"])
        self.assertNotEqual(rebuilt["runId"], result["runId"])

    def test_real_finishing_failure_inspect_uses_report_sha_and_preserves_report_bytes(self):
        (self.root / "model.py").write_text(
            "from build123d import Box\n"
            "from build_session import BuildSession\n"
            "from cad_helpers import checked_fillet\n"
            "build = BuildSession(__file__, part_names=(\"body\",))\n"
            "build.add(\"body\", Box(10, 10, 10))\n"
            "build.finish(\"body\", lambda p: checked_fillet(p, p.edges(), 100, \"round\", allow_reduce=False))\n"
            "build.export()\n"
        )
        failed = self.cli("build", {"phase": "draft", "source": "model.py"})
        self.assertEqual(failed.returncode, 1, failed.stderr + failed.stdout)
        result = json.loads(failed.stdout)
        issue = next(item for item in result["items"] if item.get("code") == "SOURCE.CHECKED_FILLET_FAILED")
        self.assertIn("id", issue)
        report = result["report"]
        path = Path(report["path"])
        before = path.read_bytes()

        inspected = self.cli("inspect", {
            "report": report["path"], "sha256": report["sha256"],
            "issueId": issue["id"], "field": "/observed",
        })
        self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
        detail = json.loads(inspected.stdout)
        attempts = next(item["value"] for item in detail["items"] if item["field"].endswith("/attempts"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_real_compile_keeps_full_evidence_and_binds_string_deliverables(self):
        examples = ROOT / "skills" / "a3d-text" / "examples"
        for name in ("simple_brep_build.py", "simple_brep_intent.py"):
            shutil.copyfile(examples / name, self.root / name)
        shutil.copyfile(examples / "bambu-a1-mini-0.4-standard.example.json", self.root / "simple_brep_printer-profile.json")
        generated = subprocess.run([sys.executable, "-B", "simple_brep_intent.py"], cwd=self.root,
                                   env={**os.environ, "AMAGINE3D_RUNTIME_DIR": str(ROOT / "skills" / "a3d-public")},
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        args = {"phase": "compile", "source": "simple_brep_build.py", "intent": "simple_brep_intent.json",
                "scene": "simple_brep_scene.json"}
        result = self.cli("build", args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        page = json.loads(result.stdout)
        self.assertTrue(page["pass"])
        self.assertTrue(page["visualReviewRequired"])
        saved = json.loads(Path(page["report"]["path"]).read_text())
        self.assertTrue(saved["inputBindings"]["source"]["sha256"])
        current = agent_api.state(self.root)["latest"]
        for key, path in saved["deliverables"].items():
            self.assertEqual({field: current["outputs"][key][field] for field in ("path", "sha256")},
                             protocol.binding(Path(path)))
        repeated = json.loads(self.cli("build", args).stdout)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["runId"], page["runId"])

    def test_real_design_compile_reads_unicode_intent_under_ascii_locale(self):
        examples = ROOT / "skills" / "a3d-text" / "examples"
        shutil.copyfile(examples / "simple_brep_intent.py", self.root / "simple_brep_intent.py")
        shutil.copyfile(examples / "bambu-a1-mini-0.4-standard.example.json", self.root / "simple_brep_printer-profile.json")
        generated = subprocess.run([sys.executable, "-B", "simple_brep_intent.py"], cwd=self.root,
            env={**os.environ, "AMAGINE3D_RUNTIME_DIR": str(ROOT / "skills/a3d-public")}, capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        intent_path = self.root / "simple_brep_intent.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent["assumptions"].append("中文说明：独立尺寸目标保持不变。")
        intent_path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
        design = {"schema": "a3d-design/v1", "units": {"length": "mm", "angle": "deg"}, "params": {},
            "frames": {"top": {"origin": [0, 0, 10], "u": [1, 0, 0], "normal": [0, 0, 1]}},
            "parts": {"simple-brep": {"result": "@simple-brep.envelope", "features": [
                {"id": "blank", "op": "box", "size": [40, 28, 10], "align": ["center", "center", "min"]},
                {"id": "round", "op": "fillet", "body": "@simple-brep.blank", "radius": 2,
                 "edges": {"of": "@simple-brep.blank", "where": {"curve": "line", "parallel": [0, 0, 1]}, "expect": {"count": 4}}},
                {"id": "pocket", "op": "hole", "body": "@simple-brep.round", "radius": 6, "requirement": "pocket",
                 "frame": "@frames.top", "direction": "-normal", "depth": {"mode": "blind", "length": 6}},
                {"id": "envelope", "op": "translate", "input": "@simple-brep.pocket", "offset": [0, 0, 0], "requirement": "body"}]}}}
        path = self.root / "model.a3d.json"
        path.write_text(json.dumps(design))
        args = {"phase": "compile", "design": path.name, "revision": sha256(path.read_bytes()).hexdigest(), "intent": intent_path.name}
        result = subprocess.run(["node", str(ROOT / "bin/a3d.mjs"), "agent", "build", json.dumps(args)], cwd=self.root,
            env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8") + result.stderr.decode("utf-8"))
        self.assertTrue(json.loads(result.stdout)["pass"])

    @unittest.skipUnless(os.name == "posix", "process group cancellation test")
    def test_cancelled_cli_reaps_source_and_context_recovers_interruption(self):
        source = self.root / "slow.py"
        source.write_text('import os, time\nfrom pathlib import Path\nPath("source-pid").write_text(str(os.getpid()))\ntime.sleep(60)\n')
        process = subprocess.Popen(["node", str(ROOT / "bin" / "a3d.mjs"), "agent", "build",
                                    json.dumps({"phase": "draft", "source": source.name})], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "source-pid").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / "source-pid").exists())
            pid = int((self.root / "source-pid").read_text())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            context = agent_api.context(self.root, {})
            self.assertEqual(context["activeAttempt"]["status"], "interrupted")
            self.assertIsNone(context["latest"])
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)


    def test_mixed_preview_batch_reuses_loaded_step_and_inspects_bound_manifest(self):
        source = self.root / "model.py"
        source.write_text('from build123d import Box\nfrom build_session import BuildSession\n'
                          'build = BuildSession(__file__, part_names=("body",))\n'
                          'build.add("body", Box(10, 10, 10))\nbuild.export()\n')
        build_process = self.cli("build", {"phase": "draft", "source": source.name})
        self.assertEqual(build_process.returncode, 0, build_process.stderr + build_process.stdout)
        built = json.loads(build_process.stdout)
        self.assertEqual(built["status"], "draft")
        parent_ref, parent = protocol.read_report(self.root, built["report"]["path"], built["report"]["sha256"])
        latest_before = agent_api.state(self.root)["latest"]
        requests = [
            {"kind": "camera", "id": "oblique", "direction": [1, -2, 1], "up": [0, 0, 1],
             "target": [0, 0, 0], "scope": {"min": [-2, -2, -2], "max": [2, 2, 2]}, "size": 320},
            {"kind": "section", "id": "mid-plane",
             "plane": {"origin": [0, 0, 0], "normal": [0, 0, 1], "u": [1, 0, 0]}},
        ]
        preview_process = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(preview_process.returncode, 0, preview_process.stderr + preview_process.stdout)
        result = json.loads(preview_process.stdout)
        self.assertEqual(result["schema"], "a3d-agent-preview/v1")
        self.assertLessEqual(protocol.wire_size(result), protocol.MAX_WIRE_BYTES)
        self.assertEqual(len(result["observations"]), 2)
        self.assertTrue(all(Path(item["image"]["path"]).is_file() for item in result["observations"]))
        self.assertEqual(result["observations"][1]["kind"], "section")
        self.assertEqual(result["observations"][0]["cameraScope"], requests[0]["scope"])
        self.assertEqual(result["observations"][1]["measurementSummary"]["status"], "material")
        self.assertEqual(agent_api.state(self.root)["latest"], latest_before)

        manifest_process = self.cli("inspect", {"report": result["report"]["path"],
            "sha256": result["report"]["sha256"], "field": "/observations"})
        self.assertEqual(manifest_process.returncode, 0, manifest_process.stderr + manifest_process.stdout)
        manifest = json.loads(manifest_process.stdout)
        self.assertEqual(manifest["page"]["total"], 2)
        self.assertEqual({item["value"]["kind"] for item in manifest["items"]}, {"camera", "section"})
        section_value = next(item["value"] for item in manifest["items"] if item["value"]["kind"] == "section")
        self.assertEqual(section_value["measurement"]["preview"], section_value["image"])
        self.assertEqual(section_value["measurement"]["preview"]["sha256"],
                         sha256(Path(section_value["image"]["path"]).read_bytes()).hexdigest())
        cached_process = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(cached_process.returncode, 0, cached_process.stderr + cached_process.stdout)
        cached = json.loads(cached_process.stdout)
        self.assertTrue(cached["cached"])
        section_process = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": [requests[1]]})
        self.assertEqual(section_process.returncode, 0, section_process.stderr + section_process.stdout)
        section_only = json.loads(section_process.stdout)
        self.assertEqual(section_only["observations"][0]["kind"], "section")

        run = next(entry for entry in agent_api.state(self.root)["runs"] if entry["report"] == parent_ref)
        step_ref = next(value for key, value in run["outputs"].items() if key.startswith("step"))
        step = Path(step_ref["path"])
        decoy_step = self.root / "later.step"
        decoy_step.write_bytes(step.read_bytes())
        current = agent_api.state(self.root)
        other_report_path = self.root / ".a3d-agent" / "runs" / "other.json"
        other_report_path.parent.mkdir(parents=True, exist_ok=True)
        other_report_path.write_text(json.dumps({"schema": "a3d-draft-result/v2", "runId": "other-run",
            "status": "draft", "taskInputBindings": run["inputs"], "artifacts": {}}))
        current["latest"] = {"operation": "draft", "report": protocol.binding(other_report_path), "runId": "other-run",
            "inputs": run["inputs"], "outputs": {"step:assembly": protocol.binding(decoy_step)}}
        agent_api.save(self.root, ".a3d-agent/state.json", current)
        historical_process = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(historical_process.returncode, 0, historical_process.stderr + historical_process.stdout)
        historical = json.loads(historical_process.stdout)
        self.assertEqual(historical["sourceArtifact"]["binding"], step_ref)
        self.assertTrue(historical["cached"])
        self.assertTrue(historical["currentness"]["historical"])
        self.assertFalse(historical["currentness"]["latestRunMatches"])
        historical_inspect_process = self.cli("inspect", {"report": result["report"]["path"],
            "sha256": result["report"]["sha256"], "field": "/observations"})
        self.assertEqual(historical_inspect_process.returncode, 0,
                         historical_inspect_process.stderr + historical_inspect_process.stdout)
        historical_inspect = json.loads(historical_inspect_process.stdout)
        self.assertTrue(historical_inspect["previewCurrentness"]["historical"])

        image = Path(result["observations"][0]["image"]["path"])
        image.write_bytes(b"damaged cached image")
        damaged_inspect = self.cli("inspect", {"report": result["report"]["path"],
            "sha256": result["report"]["sha256"], "field": "/observations"})
        self.assertEqual(damaged_inspect.returncode, 0, damaged_inspect.stderr + damaged_inspect.stdout)
        damaged_page = json.loads(damaged_inspect.stdout)
        damaged_observation = next(item["value"] for item in damaged_page["items"]
                                   if item["field"] == "/observations/0")
        self.assertNotIn("image", damaged_observation)
        self.assertEqual(damaged_page["previewCurrentness"]["status"], "stale")
        self.assertFalse(damaged_page["previewCurrentness"]["images"][0]["current"])
        image.unlink()
        missing_inspect = self.cli("inspect", {"report": result["report"]["path"],
            "sha256": result["report"]["sha256"], "field": "/observations"})
        missing_page = json.loads(missing_inspect.stdout)
        self.assertNotIn("image", next(item["value"] for item in missing_page["items"]
                                        if item["field"] == "/observations/0"))
        self.assertEqual(missing_page["previewCurrentness"]["status"], "stale")
        recomputed_process = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(recomputed_process.returncode, 0, recomputed_process.stderr + recomputed_process.stdout)
        recomputed = json.loads(recomputed_process.stdout)
        self.assertFalse(recomputed["cached"])
        self.assertTrue(Path(recomputed["observations"][0]["image"]["path"]).is_file())
        manifest_doc = json.loads(Path(recomputed["report"]["path"]).read_text())
        manifest_doc["observations"][0]["scope"] = "tampered manifest"
        Path(recomputed["report"]["path"]).write_text(json.dumps(manifest_doc))
        changed_manifest = protocol.binding(Path(recomputed["report"]["path"]))
        tampered_manifest = agent_api.dispatch(self.root, "inspect", {"report": changed_manifest["path"],
            "sha256": changed_manifest["sha256"], "field": "/observations"})
        tampered_camera = next(item["value"] for item in tampered_manifest["items"]
                               if item["field"] == "/observations/0")
        self.assertNotIn("image", tampered_camera)
        self.assertEqual(tampered_manifest["previewCurrentness"]["status"], "stale")

        old_step = step.read_bytes()
        step.write_bytes(step.read_bytes() + b"stale")
        stale = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(stale.returncode, 2)
        self.assertIn("source STEP", stale.stdout + stale.stderr)
        step.write_bytes(old_step)
        source.write_text(source.read_text() + "# input expired\n")
        expired = self.cli("preview", {"report": parent_ref["path"],
            "sha256": parent_ref["sha256"], "requests": requests})
        self.assertEqual(expired.returncode, 2)
        self.assertIn("inputs are expired", expired.stdout + expired.stderr)

    def test_rotated_asymmetric_brep_changes_camera_and_section_observations(self):
        """Exercise real STEP loading, camera rendering, and BRep sections across rotation."""
        from build123d import Box, Compound, Pos, Rot
        from display_step import export_display_step

        results = []
        for label, rotation in (("base", 0), ("rotated", 90)):
            root = self.root / label
            root.mkdir()
            source = root / "model.py"
            source.write_text(f"# asymmetric semantic fixture rotated {rotation} degrees\n")
            footprint = Box(12, 2, 2) + Pos(5, 3, 0) * Box(2, 6, 2)
            shape = Rot(0, 0, rotation) * footprint
            step = root / "assembly.step"
            exported = export_display_step([("asymmetric-body", shape, {"baseColor": "#336699"})], step)
            step_ref = protocol.binding(step)
            step_ref["scene"] = exported["scene"]
            source_ref = protocol.binding(source)
            inputs = {"source": source_ref}
            report_path = root / "parent.json"
            report = {"schema": "a3d-draft-result/v2", "runId": label, "status": "draft",
                      "source": source_ref, "taskInputBindings": inputs,
                      "artifacts": {"step:display": step_ref}, "issues": []}
            report_path.write_text(json.dumps(report))
            report_ref = protocol.binding(report_path)
            run = {"operation": "draft", "report": report_ref, "runId": label,
                   "inputs": inputs, "outputs": report["artifacts"]}
            agent_api.save(root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA,
                "latest": run, "runs": [run]})
            preview = agent_api.dispatch(root, "preview", {"report": report_ref["path"],
                "sha256": report_ref["sha256"], "requests": [
                    {"kind": "camera", "view": "top", "size": 320},
                    {"kind": "section", "id": "xy-mid", "plane": {
                        "origin": [0, 0, 0], "normal": [0, 0, 1], "u": [1, 0, 0]}}]})
            self.assertEqual([entry["kind"] for entry in preview["observations"]], ["camera", "section"])
            self.assertTrue(all(Path(entry["image"]["path"]).is_file() for entry in preview["observations"]))
            self.assertEqual(preview["observations"][1]["measurementSummary"]["status"], "material")
            results.append(preview)

        base_camera = results[0]["observations"][0]["image"]
        rotated_camera = results[1]["observations"][0]["image"]
        self.assertNotEqual(base_camera["sha256"], rotated_camera["sha256"])
        base_section = results[0]["observations"][1]["measurementSummary"]["outer_envelope"]
        rotated_section = results[1]["observations"][1]["measurementSummary"]["outer_envelope"]
        self.assertAlmostEqual(base_section["width_u_mm"], 12, delta=0.2)
        self.assertAlmostEqual(base_section["depth_v_mm"], 7, delta=0.2)
        self.assertAlmostEqual(rotated_section["width_u_mm"], 7, delta=0.2)
        self.assertAlmostEqual(rotated_section["depth_v_mm"], 12, delta=0.2)
        # Expose a real generated image path for independent visual inspection.
        self.test_preview_image_paths = [base_camera["path"], rotated_camera["path"]]

    def test_preview_batch_loads_once_and_only_tessellates_camera_objects(self):
        from types import SimpleNamespace
        from PIL import Image, ImageDraw
        import trimesh
        import agent_preview
        import display_step
        import cpu_z_buffer
        from PIL import ImageFont

        test_image = Image.new("RGB", (320, 320), "white")
        test_draw = ImageDraw.Draw(test_image)
        test_font = ImageFont.load_default()
        measure = lambda value: test_draw.textbbox((0, 0), value, font=test_font)[2]
        scope_label = agent_preview._scope_label("camera", ["long-object-name-" * 40], ["hidden"] * 12, 320, measure)
        self.assertIn("hidden 12", scope_label)
        self.assertLessEqual(measure(scope_label), 308)

        step = self.root / "fake.step"
        step.write_text("fake STEP payload")
        source = self.root / "model.py"
        source.write_text("# source")
        report_path = self.root / "parent.json"
        source_ref = protocol.binding(source)
        step_ref = protocol.binding(step)
        report_path.write_text(json.dumps({"schema": "a3d-draft-result/v2", "runId": "mock-run",
            "status": "draft", "source": source_ref, "taskInputBindings": {"source": source_ref},
            "artifacts": {"step": step_ref}}))
        parent_ref = protocol.binding(report_path)
        shape = object()
        loaded = [({"name": "body", "appearance": {"baseColor": "#7AA3C7"}}, shape)]
        mesh = trimesh.creation.box()
        mesh_rows = [("body", mesh, {"baseColor": "#7AA3C7"})]

        def fake_section(_shape, _plane, *, label, preview):
            Image.new("RGB", (32, 32), "white").save(preview)
            return {"label": label, "status": "material", "outer_envelope": [1, 1],
                    "material_island_count": 1, "hole_count": 0, "sum_material_area_mm2": 1.0}

        request = [{"kind": "camera", "direction": [1, -2, 1], "up": [0, 0, 1], "size": 320},
                   {"kind": "section", "plane": {"origin": [0, 0, 0],
                    "normal": [0, 0, 1], "u": [1, 0, 0]}}]
        stats = SimpleNamespace(triangles_input=len(mesh.faces), covered_pixels=100, elapsed_seconds=0.01)
        fake_render = SimpleNamespace(image=Image.new("RGB", (320, 320), "white"), stats=stats)
        with mock.patch.object(display_step, "load_step_objects", return_value=(loaded, {})) as load_spy, \
             mock.patch.object(display_step, "mesh_step_objects", return_value=(mesh_rows, {})) as mesh_spy, \
             mock.patch.object(cpu_z_buffer, "render_view", return_value=fake_render), \
             mock.patch("brep_measurements.measure_section", side_effect=fake_section):
            result = agent_api.dispatch(self.root, "preview", {"report": parent_ref["path"],
                "sha256": parent_ref["sha256"], "requests": request})
        self.assertEqual(load_spy.call_count, 1)
        self.assertEqual(mesh_spy.call_count, 1)
        self.assertEqual(len(result["observations"]), 2)

        section_only = [{"kind": "section", "plane": request[1]["plane"]}]
        with mock.patch.object(display_step, "load_step_objects", return_value=(loaded, {})) as load_spy, \
             mock.patch.object(display_step, "mesh_step_objects", return_value=([], {})) as mesh_spy, \
             mock.patch("brep_measurements.measure_section", side_effect=fake_section):
            result = agent_api.dispatch(self.root, "preview", {"report": parent_ref["path"],
                "sha256": parent_ref["sha256"], "requests": section_only})
        self.assertEqual(load_spy.call_count, 1)
        mesh_spy.assert_not_called()
        self.assertEqual(result["observations"][0]["kind"], "section")

        import agent_preview
        huge = {"kind": "camera", "direction": [1.79e308, 1.79e308, 1.79e308],
                "up": [-1.79e308, 1.79e308, 0], "size": 320}
        normalized = agent_preview._normalize_request(huge, 0)
        self.assertTrue(all(math.isfinite(component) for component in normalized["direction"] + normalized["up"]))
        self.assertAlmostEqual(sum(component * component for component in normalized["direction"]), 1.0)
        with self.assertRaisesRegex(ValueError, "nonzero"):
            agent_preview._normalize_request({**huge, "direction": [0, 0, 0]}, 0)
        with self.assertRaisesRegex(ValueError, "parallel"):
            agent_preview._normalize_request({**huge, "direction": [0, 0, 1], "up": [0, 0, 1]}, 0)

        oversized_batch = [{"kind": "camera", "view": "front", "size": 1280} for _ in range(4)]
        with mock.patch.object(display_step, "load_step_objects") as load_spy:
            with self.assertRaisesRegex(ValueError, "image budget"):
                agent_api.dispatch(self.root, "preview", {"report": parent_ref["path"],
                    "sha256": parent_ref["sha256"], "requests": oversized_batch})
        load_spy.assert_not_called()

        with mock.patch.object(display_step, "load_step_objects", return_value=(loaded, {})), \
             mock.patch.object(display_step, "mesh_step_objects", return_value=(mesh_rows, {})), \
             mock.patch.object(agent_preview, "triangle_count", return_value=agent_preview.DEFAULT_MAX_TRIANGLES + 1), \
             mock.patch.object(cpu_z_buffer, "render_view") as render_spy:
            with self.assertRaisesRegex(ValueError, "triangles"):
                agent_api.dispatch(self.root, "preview", {"report": parent_ref["path"],
                    "sha256": parent_ref["sha256"], "requests": [{"kind": "camera", "view": "side", "size": 320}]})
        render_spy.assert_not_called()

    def test_preview_rejects_tampered_scene_object_binding_before_step_load(self):
        import display_step

        step = self.root / "assembly.step"
        step.write_text("bound STEP")
        object_step = self.root / "body.step"
        object_step.write_text("bound object")
        source = self.root / "model.py"
        source.write_text("# source")
        source_ref = protocol.binding(source)
        step_ref = protocol.binding(step)
        scene = display_step.scene_path(step)
        scene.write_text(json.dumps({"schema": display_step.SCHEMA, "step": step_ref,
            "objects": [{"name": "body", "step": protocol.binding(object_step)}]}))
        report_path = self.root / "parent.json"
        report_path.write_text(json.dumps({"schema": "a3d-draft-result/v2", "runId": "scene-run",
            "status": "draft", "source": source_ref, "taskInputBindings": {"source": source_ref},
            "artifacts": {"step": step_ref}}))
        report_ref = protocol.binding(report_path)
        object_step.write_text("tampered object")
        with mock.patch.object(display_step, "load_step_objects") as load_spy:
            with self.assertRaisesRegex(ValueError, "scene or object binding is stale"):
                agent_api.dispatch(self.root, "preview", {"report": report_ref["path"],
                    "sha256": report_ref["sha256"], "requests": [{"kind": "camera", "view": "front"}]})
        load_spy.assert_not_called()

    def test_preview_cache_retention_is_bounded_and_only_prunes_owned_files(self):
        import agent_preview

        previews = self.root / ".a3d-agent" / "previews"
        cache = previews / "cache"
        previews.mkdir(parents=True, exist_ok=True)
        unrelated = previews / "notes.json"
        unrelated.write_text("user-owned")
        unowned_key = "f" * 64
        (previews / f"{unowned_key}.json").write_text(json.dumps({"schema": "user-data"}))
        (cache / unowned_key).mkdir(parents=True, exist_ok=True)
        (cache / unowned_key / "keep.txt").write_text("user-owned")
        parent = {"path": "parent.json", "sha256": "a" * 64}
        with mock.patch.object(agent_preview, "MAX_CACHE_BYTES", 1_000_000):
            for index in range(33):
                key = f"{index + 1:064x}"
                manifest_path = previews / f"{key}.json"
                cache_path = cache / key
                cache_path.mkdir(parents=True, exist_ok=True)
                image = cache_path / "observation-00.png"
                image.write_bytes(b"x" * 64)
                manifest = {"schema": agent_preview.MANIFEST_SCHEMA, "cacheKey": key, "observations": [
                    {"image": {"path": str(image.resolve()), "sha256": "b" * 64}}]}
                manifest["manifestSha256"] = agent_preview.sha256(agent_preview.encode(manifest).encode()).hexdigest()
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest))
                agent_preview._register_manifest(self.root, manifest_path, parent)

        registrations = agent_api.state(self.root)["visualPreviews"]
        self.assertEqual(len(registrations), agent_preview.MAX_REGISTERED_MANIFESTS)
        self.assertFalse((previews / f"{1:064x}.json").exists())
        self.assertFalse((cache / f"{1:064x}").exists())
        self.assertTrue((previews / f"{33:064x}.json").exists())
        self.assertEqual(unrelated.read_text(), "user-owned")
        self.assertTrue((previews / f"{unowned_key}.json").exists())
        self.assertEqual((cache / unowned_key / "keep.txt").read_text(), "user-owned")

    def test_deep_workspace_single_preview_keeps_full_direct_image_binding(self):
        from build123d import Box, export_step

        root = self.root
        for _ in range(4):
            root = root / ("d" * 80)
        root.mkdir(parents=True)
        source = root / "model.py"
        source.write_text("# bound input\n")
        step = root / "part.step"
        self.assertTrue(export_step(Box(8, 5, 3), step))
        report_path = root / "parent.json"
        inputs = {"source": protocol.binding(source)}
        report = {"schema": "a3d-draft-result/v2", "runId": "deep-path-run", "status": "draft",
                  "source": inputs["source"], "taskInputBindings": inputs,
                  "artifacts": {"step": protocol.binding(step)}, "issues": []}
        report_path.write_text(json.dumps(report))
        report_ref = protocol.binding(report_path)
        run = {"operation": "draft", "report": report_ref, "runId": report["runId"],
               "inputs": inputs, "outputs": report["artifacts"]}
        agent_api.save(root, ".a3d-agent/state.json", {"schema": protocol.SCHEMA,
            "latest": run, "runs": [run]})

        result = agent_api.dispatch(root, "preview", {"report": report_ref["path"],
            "sha256": report_ref["sha256"], "requests": [{"kind": "camera", "view": "front", "size": 320}]})
        image = result["observations"][0]["image"]
        self.assertGreater(len(image["path"].encode()), 300)
        self.assertTrue(Path(image["path"]).is_file())
        self.assertEqual(image["sha256"], sha256(Path(image["path"]).read_bytes()).hexdigest())
        self.assertLessEqual(protocol.wire_size(result), protocol.MAX_WIRE_BYTES)

if __name__ == "__main__":
    unittest.main()
