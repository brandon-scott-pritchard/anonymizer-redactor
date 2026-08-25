"""Classification error reports.

When the operator corrects a wrong classification they can say what went
wrong. Each report is appended to a local JSONL log shaped for later
training - the flagged string, the wrong label, the right label, and where
the flag came from - and can be turned into a ``mailto:`` URL so the
operator's own mail client opens a draft they can review and send.

Nothing here sends anything. The mail client draft is the only exit, and
the operator sees it before it goes.
"""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import __version__

REPORT_ADDRESS = "bots@pritchard.works"
FEEDBACK_PATH = Path.home() / ".anonymizer-redactor" / "feedback.jsonl"

# What the operator can say went wrong. The first entry means "no report".
NO_ERROR = "(no error - just adjusting)"
ERROR_TYPES = (
    NO_ERROR,
    "Person flagged as organization",
    "Organization flagged as person",
    "Wrong category (other)",
    "Should not have been flagged at all",
    "Replacement was wrong or unusable",
    "Other",
)

# mailto: URLs longer than ~2000 characters break in several mail clients.
_BODY_LIMIT = 1800


def build_report(
    *,
    error_type: str,
    text: str,
    predicted_category: str,
    corrected_category: str,
    corrected_replacement: str | None = None,
    source: str = "",
    occurrences: int = 0,
    documents: list[str] | None = None,
    origin: str = "review",
) -> dict:
    """One correction, shaped for training: input, wrong label, right label.

    ``documents`` should be basenames only - never paths, never content.
    The flagged ``text`` itself is the only document-derived string here.
    """
    return {
        "schema": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version": __version__,
        "platform": platform.platform(),
        "error_type": error_type,
        "text": text,
        "predicted_category": predicted_category,
        "corrected_category": corrected_category,
        "corrected_replacement": corrected_replacement,
        "source": source,
        "occurrences": occurrences,
        "documents": sorted(documents or []),
        "origin": origin,
    }


def log_report(report: dict, path: Path = FEEDBACK_PATH) -> Path:
    """Append one JSON line; create the directory on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False) + "\n")
    return path


def mailto_url(report: dict, log_path: Path = FEEDBACK_PATH) -> str:
    """A mailto: draft of ``report``, readable up top, JSON below."""
    lines = [
        "Review before sending - the flagged text may be confidential.",
        "",
        f"What went wrong:  {report.get('error_type', '')}",
        f"Flagged text:     {report.get('text', '')}",
        f"Was classified:   {report.get('predicted_category', '')}",
        f"Corrected to:     {report.get('corrected_category', '')}",
        f"Found by:         {report.get('source', '')}",
        f"App version:      {report.get('app_version', '')}",
        "",
        "Machine-readable copy:",
        json.dumps(report, ensure_ascii=False, indent=2),
    ]
    body = "\n".join(lines)
    if len(body) > _BODY_LIMIT:
        note = f"\n\n[Truncated - the full report is in {log_path}]"
        body = body[: _BODY_LIMIT - len(note)] + note
    subject = "Document Redactions & Anonymization classification report"
    return f"mailto:{REPORT_ADDRESS}?subject={quote(subject)}&body={quote(body)}"
