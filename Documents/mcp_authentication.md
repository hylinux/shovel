# MCP authentication

This reference covers the P0-P2 implementation for #22 / #30. API Key,
static Bearer/PAT and stdio credential references are implemented. OAuth
configuration and secure storage contracts are present, but OAuth networking,
login, refresh, disconnect and UI controls remain P3-P5 work.

## SDK and protocol baseline (P0)

The committed lock selects MCP 1.30.0 and MAF core 1.22.0. No dependency or
lock change is required. The MCP SDK supports protocol versions through
2025-11-25; this is the authorization baseline, not a claim of implementing
every extension in newer specifications.

An offline constructor/storage probe verifies:

| Component | Available integration |
| --- | --- |
| `MCPStreamableHTTPTool` | `static_headers` and caller-owned `http_client` |
| `MCPStdioTool` | Explicit environment plus the SDK's default safe environment |
| `OAuthClientProvider` | HTTPX auth, PKCE, metadata discovery, storage, redirect/callback hooks |
| `ClientCredentialsOAuthProvider` | Client secret basic/post, scopes and pinned issuer |
| `TokenStorage` | Async token and registered-client reads/writes |

These are SDK capabilities, not implemented Shovel OAuth behavior. User OAuth
will initially require a pre-registered public client, Authorization Code with
PKCE S256 and an exact loopback callback. Applications will use Client
Credentials with a secret and an explicitly pinned issuer. Dynamic registration,
client metadata documents, Device Code, certificates and workload identities are
not promised by this first scope. Discovery validation, issuer/resource binding,
callback checks, token expiry and replay-safe refresh still need integration
tests in P3/P4 before OAuth can be enabled.

## Configuration and compatibility

Settings still edits workspace `mcp.json`. `GET` / `PUT
/api/v1/agent/mcp` retain their existing content/revision contract. Saving does
not connect, resolve secrets, log in or restart the Agent. Restart explicitly
through Settings or `POST /api/v1/agent/restart` after changing configuration.
An enabled service failure still blocks extension loading; disable the service
explicitly to continue without it.

New fields on each server:

| Field | Contract |
| --- | --- |
| `credential_id` | Stable, unique UUID required for new authentication/references |
| `auth` | Optional discriminated HTTP authentication declaration |
| `env_credentials` | stdio-only mapping of child environment names to secret references |

Generate a different UUID for each service. Keep it when renaming the service;
use a new UUID when deleting/recreating a service or replacing its identity.
Duplicate IDs in one configuration are rejected. Do not copy IDs across service
definitions to share identities.

`auth.type` is `none`, `api_key`, `bearer`, `oauth_user` or
`oauth_client_credentials`. Missing auth preserves legacy behavior. HTTP auth
cannot be used with stdio. Secrets in new fields must be `env:NAME` or
`keyring:LABEL` references, never literal credentials.

Existing `headers`, `env` and `${NAME}` expansion remain compatible, including
their original file/readback behavior. The configuration response adds a
`warnings` array when legacy headers/env are present: these fields are returned
verbatim, not automatically protected or migrated. Move secrets explicitly to
the new reference fields; merely upgrading does not remove old plaintext from
files or backups.

Explicit authentication rejects duplicate/conflicting headers and reserved
transport headers. Authenticated URLs require HTTPS, except exact `localhost`,
`127.0.0.1` and `::1` HTTP loopback targets for local services/testing; userinfo,
query strings and fragments are not accepted. URL environment substitutions
are revalidated before resolving credentials or connecting. API keys must be
nonempty printable ASCII header values; Bearer values follow the RFC 6750
`b64token` character syntax. Neither is refreshed automatically.

### API Key

```json
{
  "mcpServers": {
    "example": {
      "url": "https://mcp.example.com/mcp",
      "credential_id": "12b9ae42-c658-4177-a3bb-f8b9b616edaf",
      "auth": {
        "type": "api_key",
        "header": "X-API-Key",
        "credential_ref": "env:EXAMPLE_MCP_KEY"
      }
    }
  }
}
```

Set the environment variable in the Host's environment before starting it.
Do not paste actual credentials into shell history or committed environment
files. A running Host does not acquire later changes made in another shell.

### Static Bearer / PAT

Use another service UUID and replace `auth` with:

```json
{
  "type": "bearer",
  "credential_ref": "env:EXAMPLE_MCP_TOKEN"
}
```

The referenced value is the raw token, without the `Bearer ` prefix.
Authentication headers apply to initialize, tool discovery and tool calls.
The locked SDK follows only same-origin redirects (with its limited HTTPS
upgrade rule); cross-origin redirects and HTTPS downgrades do not receive
credentials. Shovel uses origin-scoped MAF static headers and an owned HTTP
client with response-cookie persistence disabled.

### stdio

```json
{
  "mcpServers": {
    "local-example": {
      "command": "YOUR-MCP-EXECUTABLE",
      "args": [],
      "credential_id": "eef781dc-15ae-401d-a63a-7757118ce280",
      "env_credentials": {
        "SERVICE_TOKEN": "env:EXAMPLE_SERVICE_TOKEN"
      }
    }
  }
}
```

Only selected references are injected. The SDK additionally inherits its small,
platform-specific default environment (for example PATH, Windows system/profile
and temporary-directory variables), not every Host environment variable.
Case-insensitive conflicts between `env` and `env_credentials` are rejected.
Keep secrets out of command/arguments. Starting an executable is a trust decision:
this is not a sandbox and cannot prevent a trusted executable from reading
other local resources or leaking its own credentials/output.

## Bound credential and OAuth storage foundation (P1)

`MCPCredentials` reuses `CredentialStore` without changing resource credential
behavior. Environment references are read-only and intentionally resolve the
named Host variable. Keyring labels are local labels, not raw global keyring
account names. Their physical account is derived from the canonical workspace,
service UUID, authentication/endpoint binding and hashed label. It cannot
accidentally collide with `Resource.identity_ref` accounts.

Bindings include auth configuration and stdio command/arguments/reference
mapping. Resolved URL or command/arguments must be used when these contain
environment substitutions. Changing the endpoint, client/issuer or reference
configuration requires reprovisioning the corresponding scoped keyring secret.
Renaming the service, changing tool allow-lists or disabling it does not change
the binding.

For a static URL and `keyring:token`, an administrator can provision from Python
under the same OS identity as the Host (no dedicated MCP credentials UI/CLI yet):

```python
import asyncio
import getpass
from pathlib import Path

from shovel.runtime.agent_extensions import MCPSettings
from shovel.runtime.mcp_auth import MCPCredentials

workspace = Path(r"C:\YOUR-WORKSPACE")
settings = MCPSettings.model_validate_json((workspace / "mcp.json").read_bytes())
server = settings.mcpServers["example"]
assert server.credential_id is not None
credentials = MCPCredentials(workspace, server.credential_id, server.credential_binding())
asyncio.run(credentials.write("keyring:token", getpass.getpass("MCP secret: ")))
```

Configure that server's `credential_ref` as `keyring:token` first. The example
assumes a literal URL; do not provision a template binding for an environment-
expanded endpoint. New references are never resolved by the configuration API.
Missing credentials and unavailable keyring backends fail explicitly; there is
no plaintext-file fallback or echo of backend exception details.

`MCPOAuthStorage` implements the SDK's four async storage methods. Tokens and
registered-client data share one bound keyring record, with serialized updates
within one adapter instance. Reuse one instance per authorization binding in
the future authentication service. `clear()` overwrites the record with empty
state so it removes stored secrets without relying on the shared store's
best-effort delete; backend failure is reported rather than acknowledged.
Malformed stored data is an error, not an anonymous fallback.

This storage adapter does not perform authorization, preserve an active
connection, fence late callbacks, or implement provider-side revocation.
Absolute expiry bookkeeping, authoritative discovered issuer/subject binding,
authorization generations and disconnect/refresh cancellation belong to P3/P4.
They must be established before using stored OAuth data on the network.

## OAuth configuration and future API contract

The following objects can be validated/saved, but enabled OAuth servers fail
explicitly at load with HTTP 503; no browser, token request or anonymous MCP
connection is attempted in P0-P2. Disable them until OAuth implementation lands.

```json
{
  "type": "oauth_user",
  "client_id": "YOUR-PRE-REGISTERED-PUBLIC-CLIENT-ID",
  "issuer": "https://YOUR-AUTHORIZATION-SERVER",
  "scopes": ["tools.read"]
}
```

User `issuer` is optional for future validated discovery. Application `issuer`
is required to pin the recipient of client credentials:

```json
{
  "type": "oauth_client_credentials",
  "client_id": "YOUR-APPLICATION-ID",
  "client_secret_ref": "keyring:application-secret",
  "issuer": "https://YOUR-AUTHORIZATION-SERVER",
  "scopes": ["tools.read"],
  "token_endpoint_auth_method": "client_secret_basic"
}
```

`client_secret_post` is the other supported declaration. Scopes are distinct
OAuth scope tokens, not one space-delimited list entry. The callback address
will come from the existing loopback FastAPI Host, not a Next.js server route.

P3-P5 API contract to implement (not currently registered routes):

| Method / path below `/api/v1/agent/mcp/{server}/auth` | Semantics |
| --- | --- |
| `GET` | Nonsecret configured/state/error projection; does not initiate login |
| `POST /start` | Exact configuration revision, explicit user action; returns authorization ID, browser URL and expiry, never tokens |
| `POST /cancel` | Exact authorization ID and revision; invalidate pending callback |
| `POST /disconnect` | Exact revision; clear local identity, fence pending work and invalidate authenticated connections |

The FastAPI callback will validate state, issuer, PKCE binding, exact redirect,
expiry and single use. Statuses will distinguish not authorized, authorizing,
authorized and reauthorization required, independently of connection success.
Configuration changes produce revision conflicts; malformed declarations are
422; missing/unavailable credentials and authentication-dependent load failure
are 503 with sanitized messages. MCP HTTP 401 and 403 are distinguished in the
load error, not converted into local application authorization.

No endpoint may return access/refresh tokens or client secrets. A settings
save, a token in storage and a successful live connection are different facts.
OAuth authentication never supplies missing platform/enterprise permissions.

## Validation boundary

Tests use local HTTP/stdio servers, synthetic secrets and in-memory stores.
They cover actual discovery and tool calls, disabled/missing credentials,
configuration conflicts, namespace isolation, storage failure, OAuth storage,
401/403, redirection and secret absence from model requests.

No real credentials, OS accounts or enterprise data are used. System keyring
availability and real provider authentication are separate live checks. Internal
and Mooncake Kusto MCP endpoints/auth parameters remain unconfirmed; this work
does not establish enterprise admission or real-service acceptance.
