"""Structured diagnostics shared by CAD authoring and compiler subprocesses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
import os
from pathlib import Path
from uuid import uuid4
from typing import Any


SOURCE_DIAGNOSTICS_SCHEMA = "evidence-cad-source-diagnostics/v1"


_DIAGNOSTIC_GUIDES = "references/error-diagnostics.md"
_GENERIC_HINT = ("Collect the complete current report, failed stage and available logs; "
                 "record missing evidence without assigning an unverified cause. Read "
                 "$AMAGINE3D_RUNTIME_DIR/references/error-diagnostics.md#triage.")


def repair_hint_for_issue(issue: Mapping[str, Any]) -> str | None:
    """Return a guide route for a known structured issue, without guessing causes."""
    code = issue.get("code")
    if not isinstance(code, str):
        return None
    observed = issue.get("observed")
    if not isinstance(observed, Mapping):
        observed = {}
    actual = issue.get("actual")
    if not isinstance(actual, Mapping):
        actual = {}

    route = None
    if code in {"SOURCE.CUT_MISSED_OWNER", "SOURCE.CHECKED_CUT_FAILED",
                "SOURCE.UNION_DISCONNECTED", "SOURCE.CHECKED_UNION_FAILED"}:
        section = {
            "SOURCE.CUT_MISSED_OWNER": "cut-missed-its-owner",
            "SOURCE.CHECKED_CUT_FAILED": "boolean-execution-failed",
            "SOURCE.UNION_DISCONNECTED": "union-disconnected",
            "SOURCE.CHECKED_UNION_FAILED": "boolean-execution-failed",
        }[code]
        route = f"references/operations/booleans.md#{section}"
    elif code in {"SOURCE.CHECKED_FILLET_FAILED", "SOURCE.CHECKED_CHAMFER_FAILED"}:
        selected = observed.get("selectedEdgeCount")
        if type(selected) is int and selected == 0:
            route = "references/operations/finishing.md#python-selector-found-no-edges"
        elif isinstance(observed.get("attempts"), list) and observed["attempts"]:
            route = "references/operations/finishing.md#python-finishing-execution-failed"
    elif code in {"DESIGN.SELECTION_EMPTY", "DESIGN.SELECTION_COUNT"}:
        route = "references/operations/finishing.md#dsl-selection-or-finishing-failure"
    elif code in {"DESIGN.KERNEL_FAILED", "DESIGN.POSTCONDITION_FAILED"}:
        op = issue.get("op")
        if not isinstance(op, str) and isinstance(issue.get("details"), Mapping):
            op = issue["details"].get("op")
        check = issue.get("check")
        if not isinstance(op, str) and isinstance(check, str) and check.lower() in {"fillet", "chamfer", "cut", "union"}:
            op = check.lower()
        if isinstance(op, str) and op.lower() in {"fillet", "chamfer"}:
            route = "references/operations/finishing.md#dsl-selection-or-finishing-failure"
        elif isinstance(op, str) and op.lower() in {"cut", "subtract", "difference"}:
            route = "references/operations/booleans.md#boolean-execution-failed"
        elif isinstance(op, str) and op.lower() in {"union", "add"}:
            route = "references/operations/booleans.md#boolean-execution-failed"
        else:
            route = _DIAGNOSTIC_GUIDES + "#timeouts-and-unknown-errors"
    elif code == "AUTHORING.INVALID" or code.startswith("DESIGN.BINDING_") or code.startswith("DESIGN.OWNERSHIP"):
        route = _DIAGNOSTIC_GUIDES + "#declaration-and-binding-errors"
    elif code == "DESIGN.REVISION_CONFLICT":
        route = _DIAGNOSTIC_GUIDES + "#revision-and-stale-evidence"
    elif code == "SOURCE.PREFLIGHT_FAILED":
        route = _DIAGNOSTIC_GUIDES + "#declaration-and-binding-errors"
    elif code.startswith(("CAD.", "DISPLAY.", "VISUAL.")):
        route = _DIAGNOSTIC_GUIDES + "#cad-and-display-failures"
    elif code in {"DRAFT.TIMEOUT", "DRAFT.INCOMPLETE"}:
        route = _DIAGNOSTIC_GUIDES + "#timeouts-and-unknown-errors"

    if route is None:
        return None
    actions = {
        "SOURCE.CUT_MISSED_OWNER": "Compare owner and cutter frames, then inspect removed-volume and boolean-witness evidence.",
        "SOURCE.CHECKED_CUT_FAILED": "Validate owner/tool inputs and inspect the operation log before isolating the cut.",
        "SOURCE.UNION_DISCONNECTED": "Inspect component count and witness gap/nearest-point evidence against the intended owner.",
        "SOURCE.CHECKED_UNION_FAILED": "Validate body/addition inputs and inspect the operation log before isolating the union.",
        "SOURCE.CHECKED_FILLET_FAILED": "Check selector and selected-edge count first; do not change the requested radius to silence failure.",
        "SOURCE.CHECKED_CHAMFER_FAILED": "Check selector and selected-edge count first; do not change the requested length to silence failure.",
        "DESIGN.SELECTION_EMPTY": "Read the current node, edges.where/edges.of and candidate evidence; preserve the requested size.",
        "DESIGN.SELECTION_COUNT": "Compare actual.count, expected and current node candidates; do not change the expected count merely to pass.",
    }
    action = actions.get(code)
    if code in {"SOURCE.CHECKED_FILLET_FAILED", "SOURCE.CHECKED_CHAMFER_FAILED"}:
        selected, attempts = observed.get("selectedEdgeCount"), observed.get("attempts")
        if type(selected) is int and selected == 0:
            action = "Enumerate edge candidates and verify the source selector; do not change the requested radius or length."
        elif isinstance(attempts, list) and attempts:
            action = "Inspect attempts and selected local topology; explore parameters only within unchanged requirements."
        else:
            return None
    if code in {"DESIGN.KERNEL_FAILED", "DESIGN.POSTCONDITION_FAILED"}:
        action = "Read node, op, field and concrete source issues; do not infer a kernel cause from the wrapper."
    elif code == "DESIGN.SELECTION_COUNT" and type(actual.get("count")) is int:
        action = f"Compare actual.count={actual['count']} with expected and current node candidates; do not change expected merely to pass."
    if code.startswith("AUTHORING.") or code.startswith("DESIGN.BINDING_") or code.startswith("DESIGN.OWNERSHIP"):
        action = "Repair the Agent-authored source or declaration using ruleId, field, owner and actual/expected values."
        rule_id = issue.get("ruleId")
        if isinstance(rule_id, str) and rule_id:
            action += " Start with ruleId " + rule_id + "."
    elif code == "DESIGN.REVISION_CONFLICT":
        action = "Read the current revision and affected nodes, then apply the edit against that revision."
    elif code == "SOURCE.PREFLIGHT_FAILED":
        action = "Repair the Agent-authored source or declaration at the named field, preserving its rule and target."
    elif code.startswith(("CAD.", "DISPLAY.", "VISUAL.")):
        stage = issue.get("stage")
        if isinstance(stage, str) and stage in {"cadExport", "tessellation", "display", "preview"}:
            action = "Inspect the bound CAD export and input artifacts before retrying this display stage."
            if stage == "cadExport":
                action = "Inspect the bound CAD export and source evidence; do not treat export success as manufacturing acceptance."
            elif stage == "preview":
                action = "Inspect preview bindings; reuse passed manufacturing checks only when their current bindings verify."
        else:
            action = "Inspect the bound stage and source evidence; use its recovery instructions without inferring acceptance."
    elif code in {"DRAFT.TIMEOUT", "DRAFT.INCOMPLETE"}:
        action = "Read the current attempt, failed stage and logs; preserve input bindings."
    return (action or "Inspect the reported structured evidence and verify the next check.") + " Read $AMAGINE3D_RUNTIME_DIR/" + route


def api_error_repair_hint(error: Any) -> str:
    """Route request failures without requiring an issue or CAD report."""
    error_type = error.get("type") if isinstance(error, Mapping) else None
    if error_type in {"JSONDecodeError", "RequestError"}:
        anchor = "#request-errors"
    else:
        anchor = "#timeouts-and-unknown-errors"
    return "Read $AMAGINE3D_RUNTIME_DIR/" + _DIAGNOSTIC_GUIDES + anchor + "; use the returned request fields and logs."


def generic_diagnostic_hint() -> str:
    return _GENERIC_HINT


def add_repair_hint(issue: Mapping[str, Any], fallback: str | None = None) -> dict[str, Any]:
    """Copy an issue and add a route only when its producer supplied no hint."""
    result = dict(issue)
    existing = result.get("repairHint")
    if isinstance(existing, str) and existing.strip():
        return result
    hint = repair_hint_for_issue(result)
    if hint is not None:
        prefix = fallback.rstrip() + " " if fallback and fallback != _GENERIC_HINT else ""
        result["repairHint"] = prefix + hint
    elif fallback:
        result["repairHint"] = fallback.rstrip() if _GENERIC_HINT in fallback else fallback.rstrip() + " " + _GENERIC_HINT
    return result


class ContractIssue(str):
    """A contract error message that keeps its structured problem identity.

    Contract validators return text that existing callers join, match, and
    persist as plain strings. Carrying the rule and its location on that text
    keeps the existing contract while delivering identity from the producer all
    the way to the compile report and the repair state.
    """

    def __new__(
        cls,
        message: str,
        *,
        rule_id: str,
        field: str | None = None,
        subject_id: str | None = None,
        interface_id: str | None = None,
        feature_id: str | None = None,
        actual: Any = None,
        expected: Any = None,
        repair_hint: str | None = None,
    ) -> "ContractIssue":
        instance = super().__new__(cls, message)
        instance.rule_id = rule_id
        instance.field = field
        instance.subject_id = subject_id
        instance.interface_id = interface_id
        instance.feature_id = feature_id
        instance.actual = actual
        instance.expected = expected
        instance.repair_hint = repair_hint
        return instance

    def to_issue(self) -> dict[str, Any]:
        issue: dict[str, Any] = {
            "code": "AUTHORING.INVALID",
            "message": str(self),
            "ruleId": self.rule_id,
        }
        for key, value in (
            ("field", self.field),
            ("subjectId", self.subject_id),
            ("interfaceId", self.interface_id),
            ("featureId", self.feature_id),
            ("actual", self.actual),
            ("expected", self.expected),
            ("repairHint", self.repair_hint),
        ):
            if value is not None:
                issue[key] = value
        return issue


def contract_issue(error: Any) -> dict[str, Any] | None:
    """Return a validator error's structured identity, when it carries one."""

    return error.to_issue() if isinstance(error, ContractIssue) else None


def write_source_diagnostics(payload: Mapping[str, Any]) -> None:
    """Keep diagnostics outside truncated tool output, bound to one managed run."""
    destination = os.environ.get("AMAGINE3D_SOURCE_DIAGNOSTICS_PATH")
    phase = os.environ.get("AMAGINE3D_SOURCE_PHASE")
    run_id = os.environ.get("AMAGINE3D_DRAFT_RUN_ID" if phase == "draft" else "AMAGINE3D_COMPILE_RUN_ID")
    if phase not in {"compile", "draft"} or not destination or not run_id:
        return
    path = Path(destination)
    prior = {}
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("runId") == run_id:
                prior = value
        except (ValueError, OSError):
            pass
    issues = []
    seen = set()
    for issue in [*prior.get("issues", []), *payload.get("issues", [])]:
        if isinstance(issue, Mapping):
            issue = add_repair_hint(issue, _GENERIC_HINT)
        key = json.dumps(issue, sort_keys=True, allow_nan=False)
        if key not in seen:
            issues.append(issue)
            seen.add(key)
    merged = {**prior, **payload, "schema": SOURCE_DIAGNOSTICS_SCHEMA,
              "runId": run_id, "issues": issues}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    try:
        temporary.write_text(json.dumps(merged, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class CadDiagnosticError(RuntimeError):
    """An actionable CAD failure with machine-readable evidence."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        check: str,
        part: str | None = None,
        observed: Any = None,
        expected: Any = None,
        repair_hint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("diagnostic code must be a non-empty string")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("diagnostic message must be a non-empty string")
        if not isinstance(check, str) or not check.strip():
            raise ValueError("diagnostic check must be a non-empty string")
        self.code = code.strip()
        self.check = check.strip()
        self.part = part.strip() if isinstance(part, str) and part.strip() else None
        self.observed = observed
        self.expected = expected
        self.repair_hint = (
            repair_hint.strip()
            if isinstance(repair_hint, str) and repair_hint.strip()
            else None
        )
        self.details = dict(details or {})
        super().__init__(message.strip())

    def to_issue(self) -> dict[str, Any]:
        issue = {
            **self.details,
            "check": self.check,
            "code": self.code,
            "message": str(self),
            "severity": "error",
        }
        if self.part is not None:
            issue["part"] = self.part
        if self.observed is not None:
            issue["observed"] = self.observed
        if self.expected is not None:
            issue["expected"] = self.expected
        if self.repair_hint is not None:
            issue["repairHint"] = self.repair_hint
        return issue


def source_diagnostics_payload(
    diagnostics: Iterable[CadDiagnosticError | Mapping[str, Any]],
) -> dict[str, Any]:
    """Create the JSON payload understood by ``cad_compile`` source handling."""

    issues = [
        diagnostic.to_issue()
        if isinstance(diagnostic, CadDiagnosticError)
        else dict(diagnostic)
        for diagnostic in diagnostics
    ]
    return {
        "issues": issues,
        "pass": not any(issue.get("severity", "error") == "error" for issue in issues),
        "schema": SOURCE_DIAGNOSTICS_SCHEMA,
    }
