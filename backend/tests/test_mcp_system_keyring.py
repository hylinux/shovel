from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from shovel.runtime.mcp_auth import MCPCredentials, MCPOAuthStorage
from shovel.services.credentials import CredentialRef, KeyringCredentialStore


@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_MCP_KEYRING_TESTS") != "1",
    reason="Explicit synthetic native Windows keyring write/delete opt-in required",
)
async def test_native_windows_keyring_bound_storage_and_verified_cleanup(tmp_path: Path) -> None:
    assert sys.platform == "win32", "This acceptance check requires native Windows Credential Manager."
    import keyring
    from keyring.backends.Windows import WinVaultKeyring

    backend = keyring.get_keyring()
    assert type(backend) is WinVaultKeyring, "A native WinVaultKeyring backend is required; no fallback."
    service = f"shovel.mcp.acceptance.{uuid4().hex}"
    store = KeyringCredentialStore(service=service)
    service_id = uuid4()
    binding = {"url": "https://synthetic.example/mcp"}
    credentials = MCPCredentials(tmp_path, service_id, binding, store)
    others = (
        MCPCredentials(tmp_path / "other", service_id, binding, store),
        MCPCredentials(tmp_path, uuid4(), binding, store),
        MCPCredentials(tmp_path, service_id, {"url": "https://other.example/mcp"}, store),
    )

    def cleanup(ref: CredentialRef) -> None:
        if backend.get_password(service, ref.account) is not None:
            backend.delete_password(service, ref.account)
        assert backend.get_password(service, ref.account) is None, "Synthetic keyring cleanup failed."

    with ExitStack() as cleanup_stack:
        for scoped in (credentials, *others):
            for label in ("keyring:token", "keyring:oauth-state"):
                cleanup_stack.callback(cleanup, scoped.storage_ref(label))
        await credentials.write("keyring:token", "synthetic-initial")
        assert await credentials.get("keyring:token") == "synthetic-initial"
        await credentials.write("keyring:token", "synthetic-replacement")
        assert await credentials.get("keyring:token") == "synthetic-replacement"
        for other in others:
            assert await other.read("keyring:token") is None
        storage = MCPOAuthStorage(credentials)
        tokens = OAuthToken(
            access_token="synthetic-access",
            token_type="Bearer",
            refresh_token="synthetic-refresh",
            expires_in=3600,
        )
        client = OAuthClientInformationFull(
            client_id="synthetic-client",
            redirect_uris=["http://127.0.0.1/callback"],
        )
        await storage.set_tokens(tokens)
        await storage.set_client_info(client)
        restarted = MCPOAuthStorage(MCPCredentials(tmp_path, service_id, binding, store))
        restored = await restarted.get_tokens()
        assert restored is not None
        assert restored.access_token == tokens.access_token
        assert restored.refresh_token == tokens.refresh_token
        assert restored.expires_in is not None and 0 < restored.expires_in <= 3600
        assert await restarted.get_client_info() == client
        for other in others:
            assert await MCPOAuthStorage(other).get_tokens() is None
        await restarted.clear()
        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None
        cleared = await credentials.get("keyring:oauth-state")
        assert "synthetic-access" not in cleared and "synthetic-refresh" not in cleared
