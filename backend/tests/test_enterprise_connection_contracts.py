import pytest
from pydantic import ValidationError

from shovel.enterprise.domain import (
    Cloud,
    EnterpriseConnectionProfile,
    EnterpriseResourceRef,
    ProviderEnvironment,
    ProviderManifest,
    ProviderSetting,
    validate_token_request,
)
from shovel.identity.domain import BrokerType, IdentityProfile
from test_enterprise_identity_contracts import OTHER_TENANT, TENANT, token_request


def manifest() -> ProviderManifest:
    return ProviderManifest(
        provider_id="provider",
        provider_version="1.0",
        supported_connection_types=("query",),
        environments=(
            ProviderEnvironment(
                cloud=Cloud.AZURE_GLOBAL,
                environment="production",
                authority_origins=("https://login.microsoftonline.com",),
                endpoints=("https://cluster.example",),
                audiences=("api://resource",),
                permitted_scopes=("api://resource/read",),
            ),
        ),
        permitted_settings=("database",),
        availability_probe="probe",
        health_check="health",
        minimum_runtime_version="0.1.2",
    )


def connection(**changes: object) -> EnterpriseConnectionProfile:
    return EnterpriseConnectionProfile.model_validate(
        {
            "id": "connection",
            "display_name": "Enterprise",
            "provider_id": "provider",
            "connection_type": "query",
            "environment": "production",
            "cloud": Cloud.AZURE_GLOBAL,
            "endpoint": "https://cluster.example",
            "tenant_binding": TENANT,
            "resource_scope": ("database",),
            "identity_profile_ref": "work",
            "protection_profile_ref": "protection",
            "enabled": True,
            "status": "validated",
            "created_at": 1,
            "updated_at": 2,
            **changes,
        }
    )


def resource(**changes: object) -> EnterpriseResourceRef:
    return EnterpriseResourceRef.model_validate(
        {
            "provider_id": "provider",
            "connection_profile_id": "connection",
            "resource_type": "document",
            "resource_id": "document",
            "tenant_id": TENANT,
            "source_version": "v1",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"cloud": Cloud.AZURE_CHINA},
        {"endpoint": "https://unapproved.example"},
        {"provider_id": "unknown"},
        {"enabled": False},
        {"status": "draft"},
        {"provider_settings": (ProviderSetting(name="unreviewed", value="value"),)},
    ],
)
def test_connection_rejects_cloud_endpoint_provider_status_and_setting_mismatches(changes) -> None:
    with pytest.raises(ValueError):
        connection(**changes).validate_manifest(manifest())


@pytest.mark.parametrize(
    "name",
    ["password", "Authorization", "api_key", "cookie", "session_cookie", "session.cookie", "access_token"],
)
def test_profile_never_accepts_secret_settings(name: str) -> None:
    with pytest.raises(ValidationError):
        ProviderSetting(name=name, value="CONFIG-SENTINEL")


def test_manifest_and_profile_roundtrip_without_mutable_configuration() -> None:
    approved = manifest()
    profile = connection(provider_settings=(ProviderSetting(name="database", value="approved"),))
    profile.validate_manifest(approved)
    assert ProviderManifest.model_validate_json(approved.model_dump_json()) == approved
    assert EnterpriseConnectionProfile.model_validate_json(profile.model_dump_json()) == profile
    assert approved.content_hash == manifest().content_hash
    with pytest.raises(ValidationError):
        profile.provider_settings[0].value = "changed"
    with pytest.raises(ValidationError):
        ProviderManifest.model_validate(
            {**approved.model_dump(), "environments": (approved.environments[0],) * 2}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_scopes": ("api://resource/write",)},
        {"audience": "api://other"},
        {"authority": f"https://unapproved.example/{TENANT}"},
        {"manifest_digest": "b" * 64},
        {"connection_revision": 2},
        {"provider_version": "2.0"},
    ],
)
def test_token_request_is_checked_against_exact_manifest_and_connection(changes) -> None:
    approved = manifest()
    profile = IdentityProfile(
        id="work",
        display_name="Work",
        tenant_binding=TENANT,
        broker_type=BrokerType.PYTHON_WAM,
        enabled=True,
        created_at=1,
        updated_at=1,
    )
    good = token_request(manifest_digest=approved.content_hash)
    validate_token_request(good, connection(), approved, profile)
    with pytest.raises(ValueError):
        validate_token_request(good.model_copy(update=changes), connection(), approved, profile)


def test_resource_identity_includes_tenant_connection_and_source_version() -> None:
    original = resource()
    assert (
        len(
            {
                original.version_key,
                resource(tenant_id=OTHER_TENANT).version_key,
                resource(connection_profile_id="other").version_key,
                resource(source_version="v2").version_key,
            }
        )
        == 4
    )
    assert EnterpriseResourceRef.model_validate_json(original.model_dump_json()) == original
    with pytest.raises(ValidationError):
        resource(source_version="")
