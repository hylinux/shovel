from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from shovel.cli import app
from shovel.cli.commands import init as init_command
from shovel.config.errors import ConfigError
from shovel.config.paths import resolve_root_path, root_path_scope
from shovel.config.settings import AppSettings, load_settings
from shovel.runtime.client import workspace_id
from shovel.runtime.locking import RuntimeLock, endpoint_file
from shovel.runtime.runtime_config import RuntimeConfigInput, RuntimeConfigurationService
from shovel.services.config_server import ShovelContext
from shovel.vector.store import init_vector_store
from test_init_command import _answers
from test_init_command import profile as profile


def test_root_defaults_have_no_io_and_scopes_restore_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("SHOVEL_ROOT_PATH", raising=False)
    default = AppSettings.from_file_data({})
    assert default.root_path == tmp_path / "home" / ".shovel"
    assert not (tmp_path / "home").exists()
    selected = tmp_path / "custom root"
    monkeypatch.setenv("SHOVEL_ROOT_PATH", str(selected))
    settings = AppSettings.from_file_data({})
    assert settings.agent.workspace == selected / "workspaces" / "default"
    assert settings.database.url.endswith((selected / "data" / "shovel.db").as_posix())
    assert settings.zvec.knowledge_path == (selected / "knowledge" / "shovel_chunk").as_posix()
    assert settings.zvec.log_dir == (selected / "logs").as_posix()
    assert settings.memory.index_path == selected / "memory" / "zvec"
    assert settings.temp_path == selected / "tmp"
    assert "root_path" not in settings.model_dump()
    assert not selected.exists()
    with pytest.raises(RuntimeError), root_path_scope(tmp_path / "override"):
        assert ShovelContext().get_default_config_file() == tmp_path / "override" / "config" / "settings.toml"
        raise RuntimeError("scope interrupted")
    assert resolve_root_path() == selected
    monkeypatch.setenv("SHOVEL_ROOT_PATH", str(tmp_path / "other"))
    assert settings.root_path == selected


@pytest.mark.parametrize("value", ["relative-root", "bad\0root"])
def test_invalid_root_environment_fails_without_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    if "\0" in value:
        with pytest.raises(ConfigError):
            resolve_root_path(value)
        return
    monkeypatch.setenv("SHOVEL_ROOT_PATH", value)
    with pytest.raises(ConfigError, match="absolute"):
        resolve_root_path()


def test_file_root_and_file_ancestor_are_rejected(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("not a directory")
    for path in (file, file / "child"):
        with pytest.raises(ConfigError):
            resolve_root_path(path)


def test_custom_init_is_discoverable_from_a_fresh_process_and_keeps_all_paths_under_root(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = profile.parent / "custom space \u6839\u76ee\u5f55"
    monkeypatch.setattr(init_command, "init_vector_store", init_vector_store)
    persistence = Mock()
    monkeypatch.setattr(init_command, "persist_root_environment", persistence)
    monkeypatch.setenv("SHOVEL_ROOT_PATH", str(profile.parent / "ignored-environment"))
    result = CliRunner().invoke(app, ["init", "--root-path", str(root), "--no-input"])
    assert result.exit_code == 0, result.output
    assert not profile.exists()
    assert not (profile.parent / "ignored-environment").exists()
    persistence.assert_not_called()
    config = root / "config" / "settings.toml"
    raw = tomllib.loads(config.read_text())
    settings = AppSettings.load(config, root_path=root)
    assert settings.agent.workspace == root / "workspaces" / "default"
    assert settings.database.url.endswith((root / "data" / "shovel.db").as_posix())
    assert Path(settings.zvec.knowledge_path) == root / "knowledge" / "shovel_chunk"
    assert settings.memory.index_path == root / "memory" / "shovel_memory"
    assert Path(settings.zvec.log_dir) == root / "logs"
    assert settings.temp_path == root / "tmp"
    assert "root_path" not in raw
    env = {**os.environ, "SHOVEL_ROOT_PATH": str(root)}
    code = (
        "from shovel.services.config_server import ShovelContext; "
        "from shovel.config.settings import load_settings; "
        "c=ShovelContext(); s=load_settings(c.get_default_config_file()); "
        "assert s.root_path == c.get_default_profile_dir(); "
        "assert s.config_path == c.get_default_config_file(); print('discovered')"
    )
    child = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=profile.parent, capture_output=True, text=True, timeout=30
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "discovered"
    monkeypatch.setenv("SHOVEL_ROOT_PATH", str(root))
    checked = CliRunner().invoke(app, ["config", "--check"])
    assert checked.exit_code == 0, checked.output


def test_prompt_root_precedes_model_questions_and_cancel_creates_nothing(profile: Path) -> None:
    root = profile.parent / "prompt-selected"
    result = CliRunner().invoke(app, ["init"], input=str(root) + "\n")
    assert result.exit_code != 0
    assert "Initialization cancelled" in result.output
    assert result.output.index("Shovel initialization root") < result.output.index("Model provider")
    assert not root.exists() and not profile.exists()


def test_interactive_custom_root_and_invalid_path_reprompt(profile: Path) -> None:
    file = profile.parent / "occupied"
    file.write_text("file")
    root = profile.parent / "interactive-root"
    result = CliRunner().invoke(app, ["init"], input=str(file) + "\n" + str(root) + _answers() + "n\n")
    assert result.exit_code == 0, result.output
    assert "file instead of a directory" in result.output
    assert (root / "config" / "settings.toml").is_file()
    assert not profile.exists()


def test_persistence_only_runs_after_success_and_only_when_requested(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = profile.parent / "persistent"
    calls: list[Path] = []

    def persist(selected: Path) -> None:
        assert (selected / "config" / "settings.toml").is_file()
        assert (selected / "data" / "shovel.db").is_file()
        calls.append(selected)

    monkeypatch.setattr(init_command, "persist_root_environment", persist)
    result = CliRunner().invoke(app, ["init", "--root-path", str(root), "--no-input", "--persist-root"])
    assert result.exit_code == 0, result.output
    assert calls == [root]
    cancelled = CliRunner().invoke(
        app, ["init", "--root-path", str(profile.parent / "cancelled"), "--persist-root"], input=""
    )
    assert cancelled.exit_code != 0
    assert calls == [root]


def test_editor_keeps_actual_config_file_and_missing_defaults_after_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    unrelated = tmp_path / "arbitrary-config.toml"
    unrelated.write_text('log_level = "INFO"\n[agent]\nworkspace_path = "explicit-workspace"\n')
    active = load_settings(unrelated, root_path=root)
    service = RuntimeConfigurationService(active)
    assert active.agent.workspace_path == Path("explicit-workspace")
    monkeypatch.setenv("SHOVEL_ROOT_PATH", str(tmp_path / "wrong-root"))
    assert service.file.path == unrelated
    view = service.read()
    assert view["applied"] and view["editable"]
    service.save(RuntimeConfigInput(revision=view["revision"], changes={"log_level": "DEBUG"}))
    assert AppSettings.load(unrelated, root_path=root).log_level == "DEBUG"
    assert not (tmp_path / "wrong-root").exists()
    assert not root.exists()


def test_different_roots_have_independent_discovery_and_same_store_still_locks(tmp_path: Path) -> None:
    first = AppSettings.from_file_data({}, root_path=tmp_path / "one")
    second = AppSettings.from_file_data({}, root_path=tmp_path / "two")
    assert workspace_id(first) != workspace_id(second)
    assert endpoint_file(first) != endpoint_file(second)
    with RuntimeLock(first), RuntimeLock(second):
        from shovel.runtime.errors import RuntimeFailure

        with pytest.raises(RuntimeFailure, match="Host"), RuntimeLock(first):
            pass


def test_initialization_permission_failure_does_not_create_or_persist(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shovel.config.paths as paths

    persist = Mock()
    monkeypatch.setattr(init_command, "persist_root_environment", persist)
    monkeypatch.setattr(paths.os, "access", lambda *_: False)
    root = profile.parent / "unwritable"
    result = CliRunner().invoke(app, ["init", "--root-path", str(root), "--no-input", "--persist-root"])
    assert result.exit_code != 0 and "not writable" in result.output
    assert not root.exists() and not profile.exists()
    persist.assert_not_called()


def test_database_initialization_creates_default_parent_only_when_used(tmp_path: Path) -> None:
    from shovel.db.database import init_database

    root = tmp_path / "new-root"
    settings = AppSettings.from_file_data({}, root_path=root)
    assert not root.exists()
    assert init_database(settings.database.url)
    assert (root / "data" / "shovel.db").is_file()
    assert not (root / "knowledge").exists()


async def test_two_one_shot_hosts_keep_database_and_configuration_isolated(tmp_path: Path) -> None:
    from sqlalchemy import select

    from shovel.db.model.runtime import Operation
    from shovel.runtime.hosting import build_cli_host, resolve_runtime
    from shovel.runtime.schemas import ResourceInput

    settings = [
        AppSettings.from_file_data(
            {
                "zvec": {"dim": 4},
                "memory": {"dim": 4},
                "performance": {"warm_index": False, "warm_local_models": False},
            },
            root_path=tmp_path / name,
        )
        for name in ("first", "second")
    ]
    for configured in settings:
        configured.save(configured.root_path / "config" / "settings.toml")
        init_vector_store(configured.zvec)
    first, first_provider = build_cli_host(settings[0])
    second, second_provider = build_cli_host(settings[1])
    try:
        await first.start_async()
        await second.start_async()
        one = await resolve_runtime(first_provider)
        two = await resolve_runtime(second_provider)
        assert one.configuration.file.path == settings[0].config_path
        assert two.configuration.file.path == settings[1].config_path
        created = await one.operations.create_resource(
            ResourceInput(name="isolated", connector_kind="local_fs", config={"root_path": str(tmp_path)})
        )
        async with two.sessions() as session:
            assert await session.scalar(select(Operation.id).where(Operation.id == created.id)) is None
        async with one.sessions() as session:
            assert await session.scalar(select(Operation.id).where(Operation.id == created.id)) == created.id
    finally:
        second.lifetime.stop_application()
        first.lifetime.stop_application()
        await second.stop_async()
        await first.stop_async()


@pytest.mark.skipif(os.name != "nt", reason="Windows user environment persistence")
@pytest.mark.parametrize("failure", ["none", "write", "refresh"])
def test_windows_persistence_is_user_scoped_and_failures_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import winreg

    from shovel.cli.root_environment import persist_root_environment

    opened = Mock()
    opened.__enter__ = Mock(return_value="user-key")
    opened.__exit__ = Mock(return_value=False)
    create = Mock(return_value=opened)
    if failure == "write":
        create.side_effect = PermissionError("synthetic denied write")
    write = Mock()
    send = Mock(return_value=0 if failure == "refresh" else 1)
    library = Mock()
    library.SendMessageTimeoutW = send
    monkeypatch.setattr(winreg, "CreateKeyEx", create)
    monkeypatch.setattr(winreg, "SetValueEx", write)
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=library))
    original = os.environ.get("SHOVEL_ROOT_PATH")
    root = tmp_path / "chosen"
    if failure == "none":
        persist_root_environment(root)
    else:
        with pytest.raises(
            ConfigError, match="saving the user environment failed" if failure == "write" else "saved"
        ):
            persist_root_environment(root)
    create.assert_called_once_with(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
    if failure != "write":
        write.assert_called_once_with("user-key", "SHOVEL_ROOT_PATH", 0, winreg.REG_SZ, str(root))
        send.assert_called_once()
    else:
        write.assert_not_called()
        send.assert_not_called()
    assert os.environ.get("SHOVEL_ROOT_PATH") == original
