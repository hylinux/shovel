# MCP authentication

This reference covers the P0-P4 implementation for #22 / #30. API Key,
static Bearer/PAT, stdio credential references, OAuth application identities
and explicit OAuth user authorization are implemented. Settings UI controls
remain P5 work; real-provider acceptance remains separate.

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

Shovel uses the SDK's PKCE, metadata URL/challenge helpers and token models,
with a Host-owned HTTPX authentication lifecycle rather than the SDK's automatic
interactive/replay flow. User OAuth requires a pre-registered public client,
Authorization Code with PKCE S256 and an exact loopback callback. Applications
use Client Credentials with a secret and an explicitly pinned issuer. Dynamic registration,
client metadata documents, Device Code, certificates and workload identities are
not implemented in this scope.

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
within one adapter instance. The Host reuses one instance per authorization
binding. `clear()` overwrites the record with empty
state so it removes stored secrets without relying on the shared store's
best-effort delete; backend failure is reported rather than acknowledged.
Malformed stored data is an error, not an anonymous fallback.

The record also preserves absolute expiry, issuer, resource, client identity and
the local user-authorization generation. Relative token lifetimes do not reset
on restart. Opaque tokens are not decoded to invent a user identity.
`MCPOAuthService` owns network validation, generation fencing and provider
revocation; the storage adapter alone does not establish authorization.

## OAuth configuration and REST controls (P3/P4)

Use these objects inside an HTTP server definition with a `credential_id`.
Enabled OAuth servers require the Host authentication service. No browser is
opened automatically from chat, discovery, restart or status queries.

```json
{
  "type": "oauth_user",
  "client_id": "YOUR-PRE-REGISTERED-PUBLIC-CLIENT-ID",
  "issuer": "https://YOUR-AUTHORIZATION-SERVER",
  "scopes": ["tools.read"]
}
```

User `issuer` is optional when protected resource metadata advertises exactly
one authorization server; otherwise select it explicitly. Application `issuer`
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
comes from the existing loopback FastAPI Host, not a Next.js server route.

Registered controls:

| Method / path below `/api/v1/agent/mcp/{server}/auth` | Semantics |
| --- | --- |
| `GET` | Nonsecret local authorization state, revision, expiry and pending authorization ID; no discovery/login |
| `POST /start` | Exact configuration revision, explicit user action; returns authorization ID, browser URL and expiry, never tokens |
| `POST /cancel` | Exact authorization ID and revision; invalidate pending callback |
| `POST /disconnect` | Exact revision; clear local identity, fence pending work and invalidate authenticated connections |

Mutation bodies use `{"revision": "<current mcp.json revision>"}`; cancel also
requires `"authorization_id"`. Start returns a URL for the user to open
explicitly. Register the exact backend redirect URI with the provider:
`http://127.0.0.1:8080/api/v1/agent/mcp/oauth/callback` for default production,
or port 9999 for default development (substitute the configured Host/port).
The callback returns a small JSON result; no frontend authorization page exists yet.

The callback validates random state, issuer, PKCE binding, exact redirect,
five-minute expiry and single use. It requires the RFC 9207 `iss` response
parameter; providers omitting it are not supported in this first scope.
Only the exact callback GET accepts cross-site browser navigation. The raw
query is removed from the original ASGI scope before access logging; callback
responses, including errors, use `no-store` and `no-referrer`.
States distinguish `not_authorized`, `authorizing`, `authorized` and
`reauthorization_required`, independently of live connection success.
Configuration changes produce revision conflicts; malformed declarations are
422; missing/unavailable credentials and authentication-dependent load failure
are 503 with sanitized messages. MCP HTTP 401 and 403 are distinguished in the
load error, not converted into local application authorization.

No endpoint may return access/refresh tokens or client secrets. A settings
save, a token in storage and a successful live connection are different facts.
OAuth authentication never supplies missing platform/enterprise permissions.

### Discovery and provider compatibility

Protected resource metadata must identify the configured MCP resource and
stay on its origin. The configured issuer must be advertised verbatim; discovered
issuer metadata must match it exactly. Authorization server metadata discovery
supports the SDK's OAuth and OIDC fallback URLs. Authorization, token and
revocation endpoints must remain on the issuer origin. User providers must
advertise PKCE S256; applications validate their grant and client-auth method.
Each token request includes the bound `resource` and requested scopes.

OAuth network requests use DNS-pinned public HTTPS with certificate validation,
no environment proxy, cookies, redirects or automatic HTTP retries. Private and
reserved destinations are rejected, except the exact configured MCP loopback
origin for local servers. Consequently private-network-only MCP/identity
deployments need a separately designed trusted network policy, not a DNS bypass.
Discovery/token responses have bounded time and size; an initial MCP challenge
probe closes without consuming a potentially endless SSE response.

Tokens must have positive `expires_in`; a returned scope set must equal the
requested set. These checks deliberately reject no-expiry tokens and providers
that silently reduce or broaden scopes. Token responses and provider error
descriptions are not returned to callers or logged.

### Expiry, refresh and disconnect

Acquisition and refresh are serialized per service. Applications acquire lazily,
reuse persisted expiry with a small skew, and coalesce concurrent failed token
requests with a two-second cooldown. Users refresh only a still-bound
authorization; rotating refresh tokens replace the prior record atomically.
A failed or cancelled refresh clears local refresh state and requires explicit
reauthorization, rather than retrying an uncertain rotating credential.
Dispatched MCP requests are never automatically replayed on 401/403.

Cancel, disconnect, configuration changes and Host shutdown invalidate pending
work and old authenticated clients. Late callbacks/token writes cannot restore
the invalidated generation. Disconnect clears local tokens before attempting
advertised provider revocation, reports unsupported revocation separately, and
returns an explicit failure if the provider cannot revoke. Local removal does
not prove server-side revocation.

After a new user authorization, disconnect/reconnect or configuration change,
restart the Agent explicitly to replace previously loaded authenticated MCP
connections. A cached invalidated connection fails closed; successful browser
authorization alone does not reconnect it. Configuration saves never resolve
credentials and do not silently restart the Agent.

## Validation boundary

Tests use local HTTP/stdio servers, synthetic secrets and in-memory stores.
They cover actual discovery and tool calls, disabled/missing credentials,
configuration conflicts, namespace isolation, storage failure, OAuth storage,
401/403, redirection and secret absence from model requests. OAuth tests exercise
real MCP initialization/tool calls behind a local OAuth provider, application
acquisition/expiry/restart, user PKCE/refresh rotation, cancellation races,
discovery/binding failures, revocation failure, REST revision/CSRF checks,
callback replay/query redaction and DNS-pinned POST body preservation.

No real credentials, OS accounts or enterprise data are used. System keyring
availability and real provider authentication are separate live checks. Internal
and Mooncake Kusto MCP endpoints/auth parameters remain unconfirmed; this work
does not establish enterprise admission or real-service acceptance.
