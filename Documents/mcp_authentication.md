# MCP authentication

This reference covers the P0-P6 implementation and acceptance boundaries for
#22 / #30. API Key,
static Bearer/PAT, stdio credential references, OAuth application identities
and explicit OAuth user authorization are implemented, including Settings
controls. Real-provider acceptance remains separate.

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
under the same OS identity as the Host (Settings now also supports write-only
provisioning; there is no dedicated credentials CLI):

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
The callback returns a small JSON result in its own tab. Return to Settings;
the static frontend polls local authorization state without handling OAuth
codes or exchanging tokens.

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

## Settings workflow (P5)

Open Settings -> MCP Server Management. The existing JSON editor remains the
configuration authority; authentication examples cover API Key, Bearer/PAT,
user/application OAuth and stdio. They are disabled by default, contain only
placeholders/references, and use distinct service UUIDs. Save before using
authentication controls; unsaved drafts disable credential/authorization
mutations. Polling, language changes and tab navigation never replace drafts.
Revision conflicts remain explicit and require reload/discard rather than
silently overwriting another configuration.

The saved-server table separates authentication type from last observed
connection state. A successful MCP initialization/tool discovery is shown as
connected; it is **not** evidence that a business tool call was verified.
401, 403, missing user authorization, invalid/changed configuration,
unavailable connection/credentials and restart-required states are distinct.
The page does not initiate a connection or execute a tool to check status.
Failed all-or-nothing extension loading does not leave earlier servers
incorrectly marked connected.

User OAuth has explicit Start authorization and Open authorization page
steps. The link opens a separate tab with no opener/referrer, is shown only
for the matching pending authorization, and expires with that attempt.
Cancel uses the current authorization ID. A pending attempt created by another
page or a lost start response can be cancelled without recovering its URL.
Disconnect requires confirmation; local removal and provider revocation are
reported separately. After a lost mutation response, status is re-read rather
than automatically repeating a login, disconnect or secret write.
Application OAuth has no browser-login button and acquires tokens only when
the Agent actually connects.

Declared `keyring:` references have password-type, write-only inputs:
`PUT /api/v1/agent/mcp/{server}/credentials` accepts
`{"revision": "...", "reference": "keyring:LABEL", "value": "NEW-SECRET"}`.
Only a reference already declared for that service may be written, using the
same resolved workspace/service/configuration binding as runtime reads.
`keyring:oauth-state` is reserved for internal token storage and cannot be
written through this endpoint. Environment references remain read-only.
New values are never read back, placed in configuration responses, or persisted
in browser storage; acknowledged inputs are cleared. Existing inline
headers/env remain visible in the original JSON and are not silently migrated.
Missing/unavailable stores fail explicitly without backend exception details.
Provisioning works for disabled services, without enabling or connecting them.

Credential writes preserve configuration revision checks and drain uninterruptible
keyring writes before configuration changes can proceed. Rotation invalidates
cached HTTP and stdio tool dispatch; application rotation also clears local
OAuth tokens. Already-dispatched work cannot be undone. Restart Agent
explicitly to replace connections/process environments; saving a credential
neither restarts Agent nor establishes live connectivity. The stdio guard uses
the locked MAF SDK dispatch hook shared by generated tools and direct calls.
OAuth authorization remains subject to the same platform/enterprise boundaries.

## Validation boundary

Default tests use local HTTP/stdio servers, synthetic secrets and in-memory stores.
They cover actual discovery and tool calls, disabled/missing credentials,
configuration conflicts, namespace isolation, storage failure, OAuth storage,
401/403, redirection and secret absence from model requests. OAuth tests exercise
real MCP initialization/tool calls behind a local OAuth provider, application
acquisition/expiry/restart, user PKCE/refresh rotation, cancellation races,
discovery/binding failures, revocation failure, REST revision/CSRF checks,
callback replay/query redaction and DNS-pinned POST body preservation.
Settings interaction tests cover explicit authorization, stale poll fencing,
cancel/disconnect confirmation, inert invalid URLs, write-only provisioning,
revision conflicts, literal draft retention and Chinese presentation. Backend
tests cover provisioning scope/revision/read-only restrictions, store failure,
cancelled writes and cached-client invalidation.

### Repeatable acceptance (P6)

| Evidence | Coverage and boundary |
| --- | --- |
| `test_agent_extensions.py`, `test_mcp_auth.py` | Real local HTTP/stdio discovery and calls; API Key/Bearer/env injection, no-auth compatibility, allowed tools, scoped storage, failures and invalidated dispatch |
| `test_mcp_oauth.py` | Local OAuth provider plus real MCP; user PKCE/refresh, application secret basic/post, callback/revocation/concurrency failures and endpoint fences |
| OAuth containment regression | User and both application methods run a real Agent tool round trip through a mocked model, refresh/renew tokens, then exercise a failed exchange; access/refresh tokens, client secret, code/state, PKCE verifier and provider-private errors are absent from model requests/tool-call messages, public projections, errors and captured DEBUG/development logs |
| `mcp-authentication.test.mjs` | Settings component interactions, failure/revision handling and localization |
| `scripts/mcp-edge-smoke.mjs` | Ten actual headless Edge scenario groups against the production static export, with synthetic REST interception only |
| `test_mcp_system_keyring.py` | Separately opted-in Windows Credential Manager storage check; no OAuth login, provider requests or existing credential access |

Run the backend authentication suite from the repository root:

```powershell
uv run pytest backend\tests\test_agent_extensions.py backend\tests\test_mcp_auth.py backend\tests\test_mcp_oauth.py backend\tests\test_hosted_runtime.py --no-cov
```

From `frontend\`, run `npm ci`, `npm run lint`, `npm run typecheck`,
`npm test`, `npm run build`, then `npm run test:smoke:edge`.
Typecheck runs `next typegen && tsc --noEmit`: it generates `next-env.d.ts`
and route types before checking SVG imports, without requiring a previous
build. Generated declarations remain ignored rather than replaced with
handwritten permissive SVG declarations.

The Edge runner requires Microsoft Edge; it never falls back to another
browser. It creates a fresh browser context and ephemeral loopback static
server, blocks external requests and service workers, rejects unexpected APIs,
and closes browser/server resources even on failure. It checks explicit
authorization/safe links, cancellation, local-state polling versus connectivity,
confirmed disconnect, write-only input clearing, draft retention, unsaved-draft
fencing, exact revisions, absence of secrets/state in persistent storage and
absence of page/console/network errors. No real Host, provider or keyring is
used by this browser fixture. A successful run writes only a sanitized receipt
to `frontend\.tools\mcp-edge-smoke.json`; an old receipt is removed before each run.
The `mcp-edge-smoke` Windows CI job uses Node 22, the committed npm lock and
the actual `msedge` channel, then uploads that receipt. The quality job also
checks types before its first build.

For the separate native-system-keyring check, on Windows only:

```powershell
$env:SHOVEL_RUN_MCP_KEYRING_TESTS = "1"
try {
    uv run pytest backend\tests\test_mcp_system_keyring.py --no-cov
} finally {
    Remove-Item Env:\SHOVEL_RUN_MCP_KEYRING_TESTS
}
```

This is skipped by default, including ordinary CI. Explicit opt-in requires
the native `WinVaultKeyring`; an unavailable or different backend fails rather
than silently falling back or being reported as accepted. Each run uses a
random `shovel.mcp.acceptance.<uuid>` service namespace and synthetic values,
checks read/write/overwrite, workspace/service/endpoint isolation, OAuth
record reuse/expiry/client info and secret clearing, then verifies deletion
absence in cleanup even if an earlier assertion fails. It does not enumerate
existing accounts, read or modify existing credentials, change the configured
backend, or invoke WAM/interactive OS authentication.

The P6 local Windows check passed with the native backend and verified cleanup;
the ten browser scenario groups passed in Microsoft Edge 157.0.4341.46.
These are synthetic storage/browser results, **not real-provider acceptance**.
No real credentials, OS login or enterprise data are used.

### Real-provider acceptance gate

Real-provider validation remains **not run / blocked** until an approved
target and operation are supplied. Record the following without secrets:

| Required input | What must be established before execution |
| --- | --- |
| Target and cloud | Exact MCP endpoint (not a Kusto cluster URL), public/company/Mooncake environment, owner and permitted network route |
| OAuth contract | Verified protected-resource metadata, issuer/discovery, client type/ID, exact backend redirect URI, scopes and resource binding; compatibility with the implemented restrictions above |
| Credential preparation | Approved OS identity and environment/keyring reference provisioning; never paste tokens/client secrets into an Issue, command history or model context |
| Approved operation | Who approves, test window, exact discovery/read-only tool and bounded arguments/data scope, allowed downstream use and required platform/enterprise grants |
| Success and cleanup | Expected nonsecret outcome, disconnect/revocation expectations and evidence destination; stop and report incompatibility rather than weakening checks or retrying uncertain effects |

Company and Mooncake Kusto MCP endpoints/auth parameters remain unconfirmed.
Do not guess them, start login from a chat request, or treat synthetic receipts,
OS-keyring availability or human approval as enterprise admission. A private
provider or incompatible OAuth contract needs an explicit design decision,
not a bypass of the current same-origin/public-DNS restrictions.
