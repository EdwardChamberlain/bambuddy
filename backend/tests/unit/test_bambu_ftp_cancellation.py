"""Regression coverage for cancellation-safe FTP printer ownership."""

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


@pytest.mark.asyncio
async def test_cancelled_upload_holds_printer_lock_until_worker_finishes(tmp_path, monkeypatch):
    upload_started = threading.Event()
    release_upload = threading.Event()
    delete_started = threading.Event()

    class FakeFTPClient:
        _mode_cache = {}
        A1_MODELS = set()

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return True

        def upload_file(self, local_path: Path, remote_path: str, progress_callback):
            upload_started.set()
            while not release_upload.wait(0.005):
                pass
            return True

        def delete_file(self, remote_path):
            delete_started.set()
            return ftp_module.DeleteResult.DELETED

        def disconnect(self):
            pass

        @classmethod
        def cache_mode(cls, ip_address, mode):
            pass

    source = tmp_path / "job.3mf"
    source.write_bytes(b"payload")
    monkeypatch.setattr(ftp_module, "BambuFTPClient", FakeFTPClient)
    monkeypatch.setattr(ftp_module, "_upload_deadline", lambda _path: 5.0)
    monkeypatch.setattr(ftp_module, "_UPLOAD_CANCEL_GRACE", 0.01)
    monkeypatch.setattr(ftp_module, "_FTP_CANCEL_GRACE", 0.01)

    upload_task = asyncio.create_task(
        ftp_module.upload_file_async("127.0.0.1", "access-code", source, "/job.3mf", printer_model="X1C")
    )
    await asyncio.wait_for(asyncio.to_thread(upload_started.wait, 1.0), timeout=2.0)
    upload_task.cancel()
    await asyncio.sleep(0.05)
    assert not upload_task.done(), "the queue worker must retain ownership while the FTP thread runs"

    delete_task = asyncio.create_task(
        ftp_module.delete_file_async("127.0.0.1", "access-code", "/job.3mf", printer_model="X1C")
    )
    await asyncio.sleep(0.05)
    assert not delete_started.is_set(), "a delete must not overtake a cancelled upload on the same printer"

    release_upload.set()
    with pytest.raises(asyncio.CancelledError):
        await upload_task
    assert await asyncio.wait_for(delete_task, timeout=1.0) == ftp_module.DeleteResult.DELETED


@pytest.mark.asyncio
async def test_cancelled_delete_holds_printer_lock_until_worker_finishes(tmp_path, monkeypatch):
    delete_started = threading.Event()
    release_delete = threading.Event()
    upload_started = threading.Event()

    class FakeFTPClient:
        _mode_cache = {}
        A1_MODELS = set()

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return True

        def delete_file(self, remote_path):
            delete_started.set()
            while not release_delete.wait(0.005):
                pass
            return ftp_module.DeleteResult.DELETED

        def upload_file(self, local_path: Path, remote_path: str, progress_callback):
            upload_started.set()
            return True

        def disconnect(self):
            pass

        @classmethod
        def cache_mode(cls, ip_address, mode):
            pass

    source = tmp_path / "job.3mf"
    source.write_bytes(b"payload")
    monkeypatch.setattr(ftp_module, "BambuFTPClient", FakeFTPClient)
    monkeypatch.setattr(ftp_module, "_FTP_CANCEL_GRACE", 0.01)

    delete_task = asyncio.create_task(
        ftp_module.delete_file_async("127.0.0.1", "access-code", "/job.3mf", printer_model="X1C")
    )
    await asyncio.wait_for(asyncio.to_thread(delete_started.wait, 1.0), timeout=2.0)
    delete_task.cancel()
    await asyncio.sleep(0.05)
    assert not delete_task.done(), "the queue worker must retain ownership while the FTP thread runs"

    upload_task = asyncio.create_task(
        ftp_module.upload_file_async("127.0.0.1", "access-code", source, "/job.3mf", printer_model="X1C")
    )
    await asyncio.sleep(0.05)
    assert not upload_started.is_set(), "an upload must not overtake a cancelled delete on the same printer"

    release_delete.set()
    with pytest.raises(asyncio.CancelledError):
        await delete_task
    assert await asyncio.wait_for(upload_task, timeout=1.0)
