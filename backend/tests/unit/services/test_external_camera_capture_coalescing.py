"""Single-flight protection for external-camera one-shot captures."""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import external_camera as module

FRAME = b"\xff\xd8external-frame\xff\xd9"


@pytest.fixture(autouse=True)
def clear_inflight_captures():
    module._inflight_captures.clear()
    yield
    module._inflight_captures.clear()


@pytest.mark.asyncio
async def test_simultaneous_callers_share_one_external_capture(monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = 0

    async def fake_capture(_url, _camera_type, _timeout, _snapshot_url):
        nonlocal calls
        calls += 1
        started.set()
        await gate.wait()
        return FRAME

    monkeypatch.setattr(module, "_capture_frame_uncoalesced", fake_capture)

    leader = asyncio.create_task(module.capture_frame("/dev/video0", "usb"))
    await started.wait()
    follower = asyncio.create_task(module.capture_frame("/dev/video0", "usb"))
    await asyncio.sleep(0)

    assert calls == 1
    gate.set()
    assert await asyncio.gather(leader, follower) == [FRAME, FRAME]


@pytest.mark.asyncio
async def test_different_snapshot_endpoints_do_not_coalesce(monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()
    second_started = asyncio.Event()
    calls: list[str | None] = []

    async def fake_capture(_url, _camera_type, _timeout, snapshot_url):
        calls.append(snapshot_url)
        started.set()
        if len(calls) == 2:
            second_started.set()
        await gate.wait()
        return FRAME

    monkeypatch.setattr(module, "_capture_frame_uncoalesced", fake_capture)

    first = asyncio.create_task(module.capture_frame("http://camera/stream", "mjpeg", snapshot_url="http://camera/a"))
    await started.wait()
    second = asyncio.create_task(module.capture_frame("http://camera/stream", "mjpeg", snapshot_url="http://camera/b"))
    await second_started.wait()

    assert sorted(calls) == ["http://camera/a", "http://camera/b"]
    gate.set()
    assert await asyncio.gather(first, second) == [FRAME, FRAME]


@pytest.mark.asyncio
async def test_follower_retries_after_a_failed_leader(monkeypatch):
    gate = asyncio.Event()
    started = asyncio.Event()
    results = iter((None, FRAME))
    calls = 0

    async def fake_capture(_url, _camera_type, _timeout, _snapshot_url):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await gate.wait()
        return next(results)

    monkeypatch.setattr(module, "_capture_frame_uncoalesced", fake_capture)

    leader = asyncio.create_task(module.capture_frame("/dev/video0", "usb"))
    await started.wait()
    follower = asyncio.create_task(module.capture_frame("/dev/video0", "usb"))
    await asyncio.sleep(0)
    gate.set()

    assert await leader is None
    assert await follower == FRAME
    assert calls == 2


@pytest.mark.asyncio
async def test_completed_capture_is_not_cached(monkeypatch):
    frames = iter((b"first", b"second"))
    calls = 0

    async def fake_capture(_url, _camera_type, _timeout, _snapshot_url):
        nonlocal calls
        calls += 1
        return next(frames)

    monkeypatch.setattr(module, "_capture_frame_uncoalesced", fake_capture)

    assert await module.capture_frame("/dev/video0", "usb") == b"first"
    await asyncio.sleep(0)
    assert await module.capture_frame("/dev/video0", "usb") == b"second"
    assert calls == 2
    assert module._inflight_captures == {}
