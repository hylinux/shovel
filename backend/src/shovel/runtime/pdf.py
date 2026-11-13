"""Text-only PDF attachments parsed in a disposable, resource-limited process."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

from shovel.runtime.errors import RuntimeFailure

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_STREAM = 4 * 1024 * 1024
MAX_PDF_MEMORY = 512 * 1024 * 1024
PDF_TIMEOUT = 15
PDF_VERSION = "text-v1"
MAX_EXCERPT = 8000


@dataclass(frozen=True)
class PdfExcerpt:
    text: str
    pages: int
    read_pages: int


def pdf_excerpt(data: bytes) -> PdfExcerpt:
    if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
        raise RuntimeFailure("Invalid or oversized PDF attachment.", status=400)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "shovel.runtime.pdf"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PDF_TIMEOUT,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("PDF parsing exceeded the 15-second limit.", status=413) from exc
    except OSError as exc:
        raise RuntimeFailure("The isolated PDF parser could not start.", status=503) from exc
    if result.returncode != 0:
        raise RuntimeFailure("PDF parsing failed or exceeded its resource limit.", status=413)
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        if "error" in value:
            raise RuntimeFailure(str(value["error"]), status=int(value["status"]))
        text, pages, read_pages = value["text"], value["pages"], value["read_pages"]
        if (
            not isinstance(text, str)
            or len(text) > MAX_EXCERPT + 1
            or not isinstance(pages, int)
            or not isinstance(read_pages, int)
            or not 1 <= read_pages <= pages <= MAX_PDF_PAGES
        ):
            raise ValueError
        return PdfExcerpt(text, pages, read_pages)
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeFailure("The isolated PDF parser returned an invalid result.", status=503) from exc


def _limit_worker() -> int | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x100 | 0x2000  # process memory limit; kill on job close
        limits.process_memory = MAX_PDF_MEMORY
        if not job:
            raise OSError("PDF memory isolation is unavailable.")
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(job)
            raise OSError("PDF memory isolation is unavailable.")
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            kernel.CloseHandle(job)
            raise OSError("PDF memory isolation is unavailable.")
        return int(job)  # Keep the owning handle alive until this child exits.
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MAX_PDF_MEMORY, MAX_PDF_MEMORY))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        return None
    raise OSError("PDF memory isolation is unavailable on this platform.")


def _extract(data: bytes) -> PdfExcerpt:
    from pypdf import PdfReader, apply_configuration
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, PdfObject

    with apply_configuration(
        maximum_declared_stream_length=MAX_PDF_STREAM,
        array_based_stream_maximum_output_length=MAX_PDF_STREAM,
        array_maximum_element_count=20_000,
        dictionary_maximum_element_count=20_000,
        zlib_maximum_output_length=MAX_PDF_STREAM,
        lzw_maximum_output_length=MAX_PDF_STREAM,
        run_length_maximum_output_length=MAX_PDF_STREAM,
        brotli_maximum_output_length=MAX_PDF_STREAM,
        stream_filters_maximum_length=4,
        stream_decoding_work_maximum_length=16 * 1024 * 1024,
        stream_parsing_work_maximum_count=100_000,
        page_tree_maximum_entries=200,
        page_tree_maximum_depth=20,
        page_tree_maximum_page_entries=10_000,
        xform_maximum_invocations_per_extraction=100,
        jbig2dec_binary=None,
        disable_legacy_handling=True,
    ):
        reader = PdfReader(io.BytesIO(data), strict=True, xref_stream_maximum_entries=20_000)
        if reader.is_encrypted:
            raise RuntimeFailure("Encrypted PDFs are not supported.", status=415)
        pages = len(reader.pages)
        if not 1 <= pages <= MAX_PDF_PAGES:
            raise RuntimeFailure("PDF attachments must contain 1 to 50 pages.", status=413)
        pending: list[PdfObject] = [reader.trailer]
        indirect: set[tuple[int, int]] = set()
        visited: set[int] = set()
        work = 0
        forbidden = {
            "/JS",
            "/JavaScript",
            "/EmbeddedFiles",
            "/EF",
            "/OpenAction",
            "/AA",
            "/AcroForm",
            "/XFA",
            "/RichMediaContent",
        }
        actions = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR"}
        while pending:
            work += 1
            if work > 20_000:
                raise RuntimeFailure("PDF object graph exceeds the safety limit.", status=413)
            item = pending.pop()
            if isinstance(item, IndirectObject):
                identity = (item.idnum, item.generation)
                if identity in indirect:
                    continue
                indirect.add(identity)
                resolved = item.get_object()
                if resolved is None:
                    raise RuntimeFailure("Invalid PDF object reference.", status=400)
                item = resolved
            if isinstance(item, (DictionaryObject, ArrayObject)):
                if id(item) in visited:
                    continue
                visited.add(id(item))
                if isinstance(item, DictionaryObject):
                    if (
                        forbidden.intersection(item)
                        or item.get("/S") in actions
                        or item.get("/Type") == "/EmbeddedFile"
                    ):
                        raise RuntimeFailure(
                            "PDF actions, forms and embedded files are not supported.", status=415
                        )
                    pending.extend(item.values())
                else:
                    pending.extend(item)
        output = ""
        has_text = False
        read_pages = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text()
            has_text |= bool(text.strip())
            output += f"{'\n' if output else ''}[Page {number}]\n{text}"[: MAX_EXCERPT + 1 - len(output)]
            read_pages = number
            if len(output) > MAX_EXCERPT:
                break
        if not has_text:
            raise RuntimeFailure(
                "PDF has no extractable text. Scanned PDFs require OCR and are not supported.", status=415
            )
        return PdfExcerpt(output, pages, read_pages)


def _main() -> None:
    try:
        job = _limit_worker()
    except OSError:
        print(json.dumps({"error": "PDF memory isolation is unavailable.", "status": 503}))
        return
    from pypdf.errors import LimitReachedError, PdfReadError

    logging.getLogger("pypdf").disabled = True
    try:
        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
            raise RuntimeFailure("Invalid or oversized PDF attachment.", status=400)
        result: dict[str, Any] = asdict(_extract(data))
    except RuntimeFailure as exc:
        result = {"error": str(exc), "status": exc.status}
    except LimitReachedError, MemoryError:
        result = {"error": "PDF parsing exceeded its resource limit.", "status": 413}
    except (
        PdfReadError,
        ValueError,
        TypeError,
        LookupError,
        RecursionError,
        NotImplementedError,
        OverflowError,
    ):
        result = {"error": "Invalid or unsupported PDF structure.", "status": 400}
    print(json.dumps(result, ensure_ascii=True))
    # Do not close the Windows job handle before the response is flushed.
    del job


if __name__ == "__main__":
    _main()
