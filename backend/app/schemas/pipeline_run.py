"""Pydantic schemas for PipelineRun + eligibility (#1425 PR B)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class EligibilityIssueResponse(BaseModel):
    """Single eligibility issue — see ``services/pipeline_eligibility.py`` for
    the full list of ``kind`` values and what each means."""

    kind: Literal[
        "printer_not_set",
        "printer_not_found",
        "printer_disabled",
        "printer_offline",
        "filament_type_mismatch",
        "filament_color_mismatch",
        "ams_slot_missing",
        "filament_unverified",
    ]
    slot_index: int | None = None
    expected: str | None = None
    actual: str | None = None


class EligibilityReportResponse(BaseModel):
    """Returned by both ``POST /check-eligibility`` and (on 409) ``POST /run``
    so the frontend can render the same modal in either flow."""

    ok: bool
    target_printer_id: int | None = None
    target_printer_name: str | None = None
    issues: list[EligibilityIssueResponse] = []


class CheckEligibilityRequest(BaseModel):
    """Exactly one of ``source_library_file_id`` / ``source_archive_id`` must
    be set. The eligibility matcher itself doesn't read the source — it only
    needs the pipeline + live AMS state — but the route validates source
    existence here so the same modal can pre-flight both archive and library
    sources without growing a second endpoint.
    """

    source_library_file_id: int | None = None
    source_archive_id: int | None = None
    force: bool = Field(default=False)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "CheckEligibilityRequest":
        if (self.source_library_file_id is None) == (self.source_archive_id is None):
            raise ValueError("exactly one of source_library_file_id or source_archive_id must be set")
        return self


class PipelineRunCreateRequest(BaseModel):
    """Same XOR shape as CheckEligibilityRequest — see that schema's docstring."""

    source_library_file_id: int | None = None
    source_archive_id: int | None = None
    force: bool = Field(
        default=False,
        description="When False (default), the route returns 409 with the eligibility report if any blocking issue exists. When True, the run starts even when issues exist — recorded on PipelineRun.eligibility_overridden so the audit trail shows which runs bypassed pre-flight.",
    )

    @model_validator(mode="after")
    def exactly_one_source(self) -> "PipelineRunCreateRequest":
        if (self.source_library_file_id is None) == (self.source_archive_id is None):
            raise ValueError("exactly one of source_library_file_id or source_archive_id must be set")
        return self


class PipelineJobResponse(BaseModel):
    id: int
    pipeline_run_id: int
    copy_index: int
    assigned_printer_id: int | None
    assigned_printer_name: str | None = None
    queue_entry_id: int | None
    status: Literal[
        "pending",
        "awaiting_printer",
        "queued",
        "printing",
        "completed",
        "failed",
        "cancelled",
    ]
    error_message: str | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None


class PipelineRunResponse(BaseModel):
    id: int
    pipeline_id: int | None
    pipeline_name: str | None = None
    source_library_file_id: int | None
    source_archive_id: int | None = None
    source_filename: str | None = None
    copies: int
    status: Literal[
        "queued",
        "slicing",
        "dispatching",
        "in_progress",
        "completed",
        "failed",
        "cancelled",
    ]
    slice_job_id: int | None
    sliced_library_file_id: int | None
    eligibility_overridden: bool
    error_message: str | None = None
    created_by: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    jobs: list[PipelineJobResponse] = []


class PipelineRunListResponse(BaseModel):
    runs: list[PipelineRunResponse] = []
