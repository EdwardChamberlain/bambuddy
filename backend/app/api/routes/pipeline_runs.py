"""API routes for Slicer Pipeline runs (#1425 PR B).

PR B implements single-target dispatch: one Run-pipeline click =
  slice the source file once with the pipeline's four preset slots →
  enqueue one print on the pipeline's pinned target_printer_id.

PR C extends this with copies > 1 + class targeting + fanout strategies;
the data model already carries those columns so this file is the only
place that changes shape.

The slice is enqueued via ``slice_dispatch`` (the same path the manual
SliceModal uses), so the in-process progress toast renders for pipeline
runs the same way it does for manual slicing. The slice job's id lives
on PipelineRun.slice_job_id and is returned from ``POST /run`` so the
frontend can call ``trackJob`` directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.config import settings as app_settings
from backend.app.core.database import async_session, get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.pipeline_run import PipelineJob, PipelineRun
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.slicer_pipeline import SlicerPipeline
from backend.app.models.user import User
from backend.app.schemas.pipeline_run import (
    CheckEligibilityRequest,
    EligibilityIssueResponse,
    EligibilityReportResponse,
    PipelineJobResponse,
    PipelineRunCreateRequest,
    PipelineRunListResponse,
    PipelineRunResponse,
)
from backend.app.schemas.slicer import PresetRef, SliceRequest
from backend.app.services.pipeline_eligibility import (
    EligibilityReport,
    check_pipeline_eligibility,
)

logger = logging.getLogger(__name__)


# Two routers — one for the per-pipeline endpoints (mounted under the
# existing ``/slicer-pipelines`` prefix) and one for the per-run endpoints
# at ``/pipeline-runs``. Keeps the URL shape natural.
pipeline_run_create_router = APIRouter(prefix="/slicer-pipelines", tags=["Slicer Pipelines"])
pipeline_run_router = APIRouter(prefix="/pipeline-runs", tags=["Slicer Pipelines"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_status(report: EligibilityReport) -> EligibilityReportResponse:
    return EligibilityReportResponse(
        ok=report.ok,
        target_printer_id=report.target_printer_id,
        target_printer_name=report.target_printer_name,
        issues=[
            EligibilityIssueResponse(
                kind=issue.kind,
                slot_index=issue.slot_index,
                expected=issue.expected,
                actual=issue.actual,
            )
            for issue in report.issues
        ],
    )


async def _load_pipeline(db: AsyncSession, pipeline_id: int) -> SlicerPipeline:
    pipeline = (
        await db.execute(
            select(SlicerPipeline).where(
                SlicerPipeline.id == pipeline_id,
                SlicerPipeline.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(404, "Pipeline not found")
    return pipeline


async def _load_printer_status(printer_id: int | None) -> dict | None:
    """Snapshot the printer_manager's live PrinterState for the eligibility
    matcher. Returns ``None`` when the printer has no MQTT client (offline at
    the manager level)."""
    if printer_id is None:
        return None
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer_id)
    if state is None:
        return None
    return {"connected": state.connected, "raw_data": state.raw_data}


def _slice_request_from_pipeline(pipeline: SlicerPipeline) -> SliceRequest:
    """Materialise a SliceRequest from a pipeline so we can hand it to
    ``slice_and_persist`` exactly the way the existing SliceModal flow does."""
    try:
        raw_filaments = json.loads(pipeline.filament_presets_json or "[]")
    except (json.JSONDecodeError, TypeError):
        raw_filaments = []
    filament_presets = [
        PresetRef(source=r["source"], id=r["id"])
        for r in raw_filaments
        if isinstance(r, dict) and "source" in r and "id" in r
    ]
    return SliceRequest(
        printer_preset=PresetRef(source=pipeline.printer_preset_source, id=pipeline.printer_preset_id),
        process_preset=PresetRef(source=pipeline.process_preset_source, id=pipeline.process_preset_id),
        filament_presets=filament_presets,
        bed_type=pipeline.bed_type,
        export_3mf=True,
    )


def _compute_run_status(
    persisted: str,
    job: PipelineJob | None,
    queue_entry: PrintQueueItem | None,
) -> str:
    if persisted in ("failed", "cancelled", "completed"):
        return persisted
    if queue_entry is None:
        return persisted
    queue_status = queue_entry.status
    if queue_status == "completed":
        return "completed"
    if queue_status in ("failed", "cancelled", "aborted"):
        return "failed" if queue_status != "cancelled" else "cancelled"
    if queue_status == "printing":
        return "in_progress"
    return "dispatching" if persisted == "dispatching" else "queued"


def _compute_job_status(
    persisted: str,
    queue_entry: PrintQueueItem | None,
) -> str:
    if persisted in ("failed", "cancelled", "completed"):
        return persisted
    if queue_entry is None:
        return persisted
    qs = queue_entry.status
    if qs == "completed":
        return "completed"
    if qs in ("failed", "aborted"):
        return "failed"
    if qs == "cancelled":
        return "cancelled"
    if qs == "printing":
        return "printing"
    return "queued"


async def _materialise_run(db: AsyncSession, run: PipelineRun) -> PipelineRunResponse:
    pipeline_name: str | None = None
    if run.pipeline_id:
        pipeline = (
            await db.execute(select(SlicerPipeline).where(SlicerPipeline.id == run.pipeline_id))
        ).scalar_one_or_none()
        pipeline_name = pipeline.name if pipeline else None

    source_filename: str | None = None
    if run.source_library_file_id:
        src = (
            await db.execute(select(LibraryFile).where(LibraryFile.id == run.source_library_file_id))
        ).scalar_one_or_none()
        source_filename = src.filename if src else None
    elif run.source_archive_id:
        arc = (
            await db.execute(select(PrintArchive).where(PrintArchive.id == run.source_archive_id))
        ).scalar_one_or_none()
        source_filename = (arc.print_name or arc.filename) if arc else None

    job_rows = (
        (
            await db.execute(
                select(PipelineJob).where(PipelineJob.pipeline_run_id == run.id).order_by(PipelineJob.copy_index)
            )
        )
        .scalars()
        .all()
    )

    job_responses: list[PipelineJobResponse] = []
    rolled_up_status = run.status
    for job in job_rows:
        queue_entry = None
        if job.queue_entry_id:
            queue_entry = (
                await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == job.queue_entry_id))
            ).scalar_one_or_none()

        printer_name: str | None = None
        if job.assigned_printer_id:
            p = (await db.execute(select(Printer).where(Printer.id == job.assigned_printer_id))).scalar_one_or_none()
            printer_name = p.name if p else None

        live_job_status = _compute_job_status(job.status, queue_entry)
        job_responses.append(
            PipelineJobResponse(
                id=job.id,
                pipeline_run_id=job.pipeline_run_id,
                copy_index=job.copy_index,
                assigned_printer_id=job.assigned_printer_id,
                assigned_printer_name=printer_name,
                queue_entry_id=job.queue_entry_id,
                status=live_job_status,  # type: ignore[arg-type]
                error_message=job.error_message,
                dispatched_at=job.dispatched_at,
                completed_at=job.completed_at,
            )
        )
        rolled_up_status = _compute_run_status(run.status, job, queue_entry)

    return PipelineRunResponse(
        id=run.id,
        pipeline_id=run.pipeline_id,
        pipeline_name=pipeline_name,
        source_library_file_id=run.source_library_file_id,
        source_archive_id=run.source_archive_id,
        source_filename=source_filename,
        copies=run.copies,
        status=rolled_up_status,  # type: ignore[arg-type]
        slice_job_id=run.slice_job_id,
        sliced_library_file_id=run.sliced_library_file_id,
        eligibility_overridden=run.eligibility_overridden,
        error_message=run.error_message,
        created_by=run.created_by,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        jobs=job_responses,
    )


# ---------------------------------------------------------------------------
# Source resolution + orchestration
# ---------------------------------------------------------------------------


SourceKind = Literal["library_file", "archive"]


async def _resolve_source(
    db: AsyncSession,
    *,
    library_file_id: int | None,
    archive_id: int | None,
) -> tuple[SourceKind, int, str, Path]:
    """Look up the source row + on-disk path for either input kind. Raises 404
    when the row or its file is missing — same shape as the SliceModal's flow
    for both libraryFile and archive."""
    if library_file_id is not None:
        lib = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
        if lib is None:
            raise HTTPException(404, "Source library file not found")
        src_path = (
            Path(app_settings.base_dir) / lib.file_path
        )  # SEC-PATH-OK: lib.file_path is a LibraryFile DB column set only by the upload route (routes/library.py POST /files), which writes a UUID-named file under base_dir/library_files/ — never user-controlled at this site.
        if not src_path.exists():
            raise HTTPException(404, "Source library file missing on disk")
        return ("library_file", lib.id, lib.filename, src_path)

    assert archive_id is not None
    arc = (await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))).scalar_one_or_none()
    if arc is None:
        raise HTTPException(404, "Source archive not found")
    rel = arc.source_3mf_path or arc.file_path
    if not rel:
        raise HTTPException(400, "Archive has no source file to slice")
    src_path = (
        Path(app_settings.base_dir) / rel
    )  # SEC-PATH-OK: rel is archive.source_3mf_path / archive.file_path, both set by upload-time validators (_resolve_source_3mf_path + archive ingestion) that already do resolve+relative_to containment. Mirrors routes/archives.py:3955.
    if not src_path.exists():
        raise HTTPException(404, "Archive source file missing on disk")
    name = arc.filename or arc.print_name or src_path.name
    return ("archive", arc.id, name, src_path)


def _make_orchestration_callable(
    *,
    run_id: int,
    pipeline_id: int,
    src_kind: SourceKind,
    src_id: int,
    src_filename: str,
    src_path: Path,
    target_printer_id: int,
    creator_user_id: int | None,
):
    """Return the async callable that ``slice_dispatch.enqueue`` will run as
    the background slice job. Wrapping it as the slice job's ``run`` means the
    SliceJob's lifecycle (pending → running → completed/failed) drives the
    progress toast on the frontend exactly the same as a manual SliceModal
    slice — no separate notification surface for pipeline runs."""

    async def _orchestrate(slice_job_id: int) -> dict:
        # Local import — slice_and_persist lives in routes/library which
        # imports back into this module transitively via slicer_pipelines.
        from backend.app.api.routes.library import slice_and_persist

        async with async_session() as session:
            run = (await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
            pipeline = (
                await session.execute(select(SlicerPipeline).where(SlicerPipeline.id == pipeline_id))
            ).scalar_one_or_none()
            if run is None or pipeline is None:
                logger.warning(
                    "pipeline_run %d or pipeline %d disappeared mid-orchestration",
                    run_id,
                    pipeline_id,
                )
                return {}

            # Refresh the snapshot status now that slicing has actually
            # started — the route handler already wrote slice_job_id but
            # left status='queued' until this point.
            run.status = "slicing"
            run.started_at = datetime.now(timezone.utc)
            await session.commit()

            slice_request = _slice_request_from_pipeline(pipeline)
            model_bytes = src_path.read_bytes()

            # Resolve the folder for the sliced output. Library sources
            # keep their folder; archive sources fall through to root.
            folder_id: int | None = None
            if src_kind == "library_file":
                lib = (await session.execute(select(LibraryFile).where(LibraryFile.id == src_id))).scalar_one_or_none()
                if lib is not None:
                    folder_id = lib.folder_id

            try:
                slice_response = await slice_and_persist(
                    session,
                    model_bytes=model_bytes,
                    model_filename=src_filename,
                    folder_id=folder_id,
                    extra_metadata={
                        f"sliced_from_{src_kind}_id": src_id,
                        "sliced_via_pipeline_id": pipeline.id,
                        "sliced_via_pipeline_run_id": run.id,
                    },
                    request=slice_request,
                    current_user_id=creator_user_id,
                    job_id=slice_job_id,  # threads --pipe progress
                )
            except HTTPException as exc:
                # _SliceJobError isn't directly importable here without a
                # heavier dep; surface the slice failure cleanly and let
                # the dispatcher's generic Exception path mark the
                # SliceJob failed. Persist the pipeline-side state here.
                run.status = "failed"
                run.error_message = f"Slice failed: {exc.detail}"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
                raise
            except Exception as exc:
                logger.exception("Pipeline run %d slice raised unexpectedly", run_id)
                run.status = "failed"
                run.error_message = f"Slice failed: {exc}"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
                raise

            run.sliced_library_file_id = slice_response.library_file_id

            # PR B copies=1 — exactly one PipelineJob to dispatch.
            job = (
                (await session.execute(select(PipelineJob).where(PipelineJob.pipeline_run_id == run_id)))
                .scalars()
                .first()
            )
            if job is None:
                logger.warning("pipeline_run %d has no PipelineJob row", run_id)
                run.status = "failed"
                run.error_message = "Internal: missing pipeline_job row"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()
                return slice_response.model_dump()

            queue_item = PrintQueueItem(
                printer_id=target_printer_id,
                library_file_id=slice_response.library_file_id,
                created_by_id=creator_user_id,
                status="pending",
            )
            session.add(queue_item)
            await session.flush()

            job.queue_entry_id = queue_item.id
            job.assigned_printer_id = target_printer_id
            job.status = "queued"
            job.dispatched_at = datetime.now(timezone.utc)

            run.status = "dispatching"
            await session.commit()

            return slice_response.model_dump()

    return _orchestrate


# ---------------------------------------------------------------------------
# /slicer-pipelines/{id}/check-eligibility
# ---------------------------------------------------------------------------


@pipeline_run_create_router.post("/{pipeline_id}/check-eligibility", response_model=EligibilityReportResponse)
async def check_eligibility(
    pipeline_id: int,
    body: CheckEligibilityRequest,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    """Pre-flight check before a Run-pipeline click. Returns an eligibility
    report; the frontend uses it to show a confirmation modal if any blocking
    issue exists. Accepts either a library file id or an archive id (XOR)."""
    pipeline = await _load_pipeline(db, pipeline_id)
    # Source resolution — same shape as /run uses, so a 404 here means /run
    # would also 404 and the user can't proceed.
    await _resolve_source(
        db,
        library_file_id=body.source_library_file_id,
        archive_id=body.source_archive_id,
    )
    status = await _load_printer_status(pipeline.target_printer_id)
    report = await check_pipeline_eligibility(db, pipeline, status)
    return _serialise_status(report)


# ---------------------------------------------------------------------------
# /slicer-pipelines/{id}/run
# ---------------------------------------------------------------------------


@pipeline_run_create_router.post("/{pipeline_id}/run", response_model=PipelineRunResponse, status_code=202)
async def run_pipeline(
    pipeline_id: int,
    body: PipelineRunCreateRequest,
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.PIPELINES_RUN),
    db: AsyncSession = Depends(get_db),
):
    """Kick off a pipeline run. PR B: copies=1, target = pipeline's pinned
    printer. Returns 202 immediately; the slice runs in the background via
    ``slice_dispatch`` and the slice job id rides on the response so the
    frontend can attach the progress toast."""
    from backend.app.services.slice_dispatch import slice_dispatch

    pipeline = await _load_pipeline(db, pipeline_id)
    src_kind, src_id, src_filename, src_path = await _resolve_source(
        db,
        library_file_id=body.source_library_file_id,
        archive_id=body.source_archive_id,
    )

    # Eligibility pre-flight. ``force=True`` from the confirmation modal
    # bypasses the 409.
    status = await _load_printer_status(pipeline.target_printer_id)
    report = await check_pipeline_eligibility(db, pipeline, status)
    if not report.ok and not body.force:
        raise HTTPException(status_code=409, detail=_serialise_status(report).model_dump())

    if pipeline.target_printer_id is None:
        raise HTTPException(
            400,
            "Pipeline has no target printer set. Open the pipeline in Settings → Workflow → Pipelines and choose a target.",
        )

    run = PipelineRun(
        pipeline_id=pipeline.id,
        source_library_file_id=src_id if src_kind == "library_file" else None,
        source_archive_id=src_id if src_kind == "archive" else None,
        copies=1,
        status="queued",
        eligibility_overridden=(not report.ok and body.force),
        created_by=current_user.id if current_user else None,
    )
    db.add(run)
    await db.flush()

    job = PipelineJob(
        pipeline_run_id=run.id,
        copy_index=0,
        assigned_printer_id=pipeline.target_printer_id,
        status="pending",
    )
    db.add(job)
    await db.commit()
    await db.refresh(run)

    # Enqueue the slice job. The callable inside slice_dispatch.enqueue does
    # the full slice → enqueue-print → state-update chain. SliceJob lifecycle
    # drives the existing progress toast.
    orchestrate = _make_orchestration_callable(
        run_id=run.id,
        pipeline_id=pipeline.id,
        src_kind=src_kind,
        src_id=src_id,
        src_filename=src_filename,
        src_path=src_path,
        target_printer_id=pipeline.target_printer_id,
        creator_user_id=current_user.id if current_user else None,
    )
    slice_job = await slice_dispatch.enqueue(
        kind="library_file" if src_kind == "library_file" else "archive",
        source_id=src_id,
        source_name=src_filename,
        run=orchestrate,
    )

    run.slice_job_id = slice_job.id
    await db.commit()
    await db.refresh(run)

    return await _materialise_run(db, run)


# ---------------------------------------------------------------------------
# /slicer-pipelines/{id}/runs  + /pipeline-runs/{id}  + cancel
# ---------------------------------------------------------------------------


@pipeline_run_create_router.get("/{pipeline_id}/runs", response_model=PipelineRunListResponse)
async def list_runs_for_pipeline(
    pipeline_id: int,
    limit: int = 10,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    limit = max(1, min(limit, 100))
    rows = (
        (
            await db.execute(
                select(PipelineRun)
                .where(PipelineRun.pipeline_id == pipeline_id)
                .order_by(PipelineRun.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PipelineRunListResponse(runs=[await _materialise_run(db, r) for r in rows])


@pipeline_run_router.get("/{run_id}", response_model=PipelineRunResponse)
async def get_run(
    run_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PIPELINES_READ),
    db: AsyncSession = Depends(get_db),
):
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Pipeline run not found")
    return await _materialise_run(db, run)


@pipeline_run_router.post("/{run_id}/cancel", response_model=PipelineRunResponse)
async def cancel_run(
    run_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PIPELINES_RUN),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a queued / in-flight run. Pre-slice or post-slice — semantics
    are described on the original PR B route."""
    run = (await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Pipeline run not found")

    if run.status in ("completed", "failed", "cancelled"):
        return await _materialise_run(db, run)

    run.status = "cancelled"
    run.completed_at = datetime.now(timezone.utc)
    if not run.error_message:
        run.error_message = "Cancelled by user"

    job_rows = (await db.execute(select(PipelineJob).where(PipelineJob.pipeline_run_id == run.id))).scalars().all()
    for job in job_rows:
        if job.queue_entry_id:
            queue_entry = (
                await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == job.queue_entry_id))
            ).scalar_one_or_none()
            if queue_entry is not None and queue_entry.status in (
                "pending",
                "queued",
            ):
                queue_entry.status = "cancelled"
        if job.status not in ("completed", "failed", "cancelled"):
            job.status = "cancelled"
            job.completed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(run)
    return await _materialise_run(db, run)
