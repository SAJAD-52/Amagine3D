"""Structured diagnostics shared by CAD authoring and compiler subprocesses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _HintRule:
    route: str
    action: str


_BOOLEAN_GUIDE = "references/operations/booleans.md#"
_FINISHING_GUIDE = "references/operations/finishing.md#"
_DECLARATION_RULE = _HintRule(
    _DIAGNOSTIC_GUIDES + "#declaration-and-binding-errors",
    "Repair the Agent-authored source or declaration using ruleId, field, owner and actual/expected values.",
)
_FIXED_HINT_RULES = {
    "SOURCE.CUT_MISSED_OWNER": _HintRule(
        _BOOLEAN_GUIDE + "cut-missed-its-owner",
        "Compare owner and cutter frames, then inspect removed-volume and boolean-witness evidence.",
    ),
    "SOURCE.CHECKED_CUT_FAILED": _HintRule(
        _BOOLEAN_GUIDE + "boolean-execution-failed",
        "Validate owner/tool inputs and inspect the operation log before isolating the cut.",
    ),
    "SOURCE.UNION_DISCONNECTED": _HintRule(
        _BOOLEAN_GUIDE + "union-disconnected",
        "Inspect component count and witness gap/nearest-point evidence against the intended owner.",
    ),
    "SOURCE.CHECKED_UNION_FAILED": _HintRule(
        _BOOLEAN_GUIDE + "boolean-execution-failed",
        "Validate body/addition inputs and inspect the operation log before isolating the union.",
    ),
    "DESIGN.SELECTION_EMPTY": _HintRule(
        _FINISHING_GUIDE + "dsl-selection-or-finishing-failure",
        "Read the current node, edges.where/edges.of and candidate evidence; preserve the requested size.",
    ),
    "DESIGN.SELECTION_COUNT": _HintRule(
        _FINISHING_GUIDE + "dsl-selection-or-finishing-failure",
        "Compare actual.count, expected and current node candidates; do not change the expected count merely to pass.",
    ),
    "DESIGN.REVISION_CONFLICT": _HintRule(
        _DIAGNOSTIC_GUIDES + "#revision-and-stale-evidence",
        "Read the current revision and affected nodes, then apply the edit against that revision.",
    ),
    "SOURCE.PREFLIGHT_FAILED": _HintRule(
        _DIAGNOSTIC_GUIDES + "#declaration-and-binding-errors",
        "Repair the Agent-authored source or declaration at the named field, preserving its rule and target.",
    ),
    "DRAFT.TIMEOUT": _HintRule(
        _DIAGNOSTIC_GUIDES + "#timeouts-and-unknown-errors",
        "Read the current attempt, failed stage and logs; preserve input bindings.",
    ),
    "DRAFT.INCOMPLETE": _HintRule(
        _DIAGNOSTIC_GUIDES + "#timeouts-and-unknown-errors",
        "Read the current attempt, failed stage and logs; preserve input bindings.",
    ),
}
_FINISHING_CODES = {"SOURCE.CHECKED_FILLET_FAILED", "SOURCE.CHECKED_CHAMFER_FAILED"}
_DESIGN_WRAPPER_CODES = {"DESIGN.KERNEL_FAILED", "DESIGN.POSTCONDITION_FAILED"}
_DECLARATION_PREFIXES = ("DESIGN.BINDING_", "DESIGN.OWNERSHIP")
_CAD_STAGE_PREFIXES = ("CAD.", "DISPLAY.", "VISUAL.")
_CAD_STAGE_RULE = _HintRule(
    _DIAGNOSTIC_GUIDES + "#cad-and-display-failures",
    "Inspect the bound stage and source evidence; use its recovery instructions without inferring acceptance.",
)
_GENERIC_WRAPPER_RULE = _HintRule(
    _DIAGNOSTIC_GUIDES + "#timeouts-and-unknown-errors",
    "Read node, op, field and concrete source issues; do not infer a kernel cause from the wrapper.",
)
_CAD_STAGE_ACTIONS = {
    "cadExport": "Inspect the bound CAD export and source evidence; do not treat export success as manufacturing acceptance.",
    "preview": "Inspect preview bindings; reuse passed manufacturing checks only when their current bindings verify.",
}
_DEFAULT_CAD_STAGE_ACTION = "Inspect the bound CAD export and input artifacts before retrying this display stage."
_DESIGN_OPERATION_RULES = {
    **dict.fromkeys(
        ("fillet", "chamfer"),
        _HintRule(
            _FINISHING_GUIDE + "dsl-selection-or-finishing-failure",
            _GENERIC_WRAPPER_RULE.action,
        ),
    ),
    **dict.fromkeys(
        ("cut", "subtract", "difference", "union", "add"),
        _HintRule(
            _BOOLEAN_GUIDE + "boolean-execution-failed",
            _GENERIC_WRAPPER_RULE.action,
        ),
    ),
}


def _evidence_map(issue: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = issue.get(field)
    return value if isinstance(value, Mapping) else {}


def _finishing_hint_rule(observed: Mapping[str, Any]) -> _HintRule | None:
    if type(observed.get("selectedEdgeCount")) is int and observed["selectedEdgeCount"] == 0:
        return _HintRule(
            _FINISHING_GUIDE + "python-selector-found-no-edges",
            "Enumerate edge candidates and verify the source selector; do not change the requested radius or length.",
        )
    if isinstance(observed.get("attempts"), list) and observed["attempts"]:
        return _HintRule(
            _FINISHING_GUIDE + "python-finishing-execution-failed",
            "Inspect attempts and selected local topology; explore parameters only within unchanged requirements.",
        )
    return None


def _design_wrapper_rule(issue: Mapping[str, Any]) -> _HintRule:
    op = issue.get("op")
    if not isinstance(op, str) and isinstance(issue.get("details"), Mapping):
        op = issue["details"].get("op")
    check = issue.get("check")
    if not isinstance(op, str) and isinstance(check, str) and check.lower() in {"fillet", "chamfer", "cut", "union"}:
        op = check.lower()
    return _DESIGN_OPERATION_RULES.get(op.lower(), _GENERIC_WRAPPER_RULE) if isinstance(op, str) else _GENERIC_WRAPPER_RULE


def _selection_count_rule(actual: Mapping[str, Any]) -> _HintRule:
    count = actual.get("count")
    if type(count) is int:
        action = f"Compare actual.count={count} with expected and current node candidates; do not change expected merely to pass."
        return _HintRule(_FIXED_HINT_RULES["DESIGN.SELECTION_COUNT"].route, action)
    return _FIXED_HINT_RULES["DESIGN.SELECTION_COUNT"]


def _declaration_rule(issue: Mapping[str, Any]) -> _HintRule:
    rule_id = issue.get("ruleId")
    if isinstance(rule_id, str) and rule_id:
        action = _DECLARATION_RULE.action + " Start with ruleId " + rule_id + "."
        return _HintRule(_DECLARATION_RULE.route, action)
    return _DECLARATION_RULE


def _cad_stage_rule(issue: Mapping[str, Any]) -> _HintRule:
    stage = issue.get("stage")
    if not isinstance(stage, str) or stage not in {"cadExport", "tessellation", "display", "preview"}:
        return _CAD_STAGE_RULE
    action = _CAD_STAGE_ACTIONS.get(stage, _DEFAULT_CAD_STAGE_ACTION)
    return _HintRule(_CAD_STAGE_RULE.route, action)


def _hint_rule(
    issue: Mapping[str, Any],
    code: str,
    observed: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> _HintRule | None:
    if code in _FINISHING_CODES:
        return _finishing_hint_rule(observed)
    if code in _DESIGN_WRAPPER_CODES:
        return _design_wrapper_rule(issue)
    if code == "AUTHORING.INVALID":
        return _declaration_rule(issue)
    if code.startswith(_DECLARATION_PREFIXES):
        return _declaration_rule(issue)
    if code.startswith(_CAD_STAGE_PREFIXES):
        return _cad_stage_rule(issue)
    if code in _FIXED_HINT_RULES:
        rule = _FIXED_HINT_RULES[code]
        return _selection_count_rule(actual) if code == "DESIGN.SELECTION_COUNT" else rule
    return None


def repair_hint_for_issue(issue: Mapping[str, Any]) -> str | None:
    """Return a guide route for a known structured issue, without guessing causes."""
    code = issue.get("code")
    if not isinstance(code, str):
        return None
    rule = _hint_rule(issue, code, _evidence_map(issue, "observed"), _evidence_map(issue, "actual"))
    if rule is None:
        return None
    return rule.action + " Read $AMAGINE3D_RUNTIME_DIR/" + rule.route


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
