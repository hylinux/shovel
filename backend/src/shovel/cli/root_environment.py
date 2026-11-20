from __future__ import annotations

import os
import shlex
from pathlib import Path

from shovel.cli.ui.console import console
from shovel.config.errors import ConfigError
from shovel.config.paths import ROOT_ENVIRONMENT


def show_root_environment(root: Path) -> None:
    console.info(f"Selected Shovel data root: {root}")
    if os.name == "nt":
        quoted = str(root).replace("'", "''")
        console.print(f"$env:{ROOT_ENVIRONMENT} = '{quoted}'", markup=False)
        console.print(
            f"[Environment]::SetEnvironmentVariable('{ROOT_ENVIRONMENT}', '{quoted}', 'User')",
            markup=False,
        )
        console.info("The first command sets this terminal; the second persists it for future terminals.")
    else:
        console.print(f"export {ROOT_ENVIRONMENT}={shlex.quote(str(root))}", markup=False)
        console.info("Add this export to your shell profile to retain it in future terminals.")


def persist_root_environment(root: Path) -> None:
    if os.name != "nt":
        raise ConfigError("Automatic root persistence is Windows-only. Use the displayed shell export.")
    import ctypes
    import winreg
    from ctypes import wintypes

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, ROOT_ENVIRONMENT, 0, winreg.REG_SZ, str(root))
    except OSError as exc:
        raise ConfigError(
            f"Initialization completed, but saving the user environment failed ({type(exc).__name__}). "
            "Use the displayed environment commands."
        ) from None

    send = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPCWSTR,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    if not send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)):
        raise ConfigError(
            "The user root variable was saved, but Windows environment refresh failed. "
            "Use the displayed command in this terminal; sign out and back in if new terminals stay stale."
        )
    console.success("SHOVEL_ROOT_PATH saved for your Windows user. Open a new terminal to use it.")
    console.info("This parent terminal is unchanged; use the displayed $env command here.")
