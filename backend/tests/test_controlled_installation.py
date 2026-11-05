from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import build_controlled_installation as builder

from shovel.identity import native_wam
from shovel.identity.errors import BrokerUnavailableError
from test_native_wam import policy


def package(site: Path, name: str, *, requires: str = "", version: str = "1.0") -> None:
    directory = site / f"{name}-{version}.dist-info"
    directory.mkdir(parents=True)
    (directory / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n" + requires, encoding="utf-8"
    )


def fixture_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    home, site, native = (tmp_path / name for name in ("interpreter", "site", "native"))
    for path in (home / "DLLs", home / "Lib" / "encodings", home / "Lib" / "json", site / "shovel", native):
        path.mkdir(parents=True)
    for name in ("python.exe", "python314.dll", "python3.dll"):
        (home / name).write_bytes(b"synthetic-not-an-executable")
    (home / "Lib" / "encodings" / "__init__.py").write_bytes(b"")
    (home / "Lib" / "json" / "__init__.py").write_bytes(b"")
    for name in (
        "Shovel.Wam.exe",
        "coreclr.dll",
        "hostpolicy.dll",
        "System.Private.CoreLib.dll",
        "PresentationFramework.dll",
    ):
        (native / name).write_bytes(b"synthetic-not-an-executable")
    (native / "Shovel.Wam.runtimeconfig.json").write_text(
        json.dumps(
            {
                "runtimeOptions": {
                    "includedFrameworks": [
                        {"name": "Microsoft.NETCore.App", "version": "10.0.14"},
                        {"name": "Microsoft.WindowsDesktop.App", "version": "10.0.14"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (site / "shovel" / "__init__.py").write_bytes(b"")
    package(site, "shovel", requires="Requires-Dist: dependency>=1\n")
    package(site, "dependency")
    wheel = tmp_path / "shovel.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("shovel/__init__.py", b"")
        # Installers legitimately rewrite RECORD; application bytes must still match exactly.
        archive.writestr("shovel-1.0.dist-info/RECORD", "not-the-installed-record")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version=1\n[[package]]\nname="shovel"\nversion="1.0"\n'
        '[[package]]\nname="dependency"\nversion="1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "sys", SimpleNamespace(platform="win32", version_info=(3, 14)))
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b'["3.14.2",64,"win-amd64"]'),
    )
    return argparse.Namespace(
        python_home=home,
        site_packages=site,
        native=native,
        wheel=wheel,
        lock=lock,
        output=tmp_path / "candidate",
        install_root=r"C:\Program Files\Shovel\packages\1",
        revision=1,
        valid_until=int(time.time()) + 600,
        bindings=None,
    )


def test_offline_builder_inventories_interpreter_dependencies_and_self_contained_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = fixture_inputs(tmp_path, monkeypatch)
    builder.build(args)
    content = json.loads((args.output / "installation-payload.json").read_text(encoding="utf-8"))
    assert content["files"] == builder.inventory(args.output / "payload")
    assert "python\\python314.dll" in content["files"] and "native\\coreclr.dll" in content["files"]
    assert {item["name"] for item in content["runtime"]["distributions"]} == {"shovel", "dependency"}
    assert content["bindings"] == []
    with pytest.raises(ValueError, match="new and disjoint"):
        builder.build(args)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("dependency", "closure"),
        ("lock", "lock"),
        ("wheel", "differs"),
        ("framework", "self-contained"),
        ("interpreter", "incomplete"),
        ("venv", "venv"),
        ("editable", "Editable"),
    ],
)
def test_builder_rejects_incomplete_or_unlocked_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, message: str
) -> None:
    args = fixture_inputs(tmp_path, monkeypatch)
    if damage == "dependency":
        (args.site_packages / "dependency-1.0.dist-info" / "METADATA").write_text(
            "Name: dependency\nVersion: 0.1\n", encoding="utf-8"
        )
    elif damage == "lock":
        args.lock.write_text('[[package]]\nname="shovel"\nversion="1.0"\n', encoding="utf-8")
    elif damage == "wheel":
        (args.site_packages / "shovel" / "__init__.py").write_text("tampered", encoding="utf-8")
    elif damage == "framework":
        (args.native / "Shovel.Wam.runtimeconfig.json").write_text(
            '{"runtimeOptions":{"framework":{"name":"Microsoft.NETCore.App","version":"10.0.14"}}}',
            encoding="utf-8",
        )
    elif damage == "interpreter":
        (args.python_home / "python314.dll").unlink()
    elif damage == "venv":
        (args.python_home / "pyvenv.cfg").write_text("home=outside", encoding="utf-8")
    else:
        (args.site_packages / "shovel-1.0.dist-info" / "direct_url.json").write_text(
            '{"dir_info":{"editable":true}}', encoding="utf-8"
        )
    with pytest.raises(ValueError, match=message):
        builder.build(args)
    assert not (args.output / "installation-payload.json").exists()


def test_host_anchor_generation_change_invalidates_even_unchanged_policy(monkeypatch) -> None:
    original = policy().model_copy(update={"generation": 4})
    current = original.model_dump(by_alias=True)
    monkeypatch.setattr(native_wam, "_installed", (original, lambda: current))
    monkeypatch.setattr(native_wam, "os", SimpleNamespace(name="nt"))
    broker = native_wam.NativeWamBroker()
    assert broker.policy() == original
    current["_generation"] = 5
    with pytest.raises(BrokerUnavailableError):
        broker.policy()


def test_builder_rejects_arm64_interpreter_even_when_pointer_size_is_64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = fixture_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b'["3.14.2",64,"win-arm64"]'),
    )
    with pytest.raises(ValueError, match="x64"):
        builder.build(args)
    assert not args.output.exists()


@pytest.fixture
def bootstrap(monkeypatch):
    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        KEY_READ=2,
        KEY_WOW64_64KEY=4,
        OpenKey=None,
        QueryValueEx=None,
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    path = Path(__file__).parents[2] / "native" / "host_bootstrap.py"
    spec = importlib.util.spec_from_file_location("controlled_bootstrap_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "protected", lambda *args, **kwargs: None)
    return module


def test_bootstrap_requires_bytecode_suppression_before_loading_the_inventory(monkeypatch, bootstrap):
    monkeypatch.setattr(
        bootstrap,
        "sys",
        SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=0), argv=["host"]),
    )
    monkeypatch.setattr(
        bootstrap, "load", lambda: pytest.fail("Inventory load preceded startup flag validation.")
    )
    with pytest.raises(ValueError, match="isolated installed bootstrap"):
        bootstrap.main()


def test_organization_loader_fence_rejects_old_host_after_upgrade_revocation_or_expiry(
    monkeypatch, bootstrap
):
    content = {"_generation": 4, "_digest": "a" * 64, "validUntil": time.time() + 300}
    state = {"generation": 4, "active": {"envelopeSha256": "a" * 64}}
    monkeypatch.setattr(bootstrap, "machine_state", lambda: state)
    bootstrap.require_current_installation(content)
    state["generation"] = 5
    with pytest.raises(ValueError, match="authority changed"):
        bootstrap.require_current_installation(content)
    state["generation"] = 4
    state["active"]["envelopeSha256"] = "b" * 64
    with pytest.raises(ValueError, match="authority changed"):
        bootstrap.require_current_installation(content)
    state["active"]["envelopeSha256"] = "a" * 64
    content["validUntil"] = time.time() - 1
    with pytest.raises(ValueError, match="expired"):
        bootstrap.require_current_installation(content)


def test_bootstrap_registry_read_is_machine_only_and_has_no_legacy_fallback(
    tmp_path: Path, monkeypatch, bootstrap
) -> None:
    calls = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def open_key(hive, name, *, access):
        calls.append((hive, name, access))
        return Key()

    state = {
        "schema": 2,
        "generation": 1,
        "storeRoot": str(tmp_path),
        "highestRevision": 1,
        "signers": ["b" * 64],
        "revokedSigners": [],
        "revokedEnvelopes": [],
        "active": {"revision": 1, "envelopeSha256": "a" * 64, "signerSha256": "b" * 64},
    }

    def query(key, name):
        assert name == "State"
        return json.dumps(state), 1

    monkeypatch.setattr(bootstrap.winreg, "OpenKey", open_key)
    monkeypatch.setattr(bootstrap.winreg, "QueryValueEx", query)
    assert bootstrap.machine_state() == state
    assert calls == [(1, r"SOFTWARE\Shovel\WamTrust", 6)]
    for damage in ("revokedEnvelopes", "revokedSigners", "highestRevision", "active", "schema"):
        saved = copy.deepcopy(state)
        if damage == "revokedEnvelopes":
            state[damage] = ["a" * 64]
        elif damage == "revokedSigners":
            state[damage] = ["b" * 64]
        elif damage == "active":
            state[damage] = None
        else:
            state[damage] = 0
        with pytest.raises(ValueError, match="inactive or revoked"):
            bootstrap.machine_state()
        state = saved
