import asyncio
import os
import struct
import sys
import time
from types import SimpleNamespace

import pytest

from shovel.capability.domain import CapabilityScope, Permission
from shovel.execution.domain import ExecutionState
from shovel.identity import native_wam
from shovel.identity.errors import AuthConfigurationError, BrokerUnavailableError
from test_enterprise_identity_contracts import token_request

SENTINEL = "FAKE-WAM-SECRET-NOT-A-REAL-TOKEN"


def policy():
    return native_wam.InstalledPolicy.model_validate(
        {
            "root": "C:\\protected-fixture",
            "python": "python.exe",
            "native": "Shovel.Wam.exe",
            "bootstrap": "host_bootstrap.py",
            "modulePaths": ["app"],
            "files": {},
            "bindings": [{"prototype": token_request().model_dump(mode="json"), "target": {}}],
            "revision": 1,
            "validUntil": int(time.time()) + 600,
            "_digest": "a" * 64,
        }
    )


class FixtureBroker(native_wam.NativeWamBroker):
    def policy(self):
        return policy()


async def test_source_checkout_does_not_authorize_authentication(monkeypatch):
    monkeypatch.setattr(native_wam, "_installed", None)
    broker = native_wam.NativeWamBroker()
    with pytest.raises(BrokerUnavailableError):
        broker.policy()
    with pytest.raises(AuthConfigurationError):
        await broker.acquire(token_request())


async def test_resolver_requires_running_scoped_granted_execution():
    request = token_request()
    scope = CapabilityScope(workspace_id=request.identity.workspace_id, user_id=request.identity.user_id)
    permission = Permission(action="enterprise.read", resource=request.connection_profile_id)
    state = SimpleNamespace(value=ExecutionState.RUNNING)
    claim = SimpleNamespace(active=True)
    evaluations = []
    lease = SimpleNamespace(execution=SimpleNamespace(id=request.execution_id), token="claim-fixture")

    async def get(actual_scope, identifier):
        assert actual_scope == scope and identifier == request.execution_id
        return SimpleNamespace(
            state=state.value,
            correlation_id=request.correlation_id,
            deadline_at=time.time() + 20,
            snapshot=SimpleNamespace(
                context=SimpleNamespace(scope=scope),
                granted_permissions=(permission,),
                capability=SimpleNamespace(
                    content=SimpleNamespace(
                        required_permissions=(permission,),
                        connection_requirements=(
                            SimpleNamespace(connection_ref=request.connection_profile_id),
                        ),
                    )
                ),
            ),
        )

    async def evaluate(execution):
        evaluations.append(execution)

    async def check_active(actual, gate):
        assert actual is lease
        if not claim.active:
            raise RuntimeError("Stale execution lease.")
        await gate.evaluate(None)

    engine = SimpleNamespace(
        repository=SimpleNamespace(get=get, check_active=check_active),
        invocations=SimpleNamespace(security=SimpleNamespace(evaluate=evaluate)),
    )
    broker = FixtureBroker()
    resolver = native_wam.NativeWamResolver(engine, broker)
    with pytest.raises(AuthConfigurationError):
        await resolver.resolve(request)
    token = native_wam.authentication_execution_lease.set(lease)
    lease.execution.id = "different-execution"
    with pytest.raises(AuthConfigurationError):
        await resolver.resolve(request)
    lease.execution.id = request.execution_id
    resolution = await resolver.resolve(request)
    assert resolution.profile.enabled
    assert resolution.approved_request == request and len(evaluations) == 1
    state.value = ExecutionState.CANCELLED
    with pytest.raises(AuthConfigurationError):
        await resolver.resolve(request)
    with pytest.raises(AuthConfigurationError):
        await resolver.resolve(request.model_copy(update={"allowed_scopes": ("write",)}))
    assert len(evaluations) == 1
    state.value = ExecutionState.RUNNING
    claim.active = False
    with pytest.raises(RuntimeError, match="Stale execution lease"):
        await resolver.resolve(request)
    with pytest.raises(RuntimeError, match="Stale execution lease"):
        await broker.acquire(request)
    assert not broker._active
    native_wam.authentication_execution_lease.reset(token)


@pytest.mark.parametrize("bad_header", [False, True])
def test_secret_channel_binding_and_no_json_export(bad_header):
    read, write = os.pipe()
    nonce, digest = "a" * 64, "b" * 64
    os.write(
        write,
        bytes.fromhex(("c" * 64 if bad_header else nonce) + digest)
        + struct.pack(">I", len(SENTINEL))
        + SENTINEL.encode(),
    )
    os.close(write)
    if bad_header:
        with pytest.raises(ValueError, match="binding mismatch"):
            native_wam._read_secret(read, nonce, digest)
    else:
        credential = native_wam._read_secret(read, nonce, digest)
        assert credential.get_secret_value() == SENTINEL
        assert SENTINEL not in repr(credential)


async def test_fragmented_public_output_is_fully_read_and_bounded():
    reader = asyncio.StreamReader()

    async def feed():
        for chunk in (b'{"scope":', b'"safe"', b"}"):
            reader.feed_data(chunk)
            await asyncio.sleep(0)
        reader.feed_eof()

    feeding = asyncio.create_task(feed())
    assert await native_wam._bounded_output(reader) == b'{"scope":"safe"}'
    await feeding
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 4097)
    reader.feed_eof()
    with pytest.raises(ValueError, match="limit"):
        await native_wam._bounded_output(reader)


CHILD = r"""
import sys, os, msvcrt, struct, json, base64, hashlib
r=msvcrt.open_osfhandle(int(sys.argv[1]), os.O_RDONLY)
w=msvcrt.open_osfhandle(int(sys.argv[2]), os.O_WRONLY)
def read(n):
    b=b''
    while len(b)<n:
        b+=os.read(r,n-len(b))
    return b
item=json.loads(read(struct.unpack('>I',read(4))[0]))
fingerprint=hashlib.sha256(base64.b64decode(item['request'])).hexdigest()
nonce=item['nonce']
body=b'FAKE-WAM-SECRET-NOT-A-REAL-TOKEN'
os.write(w,bytes.fromhex(nonce+fingerprint)+struct.pack('>I',len(body))+body)
os.close(w)
print(json.dumps(dict(version=1,scope='wam_host_bridge',nonce=nonce,
fingerprint=fingerprint,expires_at=item['deadline'],status='success',
production_ready=False,live_acceptance_complete=False,native_operation_canceled=False)))
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited-handle contract")
async def test_real_private_pipes_reach_only_ephemeral_context(monkeypatch, capsys):
    original = asyncio.create_subprocess_exec

    async def fixture_child(executable, mode, *handles, **kwargs):
        assert mode == "bridge"
        assert kwargs["close_fds"] and len(kwargs["startupinfo"].lpAttributeList["handle_list"]) == 2
        kwargs["cwd"] = None
        return await original(sys.executable, "-c", CHILD, *handles, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_child)
    request = token_request()
    broker = FixtureBroker()
    context = await broker._acquire(request, time.time() + 20, policy())
    assert (
        context.consume(request, now=time.time(), transport=lambda value: value.get_secret_value())
        == SENTINEL
    )
    with pytest.raises(ValueError):
        context.validate(request, now=time.time())
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


@pytest.mark.skipif(os.name != "nt", reason="Windows native-cancel delivery contract")
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancel_delivers_bound_control_and_discards_late_private_credential(
    monkeypatch, capsys, repeat_cancel
):
    original = asyncio.create_subprocess_exec
    original_frame = native_wam._frame
    ready = asyncio.Event()
    cancelling = asyncio.Event()
    children = []
    loop = asyncio.get_running_loop()
    delayed = CHILD.replace(
        "body=b'FAKE-WAM-SECRET-NOT-A-REAL-TOKEN'",
        "cancel=json.loads(read(struct.unpack('>I',read(4))[0]))\n"
        "assert cancel['cancel']==nonce\nimport time\ntime.sleep(0.1)\n"
        "body=b'FAKE-WAM-SECRET-NOT-A-REAL-TOKEN'",
    )

    def frame(fd, value):
        original_frame(fd, value)
        if "request" in value:
            loop.call_soon_threadsafe(ready.set)
        if "cancel" in value:
            loop.call_soon_threadsafe(cancelling.set)

    async def fixture_child(executable, mode, *handles, **kwargs):
        kwargs["cwd"] = None
        child = await original(sys.executable, "-c", delayed, *handles, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_child)
    monkeypatch.setattr(native_wam, "_frame", frame)
    broker = FixtureBroker()
    running = asyncio.create_task(broker._acquire(token_request(), time.time() + 20, policy()))
    await asyncio.wait_for(ready.wait(), 5)
    running.cancel()
    if repeat_cancel:
        await asyncio.wait_for(cancelling.wait(), 5)
        for _ in range(3):
            await asyncio.sleep(0.01)
            running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert children and children[0].returncode is not None
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


async def test_disconnect_invalidates_only_matching_profile():
    broker = FixtureBroker()
    request = token_request()
    tasks = [asyncio.create_task(asyncio.sleep(60)) for _ in range(2)]
    broker._active = dict(zip(("one", "two"), tasks, strict=True))
    broker._profiles = {"one": request.identity.identity_profile_id, "two": "other"}
    from test_enterprise_authentication import profile

    await broker.disconnect(profile())
    assert tasks[0].cancelled() and not tasks[1].done()
    await broker.close()
