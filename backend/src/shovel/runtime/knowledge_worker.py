"""Private, bounded JSON IPC for a single-owner knowledge collection process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import struct
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from time import perf_counter, process_time
from typing import IO, TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from shovel.exceptions.pipeline import VectorSinkError
from shovel.runtime.locking import KnowledgeWorkerLock
from shovel.runtime.process_guard import arm_parent_death, close_kill_job, create_kill_job

if TYPE_CHECKING:
    from shovel.pipeline.sink import IndexOpenTimings, ZvecChunkSink

MAX_FRAME = 32 * 1024 * 1024
_MUTATIONS = {"upsert", "delete", "optimize"}
_OPERATIONS = _MUTATIONS | {"metadata", "search", "close"}


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        block = stream.read(size - len(parts))
        if not block:
            raise EOFError("Knowledge worker channel closed.")
        parts.extend(block)
    return bytes(parts)


def read_frame(stream: IO[bytes]) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(stream, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("Knowledge worker frame exceeds its limit.")
    value = json.loads(_read_exact(stream, size))
    if not isinstance(value, dict):
        raise ValueError("Invalid knowledge worker envelope.")
    return value


def write_frame(stream: IO[bytes], value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_FRAME:
        raise ValueError("Knowledge worker frame exceeds its limit.")
    stream.write(struct.pack("!I", len(data)) + data)
    stream.flush()


def state_path(collection: str | Path) -> Path:
    path = Path(collection).expanduser().resolve()
    return path.with_name(path.name + ".worker-state.json")


def _persist_state(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if sys.platform == "linux":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class KnowledgeWorker:
    def __init__(self, path: str, *, log_dir: str, memory_mb: int = 4096) -> None:
        self.path = str(Path(path).expanduser().resolve())
        self.log_dir = str(Path(log_dir).expanduser().resolve() / "knowledge-worker")
        self.memory_mb = memory_mb
        self.generation = uuid4().hex
        self._process: subprocess.Popen[bytes] | None = None
        self._job: int | None = None
        self._state_lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        self._sequence = 0
        self._closing = False
        self._stop_task: asyncio.Task[None] | None = None
        self._deadline: float | None = None
        self._phase = "unopened"
        self._command = [self._executable(), "-u", "-s", "-m", __name__]
        self.forced = False
        self.interrupted = False
        self.recovery_required = False
        self._opened = False
        self._clean_exit = False

    @property
    def shutdown_failed(self) -> bool:
        return self.forced or self.interrupted or (self._opened and not self._clean_exit)

    @staticmethod
    def _executable() -> str:
        # Match multiprocessing's Windows venv launcher bypass: PID must be the interpreter.
        if sys.platform == "win32" and sys.prefix != sys.base_prefix:
            return str(getattr(sys, "_base_executable", sys.executable))
        return sys.executable

    def snapshot(self) -> dict[str, Any]:
        process = self._process
        return {
            "pid": process.pid if process else None,
            "generation": self.generation,
            "state": self._phase,
            "alive": process is not None and process.poll() is None,
            "forced": self.forced,
            "interrupted": self.interrupted,
            "recovery_required": self.recovery_required,
            "clean_exit": self._clean_exit,
        }

    def _start(self) -> None:
        with self._state_lock:
            if self._closing:
                raise VectorSinkError("Knowledge worker is stopping.")
            if self._process is not None:
                self.generation = uuid4().hex
                self._sequence = 0
            self._opened = self._clean_exit = False
        environment = {
            key: value
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "SystemDrive")
            if (value := os.environ.get(key)) is not None
        }
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        environment["PYTHONUTF8"] = "1"
        if sys.platform == "win32" and sys.prefix != sys.base_prefix:
            environment["__PYVENV_LAUNCHER__"] = sys.executable
        process = subprocess.Popen(  # noqa: S603  # Trusted module; IPC never selects executable code.
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=Path(__file__).resolve().parents[2],
            creationflags=int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            start_new_session=sys.platform != "win32",
        )
        with self._state_lock:
            self._process = process
            if self._closing:
                process.kill()
                process.wait()
                raise VectorSinkError("Knowledge worker stopped before dispatch.")
            if sys.platform == "win32":
                self._job = create_kill_job(process.pid, memory_bytes=(self.memory_mb + 512) * 1024 * 1024)
        self._phase = "opening"
        assert process.stdin is not None and process.stdout is not None
        write_frame(
            process.stdin,
            {
                "generation": self.generation,
                "parent_pid": os.getpid(),
                "path": self.path,
                "log_dir": self.log_dir,
                "memory_mb": self.memory_mb,
            },
        )
        hello = read_frame(process.stdout)
        if hello != {"pid": process.pid, "generation": self.generation}:
            raise ValueError("Knowledge worker identity mismatch.")
        write_frame(process.stdin, {"generation": self.generation, "start": True})

    def open(self, path: str, *, measurement: IndexOpenTimings | None = None) -> ZvecChunkSink:
        from shovel.pipeline.process_sink import ProcessChunkSink

        if str(Path(path).expanduser().resolve()) != self.path:
            raise ValueError("Knowledge worker collection identity changed.")
        with self._rpc_lock:
            if self._process is not None and self._process.poll() is None:
                raise VectorSinkError("Knowledge worker is already open.")
            if self.interrupted or self.forced:
                raise VectorSinkError("Restart the Host to recover the interrupted knowledge worker.")
            try:
                if measurement is not None:
                    measurement.phase = "process_start"
                self._start()
                if measurement is not None:
                    measurement.phase = "process_open"
                assert self._process is not None and self._process.stdout is not None
                response = read_frame(self._process.stdout)
                self._validate(response, 0, measurement=measurement)
                self._opened = True
                self._phase = "ready"
                if self.recovery_required:
                    structlog.get_logger("shovel.knowledge").warning(
                        "knowledge.worker.recovery_required", generation=self.generation
                    )
                return ProcessChunkSink(self)
            except BaseException as exc:
                self.interrupted |= not isinstance(exc, (FileNotFoundError, VectorSinkError))
                self.recovery_required |= self.interrupted
                self._terminate()
                raise

    def _validate(
        self,
        response: dict[str, Any],
        sequence: int,
        *,
        measurement: IndexOpenTimings | None = None,
    ) -> None:
        if response.get("generation") != self.generation or response.get("sequence") != sequence:
            raise ValueError("Knowledge worker response identity mismatch.")
        if measurement is not None and isinstance(timings := response.get("timings"), dict):
            for key in (
                "import_ms",
                "runtime_ms",
                "collection_ms",
                "collection_cpu_ms",
                "worker_ms",
                "process_cpu_ms",
                "phase",
            ):
                setattr(measurement, key, timings[key])
        self.recovery_required |= bool(response.get("recovery_required", False))
        error = response.get("error")
        if error is not None:
            if error == "FileNotFoundError":
                raise FileNotFoundError("Knowledge collection is unavailable.")
            raise VectorSinkError(f"Knowledge worker rejected an operation ({error}).")

    def call(self, operation: str, payload: dict[str, Any]) -> Any:
        if operation not in _OPERATIONS:
            raise ValueError("Unsupported knowledge worker operation.")
        with self._rpc_lock:
            process = self._process
            if process is None or process.poll() is not None:
                self.interrupted = True
                self.recovery_required = True
                raise VectorSinkError("Knowledge worker exited; native completion is unknown.")
            assert process.stdin is not None and process.stdout is not None
            self._sequence += 1
            self._phase = operation
            try:
                write_frame(
                    process.stdin,
                    {
                        "generation": self.generation,
                        "sequence": self._sequence,
                        "operation": operation,
                        "payload": payload,
                    },
                )
                response = read_frame(process.stdout)
                self._validate(response, self._sequence)
                return response["result"]
            except (OSError, EOFError, ValueError) as exc:
                self.interrupted = True
                self.recovery_required = True
                self._terminate()
                raise VectorSinkError("Knowledge worker channel failed; reconcile native work.") from exc
            finally:
                if process.poll() is None:
                    self._phase = "ready"

    def begin_shutdown(self, deadline: float) -> None:
        with self._state_lock:
            self._closing = True
        if self._deadline is None or deadline < self._deadline:
            self._deadline = deadline
            if self._stop_task is not None:
                self._stop_task.cancel()
            self._stop_task = asyncio.create_task(self._stop_at(deadline), name="knowledge-worker-deadline")

    async def _stop_at(self, deadline: float) -> None:
        now = asyncio.get_running_loop().time()
        reserve = min(5.0, max(0.0, deadline - now) * 0.2)
        await asyncio.sleep(max(0.0, deadline - reserve - now))
        with self._state_lock:
            process = self._process
            if process is not None and process.poll() is None:
                self.forced = self.interrupted = True
                self.recovery_required = True
                self._phase = "forced"
                structlog.get_logger("shovel.knowledge").error(
                    "knowledge.worker.forced_stop", pid=process.pid, generation=self.generation
                )
                process.kill()
        while process is not None and process.poll() is None:
            if asyncio.get_running_loop().time() >= deadline:
                structlog.get_logger("shovel.knowledge").error("knowledge.worker.reap.over_budget")
                break
            await asyncio.sleep(0.01)
        # Never release the owner's resources while a killed child is still alive.
        if process is not None and process.poll() is None:
            await asyncio.to_thread(process.wait)

    def _terminate(self) -> None:
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
        self._release_handles()

    def _release_handles(self) -> None:
        if self._job is not None:
            close_kill_job(self._job)
            self._job = None
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream is not None:
                    stream.close()

    def close(self) -> None:
        process = self._process
        try:
            if process is not None and process.poll() is None:
                self.call("close", {})
                process.wait()
                if process.returncode != 0 or self.forced:
                    self.interrupted = True
                    raise VectorSinkError("Knowledge worker did not exit cleanly.")
                with KnowledgeWorkerLock(self.path):
                    marker = state_path(self.path)
                    state = json.loads(marker.read_text(encoding="utf-8"))
                    if state.get("generation") != self.generation:
                        raise VectorSinkError("Knowledge worker recovery generation changed.")
                    state["state"] = "clean"
                    _persist_state(marker, state)
                    self._clean_exit = True
            elif process is not None and self._opened and not self._clean_exit:
                self.interrupted = True
                self.recovery_required = True
        finally:
            if process is None or process.poll() is not None:
                self._release_handles()
                self._phase = "closed"

    async def aclose(self) -> None:
        from shovel.runtime.shutdown import drain_task

        try:
            if self._process is not None:
                await drain_task(asyncio.create_task(asyncio.to_thread(self.close)))
        except BaseException:
            if self._process is not None and self._process.poll() is None:
                self.forced = self.interrupted = True
                await drain_task(asyncio.create_task(asyncio.to_thread(self._terminate)))
            raise
        finally:
            if self._stop_task is not None:
                self._stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._stop_task


async def _dispatch(sink: ZvecChunkSink, operation: str, payload: dict[str, Any]) -> Any:
    from zvec import Query

    from shovel.pipeline.sink import VectorRecord
    from shovel.vector.schema import DENSE_VECTOR

    if operation == "metadata":
        return await sink.diagnostic_snapshot()
    if operation == "upsert":
        return await sink.upsert([VectorRecord(**record) for record in payload["records"]])
    if operation == "delete":
        return await sink._delete(payload["filter"])
    if operation == "search":
        from shovel.pipeline.sink import VectorSearchTimings

        timings = VectorSearchTimings()
        rows = await sink._io(
            lambda: sink._collection.query(
                queries=Query(field_name=DENSE_VECTOR, vector=payload["vector"]),
                topk=payload["limit"],
                filter=payload["filter"],
                output_fields=["resource_id"],
            ),
            measurement=timings,
        )
        return {"rows": [[str(row.id), float(row.score or 0)] for row in rows], "timings": asdict(timings)}
    if operation == "optimize":

        async def admitted(_before: dict[str, Any]) -> None:
            return None

        return await sink.optimize(concurrency=payload["concurrency"], on_dispatch=admitted)
    if operation == "close":
        await sink.aclose()
        return None
    raise ValueError("Unsupported knowledge worker operation.")


def worker_main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    reader = sys.stdin.buffer
    # Native libraries may write to stdout. Keep the protocol on a private duplicate.
    writer = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    from shovel.pipeline.sink import IndexOpenTimings, open_chunk_sink
    from shovel.vector.store import init_zvec_runtime

    boot = read_frame(reader)
    generation = boot["generation"]
    arm_parent_death(boot["parent_pid"])
    write_frame(writer, {"pid": os.getpid(), "generation": generation})
    if read_frame(reader) != {"generation": generation, "start": True}:
        raise ValueError("Knowledge worker was not admitted.")
    loop = asyncio.new_event_loop()
    sink = None
    sequence = 0
    marker = state_path(boot["path"])
    timings = IndexOpenTimings(host_id=generation, reason="on_demand")
    with KnowledgeWorkerLock(boot["path"]):
        previous = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        recovery_required = previous.get("state") == "unclean" or previous.get("recovery_required", False)
        state = {
            "version": 1,
            "generation": generation,
            "pid": os.getpid(),
            "state": "unclean",
            "operation": "open",
            "sequence": sequence,
            "recovery_required": recovery_required,
        }
        _persist_state(marker, state)
        try:
            started, cpu = perf_counter(), process_time()
            init_zvec_runtime(
                log_dir=boot["log_dir"],
                query_threads=2,
                optimize_threads=2,
                memory_limit_mb=boot["memory_mb"],
            )
            runtime_ms, runtime_cpu_ms = (perf_counter() - started) * 1000, (process_time() - cpu) * 1000
            sink = open_chunk_sink(boot["path"], measurement=timings)
            timings.runtime_ms = (timings.runtime_ms or 0) + runtime_ms
            timings.worker_ms = (timings.worker_ms or 0) + runtime_ms
            timings.process_cpu_ms = (timings.process_cpu_ms or 0) + runtime_cpu_ms
            # Opening validates native metadata; retained recovery_required is not a full integrity claim.
            loop.run_until_complete(sink.diagnostic_snapshot())
            write_frame(
                writer,
                {
                    "generation": generation,
                    "sequence": 0,
                    "timings": asdict(timings),
                    "recovery_required": recovery_required,
                },
            )
            while True:
                request = read_frame(reader)
                if request.get("generation") != generation or request.get("sequence") != sequence + 1:
                    raise ValueError("Stale knowledge worker request.")
                sequence += 1
                operation = request["operation"]
                if operation not in _OPERATIONS or not isinstance(request.get("payload"), dict):
                    raise ValueError("Invalid knowledge worker operation.")
                if operation in _MUTATIONS | {"close"}:
                    state.update(operation=operation, sequence=sequence)
                    _persist_state(marker, state)
                response = {
                    "generation": generation,
                    "sequence": sequence,
                    "recovery_required": recovery_required,
                }
                try:
                    response["result"] = loop.run_until_complete(
                        _dispatch(sink, operation, request["payload"])
                    )
                except Exception as exc:
                    if operation in _MUTATIONS:
                        recovery_required = True
                        state["recovery_required"] = True
                        _persist_state(marker, state)
                    response.update(error=type(exc).__name__, recovery_required=recovery_required)
                write_frame(writer, response)
                if operation == "close":
                    break
        except Exception as exc:
            write_frame(
                writer,
                {
                    "generation": generation,
                    "sequence": sequence,
                    "error": type(exc).__name__,
                    "timings": asdict(timings),
                    "recovery_required": recovery_required,
                },
            )
        finally:
            if sink is not None:
                loop.run_until_complete(sink.aclose())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()


if __name__ == "__main__":
    worker_main()
