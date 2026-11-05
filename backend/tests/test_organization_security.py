import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import SecretStr, ValidationError
from scripts.synthetic_storage import synthetic_governance
from sqlalchemy import select, text

from shovel.capability.domain import (
    CapabilityContent,
    ConnectionRequirement,
    FunctionContract,
    FunctionImplementation,
    Permission,
)
from shovel.db.authentication_repository import SQLiteAuthenticationRepository
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.model.organization import OrganizationRevision, OrganizationSource
from shovel.db.model.protection import EnterpriseSourceFence
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import Cloud, ConnectionDraft, EnterpriseConnectionProfile
from shovel.enterprise.execution import EnterpriseReadHandler
from shovel.enterprise.kusto import (
    KustoMetadataClient,
    KustoSourceDenied,
    metadata_command,
    parse_metadata,
)
from shovel.enterprise.organization import (
    MetadataConnection,
    MetadataLabel,
    OrganizationAnchor,
    OrganizationPolicy,
    verify_policy,
)
from shovel.enterprise.organization_service import OrganizationService
from shovel.enterprise.service import ConnectionService
from shovel.execution.domain import ExecutionState
from shovel.identity.domain import AuthContext, BrokerType, IdentityDraft, IdentityProfile
from shovel.identity.profiles import IdentityService
from shovel.identity.waits import authentication_execution_lease
from shovel.protection.domain import (
    ContentRight,
    ProtectionAdapterError,
    ProtectionDeniedError,
    SourceBinding,
)
from shovel.security.services import AuthorizationService, SecurityGate
from test_capability_registry import SCOPE
from test_capability_registry import catalog as catalog
from test_enterprise_execution import FixturePermissions
from test_enterprise_identity_contracts import TENANT, token_request
from test_execution_engine import invocation, publish
from test_execution_engine import kernel as kernel

TOKEN = "SYNTHETIC-ORGANIZATION-CREDENTIAL"
ENDPOINT = "https://metadata.chinanorth2.kusto.chinacloudapi.cn"


def policy_document(*, connection_id="connection", identity=None, profile=None, revision=1):
    identity = identity or IdentityProfile(
        id="identity",
        display_name="Metadata fixture",
        tenant_binding=TENANT,
        enabled=True,
        broker_type=BrokerType.NATIVE_WAM,
        created_at=1000,
        updated_at=1000,
    )
    profile = profile or EnterpriseConnectionProfile(
        id=connection_id,
        display_name="Metadata fixture",
        provider_id="kusto.metadata",
        connection_type="table_metadata",
        environment="organization",
        cloud=Cloud.AZURE_CHINA,
        endpoint=ENDPOINT,
        tenant_binding=TENANT,
        resource_scope=("Demo", "Allowed"),
        identity_profile_ref=identity.id,
        protection_profile_ref="internal-metadata",
        enabled=True,
        status="validated",
        created_at=1000,
        updated_at=1000,
    )
    request = token_request(
        connection_profile_id=profile.id,
        provider_id="kusto.metadata",
        provider_version="1.0.0",
        connection_revision=profile.revision,
        manifest_digest="0" * 64,
        authority=f"https://login.chinacloudapi.cn/{TENANT}",
        audience="https://kusto.kusto.chinacloudapi.cn",
        allowed_scopes=("https://kusto.kusto.chinacloudapi.cn/user_impersonation",),
    )
    request = request.model_copy(
        update={
            "identity": request.identity.model_copy(
                update={
                    "workspace_id": SCOPE.workspace_id,
                    "user_id": SCOPE.user_id,
                    "identity_profile_id": identity.id,
                    "identity_revision": identity.revision,
                }
            )
        }
    )
    connection = MetadataConnection(
        profile=profile,
        identity=identity,
        authentication=request,
        database="Demo",
        table="Allowed",
        label_id="internal-metadata",
        active=True,
    )
    policy = OrganizationPolicy(
        purpose="shovel.organization.metadata.v1",
        organization_id="organization",
        scope=SCOPE,
        tenant_id=TENANT,
        revision=revision,
        issued_at=900,
        expires_at=2000,
        labels=(
            MetadataLabel(
                id="internal-metadata",
                classification_id="internal",
                rank=1,
                protection_status="unprotected",
                encrypted=False,
                dlp_applicable=False,
                retention_policy_ref="ephemeral_metadata_only",
                rights=(ContentRight.READ, ContentRight.DISPLAY),
                restrictions=("metadata_only",),
            ),
        ),
        connections=(connection,),
    )
    connection = connection.model_copy(
        update={
            "authentication": request.model_copy(update={"manifest_digest": policy.manifest.content_hash})
        }
    )
    policy = policy.model_copy(update={"connections": (connection,)})
    policy.validate_connection(connection)
    return policy


@pytest.fixture(scope="module")
def signer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline policy fixture")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.fromtimestamp(800, UTC))
        .not_valid_after(datetime.fromtimestamp(3000, UTC))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True)
        .sign(key, hashes.SHA256())
    )
    der = certificate.public_bytes(serialization.Encoding.DER)

    def sign(policy, payload=None):
        payload = payload or policy.model_dump_json().encode()
        envelope = json.dumps(
            {
                "payload": base64.b64encode(payload).decode(),
                "signature": base64.b64encode(
                    key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
                ).decode(),
                "certificate": base64.b64encode(der).decode(),
            }
        ).encode()
        anchor = OrganizationAnchor(
            organization_id=policy.organization_id,
            revision=policy.revision,
            certificate_sha256=hashlib.sha256(der).hexdigest(),
            envelope_sha256=hashlib.sha256(envelope).hexdigest(),
        )
        return anchor, envelope

    return sign


def response_body(*, version="v1.1", present=True):
    rows = [["Demo", None, None, None, version]]
    if present:
        rows += [["Demo", "Allowed", None, None, None], ["Demo", "Allowed", "Column", "System.String", None]]
    return json.dumps(
        {
            "Tables": [
                {
                    "TableName": "Table_0",
                    "Columns": [
                        {"ColumnName": name, "DataType": "String"}
                        for name in ("DatabaseName", "TableName", "ColumnName", "ColumnType", "Version")
                    ],
                    "Rows": rows,
                }
            ]
        }
    ).encode()


def test_signature_pins_expiry_duplicate_fields_and_rights_fail_closed(signer):
    policy = policy_document()
    anchor, envelope = signer(policy)
    assert verify_policy(envelope, anchor, now=1000).policy == policy
    for data, pin, now in (
        (envelope + b" ", anchor, 1000),
        (envelope, anchor.model_copy(update={"certificate_sha256": "0" * 64}), 1000),
        (envelope, anchor.model_copy(update={"revision": 2}), 1000),
        (envelope, anchor, 2000),
    ):
        with pytest.raises(ProtectionDeniedError):
            verify_policy(data, pin, now=now)
    duplicate = policy.model_dump_json().encode().replace(b'"revision":1', b'"revision":1,"revision":1', 1)
    pin, data = signer(policy, duplicate)
    with pytest.raises(ProtectionDeniedError):
        verify_policy(data, pin, now=1000)
    with pytest.raises(ValidationError):
        MetadataLabel(id="unknown", classification_id="unknown", rank=1, rights=("persist",))
    with pytest.raises(ValidationError):
        MetadataLabel(id="unknown", classification_id="unknown", rank=1, restrictions=("unknown-drm",))


@pytest.mark.parametrize("status", [200, 302, 401, 403, 206])
async def test_fixed_kusto_metadata_http_no_redirect_or_business_query(status, caplog):
    connection = policy_document().connections[0]
    request = connection.authentication
    calls = []

    async def remote(outgoing):
        calls.append(outgoing)
        assert outgoing.url == ENDPOINT + "/v1/rest/mgmt"
        assert outgoing.headers["authorization"] == "Bearer " + TOKEN
        command = json.loads(outgoing.content)
        assert command == {"db": "Demo", "csl": metadata_command(connection)}
        assert ".show database" in command["csl"] and 'TableName == "Allowed"' in command["csl"]
        return httpx.Response(
            status, content=response_body(), headers={"Location": "https://unapproved.invalid"}
        )

    client = KustoMetadataClient(transport=httpx.MockTransport(remote), clock=lambda: 1000)
    credential = AuthContext(request, SecretStr(TOKEN), expires_at=1050)
    if status == 200:
        observed = await client.observe(connection, request, credential)
        assert observed.present and observed.columns == (("Column", "System.String"),)
    else:
        expected = KustoSourceDenied if status in {401, 403} else ProtectionAdapterError
        with pytest.raises(expected):
            await client.observe(connection, request, credential)
    assert len(calls) == 1 and TOKEN not in caplog.text
    with pytest.raises(ValueError):
        credential.validate(request, now=1000)


def test_partial_or_out_of_scope_metadata_rejected():
    connection = policy_document().connections[0]
    for body in (
        response_body().replace(b"Allowed", b"Unapproved"),
        response_body().replace(b"v1.1", b""),
        response_body().replace(b'"Tables":', b'"Exceptions":["unsafe"],"Tables":'),
    ):
        with pytest.raises(ProtectionAdapterError):
            parse_metadata(body, connection, 1000)


def make_service(catalog, signer, *, now=None, document=None):
    now = now or [1000.0]
    holder = [signer(document or policy_document())]
    service = OrganizationService(
        catalog.sessions,
        SimpleNamespace(),
        SimpleNamespace(),
        runtime_version="0.1.2",
        loader=lambda: holder[0],
        clock=lambda: now[0],
        governance=synthetic_governance(SCOPE, lambda: now[0])[0],
    )
    return service, holder, now


@pytest.mark.parametrize("change", ["version", "deleted", "revoked", "policy", "trust"])
async def test_changes_durably_fence_old_sources_and_survive_restart(catalog, signer, change):
    service, holder, _ = make_service(catalog, signer)
    await service.refresh_policy()
    verified = service._trusted()
    connection = verified.policy.connections[0]
    first = parse_metadata(response_body(), connection, 1000)
    resource = await service.journal.observe(verified, connection, first)
    source = SourceBinding(
        resource=resource,
        identity_profile_id=connection.identity.id,
        principal_ref=connection.authentication.identity.principal_ref,
        source_generation=0,
    )
    await service.protection.check_sources(SCOPE, (source,))
    if change in {"version", "deleted"}:
        changed = parse_metadata(response_body(version="v1.2", present=change != "deleted"), connection, 1001)
        with pytest.raises(ProtectionDeniedError):
            await service.journal.observe(verified, connection, changed)
    elif change == "revoked":
        await service.journal.revoke(verified, connection)
    else:
        holder[0] = None if change == "trust" else signer(policy_document(revision=2))
        await service.refresh_policy()
    with pytest.raises(ProtectionDeniedError):
        await service.protection.check_sources(SCOPE, (source,))
    # A fresh adapter and even a replay of the original observation cannot clear the tombstone.
    restarted, _, _ = make_service(catalog, signer)
    with pytest.raises(ProtectionDeniedError):
        await restarted.journal.observe(verified, connection, first)
    async with catalog.sessions() as session:
        fence = await session.scalar(select(EnterpriseSourceFence))
        assert fence.blocked and fence.generation >= 1
        rows = (await session.execute(text("SELECT * FROM organization_source"))).all()
        assert TOKEN not in str(rows)


async def test_policy_rollback_equivocation_and_late_observation_rejected(catalog, signer):
    service, _, _ = make_service(catalog, signer)
    pin, body = signer(policy_document())
    verified = verify_policy(body, pin, now=1000)
    await service.journal.accept(verified)
    connection = verified.policy.connections[0]
    observed = parse_metadata(response_body(), connection, 1001)
    await service.journal.observe(verified, connection, observed)
    with pytest.raises(ProtectionDeniedError, match="Late"):
        await service.journal.observe(verified, connection, observed.model_copy(update={"observed_at": 1000}))
    changed = policy_document().model_copy(update={"revoked": True})
    pin, body = signer(changed)
    with pytest.raises(ProtectionDeniedError, match="equivocation"):
        await service.journal.accept(verify_policy(body, pin, now=1000))
    pin, body = signer(policy_document(revision=2))
    await service.journal.accept(verify_policy(body, pin, now=1000))
    with pytest.raises(ProtectionDeniedError, match="rollback"):
        await service.journal.accept(verified)
    async with catalog.sessions() as session:
        assert (await session.scalar(select(OrganizationRevision))).revision == 2
        assert (await session.scalar(select(OrganizationSource))).blocked


async def test_first_denial_cannot_be_forgotten_by_a_later_success(catalog, signer):
    service, _, _ = make_service(catalog, signer)
    await service.refresh_policy()
    verified = service._trusted()
    connection = verified.policy.connections[0]
    await service.journal.revoke(verified, connection)
    observed = parse_metadata(response_body(), connection, 1001)
    with pytest.raises(ProtectionDeniedError):
        await service.journal.observe(verified, connection, observed)


class FixtureAuthentication:
    def __init__(self, now):
        self.clock = lambda: now[0]
        self.calls = 0

    async def authenticate(self, request):
        self.calls += 1
        return AuthContext(request, SecretStr(TOKEN), expires_at=self.clock() + 50)


@pytest.fixture
async def metadata_flow(kernel, signer):
    now = kernel.now
    identities = IdentityService(SQLiteIdentityRepository(kernel.catalog.sessions, clock=lambda: now[0]))
    identity = (
        await identities.create(SCOPE, IdentityDraft(display_name="Metadata fixture", tenant_binding=TENANT))
    ).profile.model_copy(update={"enabled": True, "broker_type": BrokerType.NATIVE_WAM})
    initial = policy_document(identity=identity)
    connections = ConnectionService(
        SQLiteConnectionRepository(kernel.catalog.sessions, clock=lambda: now[0]),
        InstalledProviderCatalog((initial.manifest,), runtime_version="0.1.2"),
    )
    record = await connections.create(
        SCOPE,
        ConnectionDraft(
            display_name="Metadata fixture",
            provider_id="kusto.metadata",
            connection_type="table_metadata",
            environment="organization",
            cloud=Cloud.AZURE_CHINA,
            endpoint=ENDPOINT,
            tenant_binding=TENANT,
            resource_scope=("Demo", "Allowed"),
            identity_profile_ref=identity.id,
            protection_profile_ref="internal-metadata",
        ),
    )
    document = policy_document(
        identity=identity, profile=record.profile.model_copy(update={"enabled": True, "status": "validated"})
    )
    holder = [signer(document)]
    remote = SimpleNamespace(version="v1.1", present=True, status=200, calls=0)

    async def reply(request):
        remote.calls += 1
        return httpx.Response(
            remote.status, content=response_body(version=remote.version, present=remote.present)
        )

    authentication = FixtureAuthentication(now)
    service = OrganizationService(
        kernel.catalog.sessions,
        authentication,
        kernel.engine,
        runtime_version="0.1.2",
        loader=lambda: holder[0],
        clock=lambda: now[0],
        client=KustoMetadataClient(transport=httpx.MockTransport(reply), clock=lambda: now[0]),
        governance=synthetic_governance(SCOPE, lambda: now[0])[0],
    )
    await service.refresh_policy()
    from test_enterprise_rollout import install_rollout

    rollout = install_rollout(service, signer)
    permission = Permission(action="enterprise.read", resource=record.profile.id)
    gate = SecurityGate(AuthorizationService(FixturePermissions(permission)))

    gate.connection_authorizer = service.authorize_connection
    gate.execution_authorizer = service.revalidate_execution
    kernel.invocations.security = gate
    contract = FunctionContract(
        implementation_id="kusto.metadata.read",
        required_permissions=(permission,),
        connection_requirements=(ConnectionRequirement(name="metadata", connection_ref=record.profile.id),),
    )
    kernel.catalog.registry._functions["kusto.metadata.read"] = contract
    version = await publish(
        kernel.catalog,
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id="kusto.metadata.read"),
            permission_boundary=(permission,),
            **contract.model_dump(exclude={"implementation_id", "retry_safe"}),
        ),
    )
    frames = []
    display = SimpleNamespace(display=lambda execution_id, value: frames.append((execution_id, value)))
    handler = EnterpriseReadHandler(
        service, authentication, service, service, service.protection, display, gate
    )
    errors = []

    async def execute(context):
        try:
            return await handler(context)
        except ProtectionDeniedError as error:
            errors.append(str(error))
            raise

    kernel.functions._functions["kusto.metadata.read"] = execute
    kernel.engine.authentication_waits = SQLiteAuthenticationRepository(
        kernel.catalog.sessions, clock=lambda: now[0]
    )

    async def invoke():
        request = invocation(version)
        return await kernel.invocations.invoke(
            request.model_copy(
                update={"context": request.context.model_copy(update={"permission_ceiling": (permission,)})}
            )
        )

    return SimpleNamespace(
        service=service,
        holder=holder,
        document=document,
        remote=remote,
        kernel=kernel,
        invoke=invoke,
        frames=frames,
        authentication=authentication,
        connections=connections,
        errors=errors,
        rollout=rollout,
    )


async def test_real_engine_metadata_handler_only_releases_protected_metadata(metadata_flow):
    flow = metadata_flow
    execution = await flow.invoke()
    completed = await flow.kernel.engine.run(SCOPE, execution.id)
    async with flow.kernel.catalog.sessions() as session:
        audit = (await session.execute(text("SELECT event FROM protection_audit"))).all()
    assert completed.state == ExecutionState.SUCCEEDED, (
        flow.errors,
        [json.loads(row[0])["reason_codes"] for row in audit],
    )
    assert flow.frames[0][1] == {
        "database_version": "v1.1",
        "columns": [["Column", "System.String"]],
        "present": True,
    }
    assert flow.remote.calls >= 2
    async with flow.kernel.catalog.sessions() as session:
        for table in ("operation", "authentication_completion", "protection_audit", "organization_source"):
            assert TOKEN not in str((await session.execute(text(f"SELECT * FROM {table}"))).all())


@pytest.mark.parametrize("rights", [(ContentRight.READ,), ()])
async def test_kusto_access_does_not_supply_missing_organization_content_rights(
    metadata_flow, signer, rights
):
    flow = metadata_flow
    labels = (flow.document.labels[0].model_copy(update={"rights": rights}),)
    flow.holder[0] = signer(flow.document.model_copy(update={"revision": 2, "labels": labels}))
    await flow.service.refresh_policy()
    from test_enterprise_rollout import install_rollout

    install_rollout(flow.service, signer)
    execution = await flow.invoke()
    failed = await flow.kernel.engine.run(SCOPE, execution.id)
    assert failed.state == ExecutionState.FAILED and flow.frames == []
    if not rights:
        assert flow.remote.calls == flow.authentication.calls == 0


async def test_profile_disable_between_audit_and_release_never_displays(metadata_flow, monkeypatch):
    flow = metadata_flow
    armed, changed = False, False
    original_record = flow.service.policy.audit.record
    original_transaction = flow.service.protection._transaction

    def record(event):
        nonlocal armed
        original_record(event)
        if event.action.value == "read" and event.outcome.value == "allow":
            armed = True

    @asynccontextmanager
    async def transaction():
        nonlocal changed
        async with original_transaction() as session:
            yield session
        if armed and not changed:
            changed = True
            profile = flow.document.connections[0].profile
            await flow.connections.disable(SCOPE, profile.id, expected_revision=profile.revision)

    monkeypatch.setattr(flow.service.policy.audit, "record", record)
    monkeypatch.setattr(flow.service.protection, "_transaction", transaction)
    execution = await flow.invoke()
    failed = await flow.kernel.engine.run(SCOPE, execution.id)
    assert changed and failed.state == ExecutionState.FAILED and flow.frames == []


async def test_transport_denial_invalidates_previously_observed_source(metadata_flow):
    flow = metadata_flow
    execution = await flow.invoke()
    lease = await flow.kernel.engine.start(SCOPE, execution.id)
    token = authentication_execution_lease.set(lease)
    try:
        plan = await flow.service.resolve(lease.execution)
        credential = await flow.authentication.authenticate(plan.request.authentication)
        flow.remote.status = 403
        with pytest.raises(KustoSourceDenied):
            await flow.service.send(plan.request, credential, decode=lambda _: pytest.fail("No release."))
        with pytest.raises(ProtectionDeniedError):
            await flow.service.protection.check_sources(SCOPE, (plan.source,))
        assert execution.id not in flow.service._evidence
    finally:
        authentication_execution_lease.reset(token)


@pytest.mark.parametrize("change", ["version", "deleted", "revoked", "policy", "expired", "connection"])
async def test_running_execution_cannot_reuse_old_metadata_evidence(metadata_flow, signer, change):
    flow = metadata_flow
    execution = await flow.invoke()
    lease = await flow.kernel.engine.start(SCOPE, execution.id)
    token = authentication_execution_lease.set(lease)
    try:
        plan = await flow.service.resolve(lease.execution)
        if change == "version":
            flow.remote.version = "v1.2"
        elif change == "deleted":
            flow.remote.present = False
        elif change == "revoked":
            flow.remote.status = 403
        elif change == "policy":
            flow.holder[0] = signer(flow.document.model_copy(update={"revision": 2, "revoked": True}))
            await flow.service.refresh_policy()
        elif change == "expired":
            flow.kernel.now[0] += 6
            context = flow.service.protection_context(flow.service._evidence[execution.id])
            with pytest.raises(ProtectionDeniedError):
                context.require_known(flow.kernel.now[0])
            return
        else:
            await flow.connections.disable(SCOPE, plan.profile.id, expected_revision=plan.profile.revision)
        with pytest.raises(ProtectionDeniedError):
            await flow.service.resolve(lease.execution)
        assert flow.frames == []
        if change != "connection":
            with pytest.raises(ProtectionDeniedError):
                await flow.service.protection.check_sources(SCOPE, (plan.source,))
    finally:
        authentication_execution_lease.reset(token)
