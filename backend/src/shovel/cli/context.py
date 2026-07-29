from __future__ import annotations

import os
import platform
from importlib import metadata
from pathlib import Path

from shovel.cli.metadata import PRODUCT


class CliContext:

    def __init__(self):
        """
        根据操作系统读出默认的用户Profile路径
        """
        self.debug = os.environ.get("SHOVEL_DEBUG") == "1"

    # 验证各种初始化的目录, 这里是总的验证
    def validate_enviroment(self) -> None:
        pass


    # 验证整个profile 是否存在

    def validate_profile_exists(self) -> bool:
        profile_dir = self.get_default_profile_dir()

        return Path.exists(profile_dir)


    def validate_config_exists(self) -> bool:
        return Path.exists(self.get_default_config_file())

    def validate_default_worksapce(self) -> bool:
        return Path.exists(self.get_default_workspace())

    def validate_database(self) -> bool:
        return Path.exists(self.get_default_database())

    def validate_logs_dir(self) -> bool:
        return Path.exists(self.get_default_logs_dir())




    def get_os_name(self) -> str:
        system = platform.system()
        release = platform.release()

        if system == "Windows":
            return f"Windows {release}"

        if system == "Darwin":
            return f"macOS {release}"

        if system == "Linux":
            return f"Linux {release}"

        return platform.platform()


    def get_default_profile_dir(self) -> Path:
        home = Path.home()

        return home / ".shovel"

    def get_default_workspace(self) -> Path:
        workspace_dir = self.get_default_worksapce_dir()

        return workspace_dir / "default"

    def get_default_worksapce_dir(self) -> Path:
        profile_dir = self.get_default_profile_dir()

        return profile_dir / "workspaces"

    def get_default_database_dir(self) -> Path:
        profile_dir = self.get_default_profile_dir()

        return profile_dir / "data"

    def get_default_database(self) -> Path:
        db_dir = self.get_default_database_dir()

        return db_dir / "shovel.db"

    def get_default_logs_dir(self) -> Path:
        profile_dir = self.get_default_profile_dir()

        return profile_dir / "logs"

    def get_default_config_file(self) -> Path:
        config_dir = self.get_default_profile_dir() / "config"

        return config_dir / "settings.json"


    def get_package_version(
            self,
            package_name: str,
            default: str = "unknown",
    ) -> str:
        try:
            return metadata.version(package_name)
        except metadata.PackageNotFoundError:
            return default


    def get_shovel_version(self) -> str:
        return self.get_package_version(
            PRODUCT.package_name,
            PRODUCT.default_version,
        )



    def get_generic_host_version(self) -> str:
        return self.get_package_version(
            "py-generic-host",
            "unknown",
        )





cli_context = CliContext()

