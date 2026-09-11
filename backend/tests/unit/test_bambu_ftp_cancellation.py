"""Regression coverage for cooperative cancellation of FTP upload workers."""

import asyncio
import threading
import time
from pathlib import Path

import pytest

import backend.app.services.bambu_ftp as ftp_module


@pytest.mark.asyncio
async def test_upload_file_async_stops_executor_worker_when_cancelled(tmp_path, monkeypatch):
    disconnected = threading.Event()

    class FakeFTPClient:
        _mode_cache = {}
        A1_MODELS = set()

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return True

        def upload_file(self, local_path: Path, remote_path: str, progress_callback):
            while True:
                progress_callback(1, 1)
                time.sleep(0.005)

        def disconnect(self):
            disconnected.set()

        @classmethod
        def cache_mode(cls, ip_address, mode):
            pass

    source = tmp_path / "job.3mf"
    source.write_bytes(b"payload")
    monkeypatch.setattr(ftp_module, "BambuFTPClient", FakeFTPClient)
    monkeypatch.setattr(ftp_module, "_upload_deadline", lambda _path: 5.0)
    monkeypatch.setattr(ftp_module, "_UPLOAD_CANCEL_GRACE", 1.0)

    task = asyncio.create_task(
        ftp_module.upload_file_async(
            "127.0.0.1",
            "access-code",
            source,
            "/job.3mf",
            printer_model="X1C",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert disconnected.wait(timeout=1.0)
