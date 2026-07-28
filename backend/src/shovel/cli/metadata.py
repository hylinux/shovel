from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata


@dataclass(slots=True, frozen=True, kw_only=True)
class ProductInfo:
    name: str
    package_name: str
    author: str
    description: str
    default_version: str


PRODUCT = ProductInfo(
    name="Shovel",
    package_name="shovel",
    author="HongWei Guo",
    description=(
        "Shovel is a personal AI Agent platform that helps users build, run,"
        "and manage local or cloud-based AI Agents through a unified Web experience."
    ),
    default_version="0.1.0",
)


def get_package_version(package_name: str, default: str = "unknown") -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return default


def get_shovel_version() -> str:
    return get_package_version(
        PRODUCT.package_name,
        PRODUCT.default_version,
    )


def get_generic_host_version() -> str:
    return get_package_version(
        "py-generic-host",
        "unknown",
    )

