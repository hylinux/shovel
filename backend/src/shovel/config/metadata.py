from __future__ import annotations

from dataclasses import dataclass


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
