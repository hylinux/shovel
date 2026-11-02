from pydantic import TypeAdapter, ValidationError

from shovel.capability.domain import CapabilityScope
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import ConnectionDraft, ConnectionRecord, ConnectionValidation, ProviderManifest
from shovel.enterprise.ports import ConnectionRepository
from shovel.identity.domain import Revision
from shovel.runtime.errors import RuntimeFailure


class ConnectionService:
    """Local configuration management; no broker, transport, protection bypass or enable method."""

    def __init__(self, repository: ConnectionRepository, catalog: InstalledProviderCatalog) -> None:
        self.repository, self.catalog = repository, catalog

    def options(self) -> tuple[ProviderManifest, ...]:
        return self.catalog.list()

    @staticmethod
    def parse_draft(value: object) -> ConnectionDraft:
        try:
            return ConnectionDraft.model_validate(value)
        except ValidationError:
            raise RuntimeFailure("Connection draft validation failed.", status=422) from None

    def _manifest(self, draft: ConnectionDraft) -> ProviderManifest:
        draft = self.parse_draft(draft)
        manifest = self.catalog.get(draft.provider_id)
        if (
            draft.connection_type not in manifest.supported_connection_types
            or not any(
                item.cloud == draft.cloud
                and item.environment == draft.environment
                and draft.endpoint in item.endpoints
                for item in manifest.environments
            )
            or not {item.name for item in draft.provider_settings}.issubset(manifest.permitted_settings)
        ):
            raise RuntimeFailure("Connection configuration is incompatible with its installed manifest.")
        return manifest

    async def create(self, scope: CapabilityScope, draft: ConnectionDraft) -> ConnectionRecord:
        draft = self.parse_draft(draft)
        return await self.repository.create(scope, draft, self._manifest(draft))

    async def update(
        self, scope: CapabilityScope, identifier: str, draft: ConnectionDraft, *, expected_revision: int
    ) -> ConnectionRecord:
        draft = self.parse_draft(draft)
        return await self.repository.update(
            scope,
            identifier,
            draft,
            self._manifest(draft),
            expected_revision=expected_revision,
        )

    async def disable(
        self, scope: CapabilityScope, identifier: str, *, expected_revision: int
    ) -> ConnectionRecord:
        return await self.repository.disable(scope, identifier, expected_revision=expected_revision)

    async def get(self, scope: CapabilityScope, identifier: str) -> ConnectionRecord:
        return await self.repository.get(scope, identifier)

    async def list(self, scope: CapabilityScope) -> list[ConnectionRecord]:
        return await self.repository.list(scope)

    async def validate(
        self, scope: CapabilityScope, identifier: str, *, expected_revision: int
    ) -> ConnectionValidation:
        expected_revision = TypeAdapter(Revision).validate_python(expected_revision)
        record = await self.get(scope, identifier)
        if record.profile.revision != expected_revision:
            raise RuntimeFailure("Connection revision conflict.", status=409)
        manifest = self.catalog.get(record.profile.provider_id)
        if (
            manifest.provider_version != record.manifest_version
            or manifest.content_hash != record.manifest_digest
        ):
            raise RuntimeFailure(
                "Installed manifest has changed; save a reviewed connection revision.", status=409
            )
        record.profile.validate_manifest(manifest, require_enabled=False)
        return ConnectionValidation(
            identifier=identifier,
            revision=record.profile.revision,
            manifest_digest=manifest.content_hash,
        )
