import re
from types import MappingProxyType

from shovel.enterprise.domain import ProviderManifest
from shovel.runtime.errors import RuntimeFailure


def _release_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        raise RuntimeFailure("Installed provider compatibility requires a stable major.minor.patch version.")
    return int(match[1]), int(match[2]), int(match[3])


class InstalledProviderCatalog:
    """Metadata supplied by trusted installation/DI, never by commands, TOML or SQLite."""

    def __init__(self, manifests: tuple[ProviderManifest, ...] = (), *, runtime_version: str) -> None:
        release = _release_version(runtime_version)
        providers = {}
        for supplied in manifests:
            manifest = ProviderManifest.model_validate_json(supplied.model_dump_json())
            if manifest.provider_id in providers:
                raise RuntimeFailure("Installed provider IDs must be unique.")
            if _release_version(manifest.minimum_runtime_version) > release:
                raise RuntimeFailure("Installed provider requires a newer Shovel runtime.", status=409)
            providers[manifest.provider_id] = manifest
        self._providers = MappingProxyType(providers)

    def get(self, provider_id: str) -> ProviderManifest:
        manifest = self._providers.get(provider_id)
        if manifest is None:
            raise RuntimeFailure("Provider is not present in the trusted installed catalog.", status=409)
        return manifest

    def list(self) -> tuple[ProviderManifest, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))
