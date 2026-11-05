"""Probe the installed wheel with synthetic data; never certify a trusted installation."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import BadZipFile, ZipFile

import httpx
from pydantic import JsonValue
from scripts.demo_enterprise import SOURCE_TEXT, run_sample
from scripts.enterprise_acceptance import SafeArgumentParser, artifact, source_identity
from scripts.smoke_wheel import smoke

import shovel
from shovel.enterprise.acceptance import OfflineEvidence
from shovel.identity.errors import BrokerUnavailableError
from shovel.identity.native_wam import NativeWamBroker
from shovel.protection.domain import ProtectionDeniedError
from shovel.protection.governance import installed_governance


def verify_wheel(wheel: Path, package: Path) -> None:
    """Bind the exercised package bytes, not just its version, to the candidate wheel."""
    expected = set()
    with ZipFile(wheel) as archive:
        entries = archive.infolist()
        if len(entries) > 100000 or sum(entry.file_size for entry in entries) > 1024**3:
            raise ValueError("Candidate wheel exceeds inventory limits.")
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if not parts or parts[0] != "shovel" or entry.is_dir():
                continue
            if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
                raise ValueError("Unsafe wheel inventory.")
            relative = Path(*parts[1:])
            if relative in expected:
                raise ValueError("Duplicate wheel inventory.")
            expected.add(relative)
            installed = package / relative
            if installed.is_symlink() or any(parent.is_symlink() for parent in installed.parents):
                raise ValueError("Installed package contains a symbolic link.")
            with archive.open(entry) as source, installed.open("rb") as current:
                digest = hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                if digest.digest() != hashlib.file_digest(current, "sha256").digest():
                    raise ValueError("Installed bytes differ from the candidate wheel.")
    actual = {
        path.relative_to(package)
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(package).parts
    }
    if not expected or actual != expected:
        raise ValueError("Installed inventory differs from the candidate wheel.")


async def probe() -> None:
    try:
        NativeWamBroker().policy()
    except BrokerUnavailableError:
        pass
    else:
        raise RuntimeError("The package probe must not acquire installation trust.")
    try:
        installed_governance().policy()
    except ProtectionDeniedError:
        pass
    else:
        raise RuntimeError("The package probe must not acquire storage authority.")

    with TemporaryDirectory(prefix="shovel-enterprise-wheel-") as directory:
        await smoke(Path(directory))

    async def no_http(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("The offline package probe forbids external transport.")

    frames: list[JsonValue] = []
    with patch.object(httpx.AsyncClient, "send", no_http):
        report = await run_sample(frames.append)
    if (
        report["production_ready"] is not False
        or report["synthetic_only"] is not True
        or report["allow"] != "succeeded"
        or report["deny"] != {"state": "failed", "error": "data_use_denied"}
        or report["authentication"]
        != {
            "waiting": "waiting_for_authentication",
            "automatic_resumes": 0,
            "resumed": "succeeded",
            "request": "satisfied",
            "same_deadline": True,
        }
        or frames != [SOURCE_TEXT, SOURCE_TEXT]
    ):
        raise RuntimeError("Installed enterprise contract probe failed.")


def main() -> int:
    parser = SafeArgumentParser(prog="enterprise-wheel-probe", description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package = Path(shovel.__file__).resolve().parent
        if "site-packages" not in package.parts:
            raise ValueError("Run with a wheel-installed interpreter, not an editable checkout.")
        verify_wheel(args.wheel, package)
        asyncio.run(probe())
        commit, clean = source_identity()
        evidence = OfflineEvidence(
            lane="installed_wheel",
            source_commit=commit,
            source_tree_clean=clean,
            artifacts=(artifact(f"python_wheel={args.wheel}"),),
            checks={"packaged_runtime": "passed", "packaged_enterprise": "passed", "default_deny": "passed"},
            tests=3,
            failures=0,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(evidence.model_dump_json(indent=2) + "\n")
        print('{"scope":"enterprise_installed_wheel","passed":true,"production_ready":false}')
        return 0
    except OSError, ValueError, RuntimeError, BadZipFile, AssertionError, httpx.HTTPError:
        print(
            '{"scope":"enterprise_installed_wheel","passed":false,"production_ready":false}', file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
