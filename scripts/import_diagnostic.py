"""Standard-library-only receipt validation and public-safe import diagnostics."""

from __future__ import annotations

import json
import re
from pathlib import Path


DIAGNOSTIC_STAGES = {
    "starting", "configuration", "browser", "pospal_login", "export", "myj_login",
    "import_ready", "category_preflight", "import_request", "import_receipt", "receipt_saved", "summary_saved",
    "closing_browser", "completion_marker", "completed",
}
DIAGNOSTIC_REQUEST_STATES = {"not_started", "started", "response_received"}
DIAGNOSTIC_OUTCOMES = {"not_imported", "unknown", "partial", "succeeded", "rejected"}
DIAGNOSTIC_ERRORS = {
    "none", "stage_failed", "transport_unknown", "http_error", "invalid_json",
    "invalid_receipt", "failed_rows", "backend_conflict_constraint", "category_preflight_failed",
}
IMPORT_COUNT_KEYS = ("insertCount", "updateCount", "skipCount", "successCount", "failCount")


class BrowserAutomationError(RuntimeError):
    """Raised when browser automation or its receipt cannot complete safely."""


class ImportDiagnostic:
    """Store only public-safe enumerations/counts, never exception or response text."""

    def __init__(self, run_key: str, directory: Path | None = None) -> None:
        if not re.fullmatch(r"\d+:\d+", run_key):
            raise ValueError("Invalid diagnostic run key")
        self.path = (directory or Path(__file__).resolve().parents[1] / ".schedule-state") / "import-diagnostic.json"
        self.value: dict = {
            "version": 1, "run_key": run_key, "stage": "starting",
            "request_state": "not_started", "outcome": "not_imported", "error": "none",
        }
        self.update()

    def update(self, **fields: object) -> None:
        self.value.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.value, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def stage(self, stage: str) -> None:
        if stage not in DIAGNOSTIC_STAGES:
            raise ValueError("Invalid diagnostic stage")
        self.update(stage=stage)


def import_count_semantics(counts: dict[str, int]) -> str:
    changed = counts["insertCount"] + counts["updateCount"]
    if not counts["skipCount"] and counts["successCount"] == changed:
        return "no_skips"
    if counts["successCount"] == changed + counts["skipCount"]:
        return "skips_in_success"
    if counts["successCount"] == changed:
        return "skips_separate"
    raise BrowserAutomationError("MYJ import receipt success counts do not reconcile.")


def strict_import_counts(result: object, expected_rows: int | None = None) -> dict[str, int]:
    if not isinstance(result, dict) or type(result.get("success")) is not bool:
        raise BrowserAutomationError("MYJ import receipt has no boolean success field.")
    data = result.get("data")
    if not isinstance(data, dict):
        raise BrowserAutomationError("MYJ import receipt has no count data.")
    counts = {key: data.get(key) for key in IMPORT_COUNT_KEYS}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise BrowserAutomationError("MYJ import receipt counts must be non-negative integers.")
    import_count_semantics(counts)
    accounted_rows = counts["insertCount"] + counts["updateCount"] + counts["skipCount"] + counts["failCount"]
    if expected_rows is not None and accounted_rows != expected_rows:
        raise BrowserAutomationError("MYJ import receipt does not account for every exported row.")
    if "packWarnings" in data and not isinstance(data["packWarnings"], list):
        raise BrowserAutomationError("MYJ import receipt warning list is invalid.")
    return counts


def diagnose_import_receipt(result: object, diagnostic: ImportDiagnostic | None) -> None:
    if diagnostic is None:
        return
    try:
        counts = strict_import_counts(result, diagnostic.value.get("export_rows"))
    except BrowserAutomationError:
        diagnostic.update(error="invalid_receipt", outcome="unknown")
        return
    assert isinstance(result, dict)
    data = result["data"]
    outcome = "succeeded" if result["success"] and not counts["failCount"] else (
        "partial" if counts["successCount"] and counts["failCount"] else
        "rejected" if not counts["successCount"] else "unknown"
    )
    # Response text is used only for fixed classification, never copied to diagnostics.
    errors = data.get("errors") or []
    diagnostic_error = "failed_rows" if counts["failCount"] else "none"
    if "no unique or exclusion constraint matching the ON CONFLICT specification" in json.dumps([errors, result.get("message")]):
        diagnostic_error = "backend_conflict_constraint"
    diagnostic.update(
        counts=counts, warning_details=len(data.get("packWarnings") or []),
        count_semantics=import_count_semantics(counts), outcome=outcome, error=diagnostic_error,
    )
    total = pack_warning_total(result)
    if total is not None:
        diagnostic.update(warning_total=total)


def pack_warning_total(result: dict) -> int | None:
    """Extract only the observed fixed summary count; never copy backend prose."""
    message = result.get("message")
    if not isinstance(message, str):
        return None
    match = re.search(r"拆包关系\s*\d+\s*组（\s*\d+\s*个子品）[，,]\s*异常\s*(\d+)\s*组[。.]?\s*$", message)
    if not match:
        return None
    total = int(match.group(1))
    return total if total >= len((result.get("data") or {}).get("packWarnings") or []) else None
