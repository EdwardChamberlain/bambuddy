"""Integration tests for Slicer Pipeline runs (#1425 PR B).

Slicing itself is a network call to the slicer sidecar — these tests
stub ``slice_and_persist`` so the orchestration logic is exercised without
needing a live sidecar in CI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def _pipeline_payload(**overrides) -> dict:
    payload = {
        "name": "Production Batch",
        "description": None,
        "printer_preset": {"source": "local", "id": "1"},
        "process_preset": {"source": "local", "id": "2"},
        "filament_presets": [{"source": "local", "id": "3"}],
        "bed_type": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def pipeline_factory(async_client: AsyncClient):
    """Create pipelines via the API + optionally set a target printer."""

    async def _make(target_printer_id: int | None = None, **overrides) -> dict:
        resp = await async_client.post("/api/v1/slicer-pipelines/", json=_pipeline_payload(**overrides))
        assert resp.status_code == 201, resp.text
        pipeline = resp.json()
        if target_printer_id is not None:
            put_resp = await async_client.put(
                f"/api/v1/slicer-pipelines/{pipeline['id']}",
                json={"target_kind": "specific_printer", "target_printer_id": target_printer_id},
            )
            assert put_resp.status_code == 200, put_resp.text
            pipeline = put_resp.json()
        return pipeline

    return _make


@pytest.fixture
async def printer_factory(db_session):
    """Insert a Printer row for tests that need a target_printer_id."""
    from backend.app.models.printer import Printer

    counter = [0]

    async def _make(**overrides) -> Printer:
        counter[0] += 1
        defaults = {
            "name": f"X1C #{counter[0]}",
            "serial_number": f"SERIAL{counter[0]:04d}",
            "ip_address": "192.0.2.1",
            "access_code": "ABCD1234",
            "model": "Bambu Lab X1 Carbon",
            "is_active": True,
        }
        defaults.update(overrides)
        printer = Printer(**defaults)
        db_session.add(printer)
        await db_session.commit()
        await db_session.refresh(printer)
        return printer

    return _make


@pytest.fixture
async def library_file_factory(db_session):
    """Insert a LibraryFile row for tests that need a source_library_file_id."""
    from pathlib import Path

    from backend.app.core.config import settings as app_settings
    from backend.app.models.library import LibraryFile

    counter = [0]

    async def _make(**overrides) -> LibraryFile:
        counter[0] += 1
        # Materialise an empty file on disk so the orchestration's path-exists
        # guard passes when tests reach it.
        rel = f"test_pipeline_run_{counter[0]}.3mf"
        abs_path = Path(app_settings.base_dir) / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"")
        defaults = {
            "filename": f"cube_{counter[0]}.3mf",
            "file_path": rel,
            "file_type": "3mf",
            "file_size": 0,
            "file_hash": f"hash_{counter[0]}",
            "source_type": "uploaded",
        }
        defaults.update(overrides)
        row = LibraryFile(**defaults)
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _make


class TestSlicerPipelineTarget:
    """PUT /slicer-pipelines/{id} accepts the new target fields."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_writes_target(self, async_client: AsyncClient, pipeline_factory, printer_factory):
        printer = await printer_factory()
        pipeline = await pipeline_factory()
        resp = await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={"target_kind": "specific_printer", "target_printer_id": printer.id},
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["target_kind"] == "specific_printer"
        assert updated["target_printer_id"] == printer.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_target_printer_id_zero_clears(
        self, async_client: AsyncClient, pipeline_factory, printer_factory
    ):
        """Empty-select dropdown sends target_printer_id=0 → backend treats
        as 'clear' rather than referencing printer #0 (which doesn't exist)."""
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        resp = await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={"target_printer_id": 0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_printer_id"] is None


class TestCheckEligibility:
    """POST /slicer-pipelines/{id}/check-eligibility surfaces structured issues."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_target_set(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        library_file_factory,
    ):
        pipeline = await pipeline_factory()  # no target set
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
            json={"source_library_file_id": src.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        kinds = [i["kind"] for i in body["issues"]]
        assert "printer_not_set" in kinds

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_printer_disabled(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
    ):
        printer = await printer_factory(is_active=False)
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        with patch("backend.app.api.routes.pipeline_runs._load_printer_status", new=AsyncMock(return_value=None)):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 200
        body = resp.json()
        kinds = [i["kind"] for i in body["issues"]]
        assert "printer_disabled" in kinds
        # printer_offline also fires because get_status returns None — both
        # issues are expected and both block.
        assert "printer_offline" in kinds
        assert body["ok"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_online_match_clears_issues(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
        db_session,
    ):
        """Patch printer_manager so AMS slot 0 carries the same canonical
        type the pipeline's local-tier filament preset declares."""
        from backend.app.models.local_preset import LocalPreset

        preset = LocalPreset(
            name="My PLA",
            preset_type="filament",
            source="manual",
            setting="{}",
            filament_type="PLA",
            default_filament_colour="#FFFFFF",
        )
        db_session.add(preset)
        await db_session.commit()
        await db_session.refresh(preset)

        printer = await printer_factory()
        pipeline = await pipeline_factory(
            target_printer_id=printer.id,
            filament_presets=[{"source": "local", "id": str(preset.id)}],
        )
        src = await library_file_factory()

        live_status = {
            "connected": True,
            "raw_data": {"ams": [{"tray": [{"tray_type": "PLA Basic", "tray_color": "FFFFFFFF"}]}]},
        }
        with patch(
            "backend.app.api.routes.pipeline_runs._load_printer_status",
            new=AsyncMock(return_value=live_status),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["issues"] == []
        assert body["target_printer_name"] == printer.name


class TestRunPipeline:
    """POST /slicer-pipelines/{id}/run orchestrates slice + enqueue."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_with_issues_and_no_force_returns_409(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        library_file_factory,
    ):
        pipeline = await pipeline_factory()  # no target set
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id},
        )
        assert resp.status_code == 409
        # Eligibility report rides in detail.
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        assert any(i["kind"] == "printer_not_set" for i in detail["issues"])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_force_with_no_target_still_400(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        library_file_factory,
    ):
        """``force=True`` bypasses the 409 but the run endpoint still needs a
        target to enqueue against — the second guard returns 400."""
        pipeline = await pipeline_factory()
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id, "force": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_creates_run_and_job(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()

        live_status = {"connected": True, "raw_data": {"ams": []}}
        # AMS empty → eligibility surfaces filament_unverified (non-blocking)
        # for the standard-tier filament refs the default factory uses; report
        # is ok=True so no force needed.
        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 9001

        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["pipeline_id"] == pipeline["id"]
        assert body["source_library_file_id"] == src.id
        assert body["copies"] == 1
        assert body["status"] == "queued"
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["copy_index"] == 0
        assert body["eligibility_overridden"] is False
        # slice_job_id rides on the response so the frontend can call
        # trackJob and render the progress toast.
        assert body["slice_job_id"] == 9001


class TestRunListAndGet:
    """Run history surfaces."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_runs_empty(
        self,
        async_client: AsyncClient,
        pipeline_factory,
    ):
        pipeline = await pipeline_factory()
        resp = await async_client.get(f"/api/v1/slicer-pipelines/{pipeline['id']}/runs")
        assert resp.status_code == 200
        assert resp.json() == {"runs": []}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_run_404(
        self,
        async_client: AsyncClient,
    ):
        resp = await async_client.get("/api/v1/pipeline-runs/99999")
        assert resp.status_code == 404


class TestCancelRun:
    """Cancellation marks the run + linked queue entry."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_unknown_run_404(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/pipeline-runs/99999/cancel")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_marks_queued_run(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
        db_session,
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        live_status = {"connected": True, "raw_data": {"ams": []}}
        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 9001

        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            run_resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id},
            )
        run_id = run_resp.json()["id"]
        cancel_resp = await async_client.post(f"/api/v1/pipeline-runs/{run_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_accepts_archive_source(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        db_session,
    ):
        """``source_archive_id`` is accepted in place of source_library_file_id."""
        from pathlib import Path

        from backend.app.core.config import settings as app_settings
        from backend.app.models.archive import PrintArchive

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)

        rel = "test_pipeline_archive_source.3mf"
        (Path(app_settings.base_dir) / rel).write_bytes(b"")
        archive = PrintArchive(
            printer_id=printer.id,
            filename="Archive Source.3mf",
            file_path=rel,
            file_size=0,
            source_3mf_path=rel,
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)

        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 7777

        live_status = {"connected": True, "raw_data": {"ams": []}}
        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_archive_id": archive.id},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["source_library_file_id"] is None
        assert body["source_archive_id"] == archive.id
        assert body["slice_job_id"] == 7777

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_rejects_no_source(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        resp = await async_client.post(f"/api/v1/slicer-pipelines/{pipeline['id']}/run", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_rejects_both_sources(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id, "source_archive_id": 99},
        )
        assert resp.status_code == 422


class TestCancelTerminal:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_terminal_run_is_idempotent(
        self,
        async_client: AsyncClient,
        pipeline_factory,
        printer_factory,
        library_file_factory,
        db_session,
    ):
        from backend.app.models.pipeline_run import PipelineRun

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        run = PipelineRun(
            pipeline_id=pipeline["id"],
            source_library_file_id=src.id,
            copies=1,
            status="completed",
        )
        db_session.add(run)
        await db_session.commit()
        await db_session.refresh(run)
        resp = await async_client.post(f"/api/v1/pipeline-runs/{run.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"  # unchanged
