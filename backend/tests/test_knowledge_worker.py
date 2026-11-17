from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

from shovel.exceptions.pipeline import KnowledgeIndexUnavailableError, VectorSinkError
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import build_cli_host, build_host, resolve_runtime
from shovel.runtime.knowledge_worker import MAX_FRAME, KnowledgeWorker, read_frame, state_path
from shovel.runtime.locking import KnowledgeWorkerLock, RuntimeLock
from shovel.runtime.schemas import IndexOptimizeInput
from shovel.runtime.shutdown import ShutdownOutcome
from shovel.vector.store import init_vector_store
from test_hosted_runtime import settings_at
from test_index_maintenance import query, record

_FAULT_SCRIPT = """
import os,time,json
from pathlib import Path
import zvec
from shovel.pipeline import sink
from shovel.runtime.knowledge_worker import worker_main
operation = {operation!r}
def notify(root,value):
    marker=root/'entered.tmp'
    marker.write_text(json.dumps(value))
    marker.replace(root/'entered.json')
if operation == 'open':
    original = sink.open_chunk_sink
    def blocked(path, **kwargs):
        root=Path(path).parent
        notify(root,{{'pid':os.getpid(),
            'credential_leaked':bool(os.environ.get('SHOVEL_TEST_WORKER_SECRET'))}})
        while not (root/'release').exists():
            time.sleep(.01)
        return original(path, **kwargs)
    sink.open_chunk_sink=blocked
else:
    original=getattr(zvec.Collection,operation)
    def blocked(self,*args,**kwargs):
        value=original(self,*args,**kwargs)
        # Native work ran, but its final flush/result has not been acknowledged.
        root=Path({root!r})
        notify(root,{{'pid':os.getpid()}})
        while not (root/'release').exists():
            time.sleep(.01)
        return value
    setattr(zvec.Collection,operation,blocked)
worker_main()
"""


def fault(worker: KnowledgeWorker, root: Path, operation: str) -> None:
    worker._command = [
        worker._executable(),
        "-u",
        "-s",
        "-c",
        _FAULT_SCRIPT.format(operation=operation, root=str(root)),
    ]


async def entered(root: Path) -> dict[str, Any]:
    async with asyncio.timeout(25):
        path = root / "entered.json"
        while not path.exists():
            await asyncio.sleep(0.02)
        return json.loads(path.read_text())


async def test_real_worker_owns_complete_lifecycle_and_does_not_receive_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    worker = KnowledgeWorker(settings.zvec.knowledge_path, log_dir=settings.zvec.log_dir)
    monkeypatch.setenv("SHOVEL_TEST_WORKER_SECRET", "must-not-reach-child")
    fault(worker, tmp_path, "open")
    opening = asyncio.create_task(asyncio.to_thread(worker.open, settings.zvec.knowledge_path))
    try:
        identity = await entered(tmp_path)
        assert identity["pid"] != os.getpid()
        assert identity["pid"] == worker.snapshot()["pid"]
        assert identity["credential_leaked"] is False
        with pytest.raises(RuntimeFailure), RuntimeLock(settings):
            pytest.fail("A parent without the worker's ownership cannot start.")
        (tmp_path / "release").touch()
        sink = await opening
        assert await sink.upsert([record()]) == 1
        assert await query(sink) == ["first"]
        assert (await sink.diagnostic_snapshot())["doc_count"] == 1
        result = await sink.optimize(on_dispatch=_acknowledge)
        assert result["after"]["index_completeness"]["dense"] == 1.0
        await sink.delete_document("fixture", "fixture")
        assert await query(sink) == []
        await sink.upsert([record("second")])
        await sink.delete_resource("fixture")
        assert await query(sink) == []
        assert json.loads(state_path(worker.path).read_text())["state"] == "unclean"
        await sink.aclose()
        assert not worker.snapshot()["alive"]
        assert json.loads(state_path(worker.path).read_text())["state"] == "clean"
    finally:
        (tmp_path / "release").touch()
        await asyncio.gather(opening, return_exceptions=True)
        worker.begin_shutdown(asyncio.get_running_loop().time() + 1)
        await worker.aclose()
    with RuntimeLock(settings):
        pass


async def _acknowledge(_before: dict[str, Any]) -> None:
    return None


async def test_host_forces_blocked_open_inside_budget_and_records_unclean_recovery(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    host, provider = build_cli_host(settings)
    host.options.shutdown_timeout = 1
    await host.start_async()
    runtime = await resolve_runtime(provider)
    worker = runtime._knowledge_worker
    fault(worker, tmp_path, "open")
    opening = asyncio.create_task(runtime.sink())
    try:
        await entered(tmp_path)
        before = json.loads(state_path(worker.path).read_text())
        assert before["state"] == "unclean" and before["operation"] == "open"
        started = asyncio.get_running_loop().time()
        await host.stop_async()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.5
        assert not worker.snapshot()["alive"] and worker.forced
        assert host.exit_code == 1 and host.shutdown_outcome == ShutdownOutcome.FAILED
        with RuntimeLock(settings):
            pass
        assert isinstance((await asyncio.gather(opening, return_exceptions=True))[0], Exception)
    finally:
        (tmp_path / "release").touch()
        await asyncio.gather(opening, return_exceptions=True)
        await host.stop_async()
    restarted = KnowledgeWorker(worker.path, log_dir=settings.zvec.log_dir)
    sink = await asyncio.to_thread(restarted.open, worker.path)
    try:
        assert restarted.recovery_required
        await sink.upsert([record()])
        assert await query(sink) == ["first"]
        assert (await sink.diagnostic_snapshot())["doc_count"] == 1
    finally:
        await sink.aclose()
        await restarted.aclose()
    after = json.loads(state_path(worker.path).read_text())
    assert after["state"] == "clean" and after["recovery_required"] is True
    assert after["generation"] != before["generation"]


async def test_lost_upsert_ack_is_not_success_and_idempotent_replay_recovers(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    worker = KnowledgeWorker(settings.zvec.knowledge_path, log_dir=settings.zvec.log_dir)
    fault(worker, tmp_path, "upsert")
    sink = await asyncio.to_thread(worker.open, worker.path)
    write = asyncio.create_task(sink.upsert([record()]))
    try:
        await entered(tmp_path)
        marker = json.loads(state_path(worker.path).read_text())
        assert marker["state"] == "unclean" and marker["operation"] == "upsert"
        worker.begin_shutdown(asyncio.get_running_loop().time() + 0.3)
        with pytest.raises(VectorSinkError):
            await write
        with pytest.raises(KnowledgeIndexUnavailableError):
            await query(sink)
    finally:
        (tmp_path / "release").touch()
        await asyncio.gather(write, return_exceptions=True)
        await worker.aclose()
    recovered = KnowledgeWorker(worker.path, log_dir=settings.zvec.log_dir)
    reopened = await asyncio.to_thread(recovered.open, recovered.path)
    try:
        assert recovered.recovery_required
        await reopened.upsert([record()])
        await reopened.upsert([record()])
        assert (await reopened.diagnostic_snapshot())["doc_count"] == 1
        assert await query(reopened) == ["first"]
    finally:
        await reopened.aclose()
        await recovered.aclose()


async def test_forced_native_maintenance_requires_operation_reconciliation(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    host, provider = build_cli_host(settings)
    await host.start_async()
    runtime = await resolve_runtime(provider)
    fault(runtime._knowledge_worker, tmp_path, "optimize")
    sink = await runtime.sink()
    await sink.upsert([record()])
    operation = await runtime.operations.optimize_index(IndexOptimizeInput(maintenance_window=True))
    running = asyncio.create_task(runtime.operations.run_once(identifier=operation.id))
    try:
        await entered(tmp_path)
        assert (await runtime.operations.get(operation.id)).result["phase"] == "dispatched"
        runtime.begin_shutdown(asyncio.get_running_loop().time() + 0.3)
        await running
        receipt = await runtime.operations.get(operation.id)
        assert receipt.state == "failed" and receipt.result["reconciliation_required"]
        assert receipt.attempts == 1 and receipt.result["phase"] == "dispatched"
        await runtime.operations.recover()
        assert (await runtime.operations.get(operation.id)).state == "failed"
    finally:
        (tmp_path / "release").touch()
        await asyncio.gather(running, return_exceptions=True)
        await host.stop_async()
    assert host.exit_code == 1


def test_protocol_rejects_oversized_and_nonobject_frames_before_allocation() -> None:
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("!I", MAX_FRAME + 1)))
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("!I", 2) + b"[]"))


async def test_parent_death_kills_worker_and_releases_secondary_owner_lock(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    owner = KnowledgeWorker(settings.zvec.knowledge_path, log_dir=settings.zvec.log_dir)
    script = """
import os,sys,time,threading
from pathlib import Path
from shovel.runtime.knowledge_worker import KnowledgeWorker,read_frame,write_frame
from shovel.runtime.process_guard import arm_parent_death
boot=read_frame(sys.stdin.buffer); generation=boot['generation']
arm_parent_death(boot['parent_pid'])
write_frame(sys.stdout.buffer,dict(pid=os.getpid(),generation=generation))
read_frame(sys.stdin.buffer)
child=KnowledgeWorker(boot['path'],log_dir=boot['log_dir'])
child._command=[child._executable(),'-u','-s','-c',CHILD_SCRIPT]
threading.Thread(target=child.open,args=(child.path,)).start()
while not Path(boot['path']).parent.joinpath('entered.json').exists():
    time.sleep(.01)
write_frame(sys.stdout.buffer,dict(generation=generation,sequence=0,recovery_required=False))
read_frame(sys.stdin.buffer)
"""
    script = script.replace("CHILD_SCRIPT", repr(_FAULT_SCRIPT.format(operation="open", root=str(tmp_path))))
    owner._command = [owner._executable(), "-u", "-s", "-c", script]
    opening = asyncio.create_task(asyncio.to_thread(owner.open, owner.path))
    try:
        await entered(tmp_path)
        await opening
        with pytest.raises(OSError), KnowledgeWorkerLock(owner.path):
            pytest.fail("The native child's independent lock must be held.")
        assert owner._process is not None
        owner._process.kill()
        await asyncio.to_thread(owner._process.wait)
        # Keep the outer Job handle open: only the dead owner's Job may reap the grandchild.
        async with asyncio.timeout(3):
            while True:
                try:
                    with KnowledgeWorkerLock(owner.path):
                        break
                except OSError:
                    await asyncio.sleep(0.02)
        assert json.loads(state_path(owner.path).read_text())["state"] == "unclean"
        with RuntimeLock(settings):
            pass
    finally:
        owner.begin_shutdown(asyncio.get_running_loop().time() + 0.5)
        await owner.aclose()


async def test_worker_crash_while_idle_is_not_a_clean_host_shutdown(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    host, provider = build_cli_host(settings)
    await host.start_async()
    runtime = await resolve_runtime(provider)
    sink = await runtime.sink()
    await sink.upsert([record()])
    worker = runtime._knowledge_worker
    assert worker._process is not None
    worker._process.kill()
    await asyncio.to_thread(worker._process.wait)
    await host.stop_async()
    assert worker.shutdown_failed and host.exit_code == 1
    assert json.loads(state_path(worker.path).read_text())["state"] == "unclean"


async def test_native_close_without_ack_never_marks_journal_clean(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    init_vector_store(settings.zvec)
    worker = KnowledgeWorker(settings.zvec.knowledge_path, log_dir=settings.zvec.log_dir)
    fault(worker, tmp_path, "close")
    sink = await asyncio.to_thread(worker.open, worker.path)
    await sink.upsert([record()])
    closing = asyncio.create_task(sink.aclose())
    try:
        await entered(tmp_path)
        worker.begin_shutdown(asyncio.get_running_loop().time() + 0.3)
        with pytest.raises(VectorSinkError):
            await closing
        assert worker.shutdown_failed and not worker.snapshot()["alive"]
        assert json.loads(state_path(worker.path).read_text())["state"] == "unclean"
    finally:
        (tmp_path / "release").touch()
        await asyncio.gather(closing, return_exceptions=True)
        await worker.aclose()


def test_uvicorn_cannot_take_signal_ownership_from_host(tmp_path: Path) -> None:
    import signal

    from py_generic_host.web.uvicorn_service import UvicornHostedService

    host = build_host(settings_at(tmp_path), development=True)
    server = next(service._server for service in host.services if isinstance(service, UvicornHostedService))
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows uses synchronous signal handlers.")
async def test_windows_host_observes_sigint_without_cancelling_main_task(tmp_path: Path) -> None:
    import signal

    from py_generic_host.hosting.stop_reason import StopReason

    settings = settings_at(tmp_path)
    host, _provider = build_cli_host(settings)
    before = signal.getsignal(signal.SIGINT)
    running = asyncio.create_task(host.run_async())
    try:
        async with asyncio.timeout(10):
            while not host._started:
                await asyncio.sleep(0.01)
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(running, 10)
        assert host.stop_reason == StopReason.SIGNAL and host.exit_code == 0
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        await host.stop_async()
        await running
