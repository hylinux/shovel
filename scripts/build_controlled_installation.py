"""Assemble an offline Windows runtime for separate organization signing; never install trust."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import ntpath
import os
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path, PureWindowsPath

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def files(root: Path) -> list[Path]:
    result = []
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Reparse points are not installation inputs.")
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError("Reparse points are not installation inputs.")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                result.append(path)
            else:
                raise ValueError("Unsupported installation file.")
            if len(result) > 100000:
                raise ValueError("Installation inventory too large.")
    return result


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(files(root)):
        name = str(PureWindowsPath(path.relative_to(root)))
        parts = PureWindowsPath(name).parts
        if any(part.endswith((" ", ".")) or ntpath.isreserved(part) for part in parts):
            raise ValueError("Ambiguous Windows installation path.")
        with path.open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    if not result or len({name.casefold() for name in result}) != len(result):
        raise ValueError("Empty or case-colliding installation inventory.")
    return result


def distributions(site: Path) -> list[dict[str, str]]:
    entries = []
    installed = {}
    for directory in sorted(site.glob("*.dist-info")):
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Reparse point in dependency metadata.")
        package = importlib.metadata.PathDistribution(directory)
        name = canonicalize_name(package.metadata["Name"])
        if name in installed or not (directory / "METADATA").is_file():
            raise ValueError("Missing or duplicate distribution metadata.")
        installed[name] = package
        entries.append(
            {
                "name": name,
                "version": package.version,
                "metadata": str(
                    PureWindowsPath("python", "Lib", "site-packages", directory.name, "METADATA")
                ),
            }
        )
    if "shovel" not in installed:
        raise ValueError("Installed Shovel wheel is required.")
    for package in installed.values():
        for raw in package.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependency = installed.get(canonicalize_name(requirement.name))
            if dependency is None or not requirement.specifier.contains(dependency.version, prereleases=True):
                raise ValueError("Runtime dependency closure is incomplete.")
    return entries


def verify_wheel(site: Path, wheel: Path) -> None:
    expected = set()
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            relative = PureWindowsPath(item.filename)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or ":" in item.filename
                or "\\" in item.filename
                or any(part.endswith(".data") for part in relative.parts)
            ):
                raise ValueError("Unsupported wheel layout.")
            if relative.parts[0] != "shovel":
                continue
            if relative in expected:
                raise ValueError("Duplicate wheel file.")
            expected.add(relative)
            installed = site.joinpath(*relative.parts)
            if not installed.is_file() or installed.read_bytes() != archive.read(item):
                raise ValueError("Installed application differs from the candidate wheel.")
    actual = {
        PureWindowsPath(path.relative_to(site))
        for path in files(site / "shovel")
        if "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    if not expected or actual != expected:
        raise ValueError("Installed application inventory differs from the wheel.")


def build(args: argparse.Namespace) -> None:
    if sys.platform != "win32" or sys.version_info[:2] < (3, 14):
        raise ValueError("Use Windows x64 Python 3.14+ to assemble the Windows package.")
    sources = [args.python_home.resolve(), args.site_packages.resolve(), args.native.resolve()]
    output = args.output.resolve()
    if output.exists() or any(
        output.is_relative_to(source) or source.is_relative_to(output) for source in sources
    ):
        raise ValueError("Output must be new and disjoint from all input trees.")
    root = PureWindowsPath(args.install_root)
    if (
        not root.is_absolute()
        or str(root).startswith("\\\\")
        or root.name != str(args.revision)
        or root.parent.name != "packages"
        or args.revision < 1
        or args.valid_until <= int(time.time())
    ):
        raise ValueError("Use the exact machine store packages\\<revision> destination.")
    python_home, site, native = sources
    if (python_home / "pyvenv.cfg").exists() or any(python_home.glob("*._pth")):
        raise ValueError("Use a complete standalone interpreter, not a venv or embedded path override.")
    probe = subprocess.run(  # noqa: S603 -- operator-supplied approved local interpreter, never downloaded here
        [
            str(python_home / "python.exe"),
            "-I",
            "-S",
            "-B",
            "-c",
            "import json,struct,sys,sysconfig; "
            "print(json.dumps([sys.version.split()[0],struct.calcsize('P')*8,sysconfig.get_platform()]))",
        ],
        capture_output=True,
        check=True,
        timeout=30,
        env={key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
    )
    version, bits, architecture = json.loads(probe.stdout)
    if bits != 64 or architecture != "win-amd64" or tuple(map(int, version.split(".")[:2])) < (3, 14):
        raise ValueError("Approved interpreter must be Python 3.14+ x64.")
    for source in sources:
        files(source)
    verify_wheel(site, args.wheel)
    packages = distributions(site)
    module_paths = ["python\\Lib", "python\\Lib\\site-packages", "python\\DLLs"]
    dll_directories = []
    if any(item["name"] == "pywin32" for item in packages):
        module_paths.extend(
            [
                "python\\Lib\\site-packages\\win32",
                "python\\Lib\\site-packages\\win32\\lib",
                "python\\Lib\\site-packages\\Pythonwin",
            ]
        )
        dll_directories.append("python\\Lib\\site-packages\\pywin32_system32")
    runtime_config = json.loads((native / "Shovel.Wam.runtimeconfig.json").read_text(encoding="utf-8"))
    frameworks = {
        item["name"]: item["version"]
        for item in runtime_config["runtimeOptions"].get("includedFrameworks", [])
    }
    if not {"Microsoft.NETCore.App", "Microsoft.WindowsDesktop.App"} <= frameworks.keys():
        raise ValueError("Native publish must include the complete self-contained Windows Desktop runtime.")
    lock = args.lock.read_bytes()
    locked = tomllib.loads(lock.decode("utf-8"))
    allowed = {
        (canonicalize_name(package["name"]), package["version"])
        for package in locked["package"]
        if "version" in package
    }
    if any((item["name"], item["version"]) not in allowed for item in packages):
        raise ValueError("Installed distribution is absent from the committed dependency lock.")
    for package in locked["package"]:
        git = package.get("source", {}).get("git")
        if git is not None:
            matches = [item for item in packages if item["name"] == canonicalize_name(package["name"])]
            for item in matches:
                metadata = site / PureWindowsPath(item["metadata"]).parts[-2] / "direct_url.json"
                direct = json.loads(metadata.read_text(encoding="utf-8"))
                if direct.get("vcs_info", {}).get("commit_id") != git.rsplit("#", 1)[-1]:
                    raise ValueError("Installed VCS dependency does not match the locked commit.")
    if any(site.glob("*.egg-link")) or any(
        json.loads(path.read_text(encoding="utf-8")).get("dir_info", {}).get("editable")
        for path in site.glob("*.dist-info/direct_url.json")
    ):
        raise ValueError("Editable dependencies are not controlled installation inputs.")
    output.mkdir(parents=True)
    payload = output / "payload"
    payload.mkdir()

    def interpreter_ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(shutil.ignore_patterns("__pycache__", "*.pyc")(directory, names))
        if Path(directory) == python_home / "Lib":
            ignored.add("site-packages")
        return ignored

    shutil.copytree(python_home, payload / "python", ignore=interpreter_ignore)
    installed_site = payload / "python" / "Lib" / "site-packages"
    if installed_site.exists() and any(installed_site.iterdir()):
        raise ValueError("Interpreter must not contain a second dependency installation.")
    shutil.copytree(
        site, installed_site, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copytree(native, payload / "native")
    shutil.copyfile(
        Path(__file__).parent.parent / "native" / "host_bootstrap.py", payload / "host_bootstrap.py"
    )
    installed_files = inventory(payload)
    python_dll = f"python\\python{version.split('.')[0]}{version.split('.')[1]}.dll"
    required = [
        python_dll,
        "python\\python3.dll",
        "python\\Lib\\encodings\\__init__.py",
        "python\\Lib\\json\\__init__.py",
        "native\\Shovel.Wam.exe",
        "native\\coreclr.dll",
        "native\\hostpolicy.dll",
        "native\\System.Private.CoreLib.dll",
        "native\\PresentationFramework.dll",
    ]
    if any(name not in installed_files for name in required) or not (payload / "python" / "DLLs").is_dir():
        raise ValueError("Interpreter, standard library or native runtime is incomplete.")
    bindings = json.loads(args.bindings.read_text(encoding="utf-8")) if args.bindings else []
    manifest = {
        "root": str(root),
        "python": "python\\python.exe",
        "native": "native\\Shovel.Wam.exe",
        "bootstrap": "host_bootstrap.py",
        "modulePaths": module_paths,
        "files": installed_files,
        "bindings": bindings,
        "revision": args.revision,
        "validUntil": args.valid_until,
        "runtime": {
            "pythonVersion": version,
            "pythonArchitecture": architecture,
            "pythonDll": python_dll,
            "standardLibrary": "python\\Lib",
            "sitePackages": "python\\Lib\\site-packages",
            "dotnetVersion": frameworks["Microsoft.NETCore.App"],
            "lockSha256": hashlib.sha256(lock).hexdigest(),
            "distributions": packages,
            "dllDirectories": dll_directories,
        },
    }
    with (output / "installation-payload.json").open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=True, separators=(",", ":"))
        file.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python-home", "site-packages", "native", "wheel", "lock", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--valid-until", type=int, required=True)
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args()
    try:
        build(args)
    except OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile:
        print(
            '{"scope":"controlled_installation_build","status":"blocked","production_ready":false}',
            file=sys.stderr,
        )
        return 3
    print('{"scope":"controlled_installation_build","status":"candidate","production_ready":false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
