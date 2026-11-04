# Standalone Windows WAM component

This Windows-only component uses the public Windows WAM APIs, not MSAL or a
synthetic broker. Host wiring remains unavailable without the controlled
installation described below; enterprise admission remains disabled.
It also contains an installed-Host-only bridge; a source checkout has no trust
context and cannot use that bridge. Compilation, synthetic tests and platform metadata checks do not establish live
authentication acceptance. Every receipt retains `production_ready=false` and
`live_acceptance_complete=false`.

## Build and verify without authentication

Use Windows 11, an x64 process, .NET SDK from `global.json`, and the committed
NuGet locks. Run from `native\` so SDK resolution honors that file:

```powershell
Set-Location native
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"
$env:TESTINGPLATFORM_TELEMETRY_OPTOUT = "1"
dotnet restore Shovel.Wam.Tests\Shovel.Wam.Tests.csproj --configfile NuGet.Config --locked-mode
dotnet test Shovel.Wam.Tests\Shovel.Wam.Tests.csproj --no-restore -c Release
dotnet format Shovel.Wam\Shovel.Wam.csproj --no-restore --verify-no-changes
dotnet format Shovel.Wam.Tests\Shovel.Wam.Tests.csproj --no-restore --verify-no-changes
dotnet run --project Shovel.Wam\Shovel.Wam.csproj --no-build -c Release -- check
```

Tests use synthetic providers/accounts/results and async operations. The real
request-builder test constructs local WinRT request objects without discovery or
acquisition. The HWND test creates and closes an **unshown** owned window; it
does not open authentication UI. `check` only checks OS/process/session/API type
availability. Neither tests nor `check` find an OS account or request a token.
There is no live-auth CI job.

## Separately approved real authentication

Do not run the following modes without approval of the **specific tenant,
application, existing WAM account, resource and scopes**, plus separate approval
of the actual authentication operation. Interactive login/consent is completed
by the user, never by the Agent or CI. OS WAM may update its own cache/session.
No business request is sent, but that does not mean OS authentication state is
unchanged.

Create a local JSON target outside the repository. The following is an invalid,
non-runnable template; replace every placeholder with an approved value:

```json
{
  "tenantId": "00000000-0000-0000-0000-000000000000",
  "clientId": "00000000-0000-0000-0000-000000000000",
  "accountId": "<approved-existing-WAM-account-id>",
  "resource": "https://resource.example.invalid",
  "scopes": ["<approved-scope>"],
  "timeoutSeconds": 60
}
```

There is no default account/scope or account enumeration/enrollment. `accountId`
is the exact WAM account ID, not a display name/email guess. The app registration
must allow the desktop broker redirect
`ms-appx-web://Microsoft.AAD.BrokerPlugin/<clientId>`.
This version supports an exact Azure Global tenant authority, HTTPS resource,
and one existing connected organizational WAM account only. China, account
enrollment, guest/CA/claims and actual permission/resource binding remain
unaccepted; do not infer effective token grants from request fields.
Unknown/duplicate fields, arbitrary authority/cloud/property bags, credentials,
empty GUIDs, duplicate scopes and invalid timeouts are rejected.

Only after the separate approval, from `native\`:

```powershell
dotnet run --project Shovel.Wam\Shovel.Wam.csproj --no-build -c Release -- silent --target "C:\approved\wam-target.json" --allow-authentication
dotnet run --project Shovel.Wam\Shovel.Wam.csproj --no-build -c Release -- interactive --target "C:\approved\wam-target.json" --allow-authentication
```

Silent calls `GetTokenSilentlyAsync` and never automatically retries
interactively. Interactive first displays the app's own window; provider/account
discovery and `RequestTokenWithWebAccountForWindowAsync` start only after its
sign-in button is clicked. The HWND must belong to the current process and
thread and is rechecked before dispatch. An unavailable/changed account, provider
or HWND blocks dispatch. There is no browser, CLI, device-code, ROPC or custom
OAuth fallback. WAM's own UI may contain web-based authentication steps.

## Cancellation and safe receipts

Cancel, window close, Ctrl+C and the shared discovery/acquisition deadline request
native `IAsyncInfo.Cancel`. The original async operation is retained; cancellation
is fenced before decoding a late result. Completed operations are closed, and
pending results are observed/discarded and closed when they finish while the
process remains alive. Native cancel/status/close failures produce enum-only
diagnostics; a synchronous close failure cannot report success.

After a two-second cancellation grace period, lack of completion produces
`cancellation_unconfirmed`, not a claim that WAM stopped.
`native_operation_canceled=true` means **only** that the observed native
`AsyncStatus` was `Canceled`; it does not prove UI closure, cessation of SSO work,
cache erasure, or accepted production cancellation. Process exit can preempt late
draining and cannot guarantee OS-side cleanup. Those cases need approved live
acceptance before production use.

Stdout receipts contain only format version, scope, status, stage and acceptance/
native-state flags; stderr diagnostics contain only an enum. No token, account,
tenant, scope contents, claims, source path or raw exception is output. The
component never returns/persists credentials or creates its own token cache.
WinRT may materialize an immutable token string for the presence check; closing
handles/releasing references is **not** reliable secret zeroization.

Exit codes: `0` for help/successful platform check/acquisition, `2` for invalid
arguments/target, and `3` for unavailable/blocked/canceled/timed-out/native-error
outcomes. A platform-check success is not authentication success.

The remaining live matrix and future trusted installation/IPC boundaries are in
[the WAM design](../Documents/windows_wam_required_design.md).

## Opt-in acceptance runner

After approval of the target and the specific operation, use the same target file:

```powershell
dotnet run --project Shovel.Wam\Shovel.Wam.csproj --no-build -c Release -- accept --case Silent --target "C:\approved\wam-target.json" --allow-authentication
```

Cases are `Silent`, `InteractionRequired`, `Interactive`, `Cancel`, and `Timeout`.
`Cancel` uses the app's interactive window and requires the user to cancel;
`Timeout` uses the target's configured deadline. Fast successful authentication
does not pass a timeout/cancel case. Receipts record actual acquisition dispatch
count, the native outcome and whether the expected outcome was observed.
**They never mark live acceptance complete.** Independent observation of UI,
tenant/account/resource/grants, broker behavior and OS-side late cleanup remains
necessary. Missing targets, opt-in, required interaction or independent evidence
must not be replaced with synthetic results.

## Controlled installed Host

The implemented trust model assumes administrator-controlled installation and a
trusted interpreter/dependency tree. It does not isolate same-user malware,
administrators or injected in-process code.

Deployment requires an organization-controlled signed installation envelope and
a 64-bit **HKLM** `SOFTWARE\Shovel\WamTrust` key. No runtime command creates keys,
installs certificates, signs with an organization key or grants permissions.
Administrator provisioning must independently approve:

| Registry value | Meaning |
|---|---|
| `PolicyPath` (string) | Absolute path of the signed envelope, outside the immutable package root |
| `PolicySha256` (string) | Lowercase SHA-256 of the exact envelope bytes |
| `SignerCertificateSha256` (string) | Lowercase SHA-256 of the DER signing certificate |
| `Revision` (DWORD) | Exact approved policy revision, at least 1 |

Envelope JSON has exactly `payload`, `signature`, `certificate`, each Base64.
The payload is UTF-8 JSON; the signature is RSA PKCS#1 SHA-256 over its exact
bytes. The pinned certificate must be currently valid and have code-signing EKU,
with an RSA key of at least 2048 bits. Pinning is the authority, not self-issued
certificate installation or a certificate supplied by TOML.

Payload fields are `root`, `python`, `native`, `bootstrap`, `modulePaths`, `files`,
`bindings`, `revision`, `validUntil`. `validUntil` is UTC Unix seconds and cannot
extend beyond the signing certificate's validity.
`python`, `native`, `bootstrap`, module directories and inventory paths are
Windows-style paths relative to `root`, without traversal, ADS or reparse points.
`files` maps **every installed file** to its lowercase SHA-256; unlisted files,
missing files and case-colliding paths are rejected. Include the interpreter,
stdlib, application and dependencies, published native DLLs/EXE/config files and
`host_bootstrap.py`. Package/key ownership and writable ACEs are checked for all
files and directories, including child-deletion rights;
ordinary-user-owned or writable installations are rejected. Complex writable
ACLs unsupported by the bootstrap must be rejected, not loosened.

Each binding contains a `prototype` (the existing complete `TokenRequest` JSON
contract) and a `target` (the target contract above). Only request/execution/
correlation IDs vary; workspace, user, principal, identity/connection revisions,
provider/version/manifest, authority, audience and scopes must match exactly.
Native target tenant/resource/scopes must agree. Host V1 is **silent-only**;
claims challenges, force refresh and interactive requests are blocked, never
silently approximated. Request `minimum_token_lifetime` is derived from the
bounded acquisition lease, not an arbitrary provider-property bag.

Publish the native project into the controlled package before inventory/signing.
Include only installed files, not mutable build output, logs or user stores.
The approved interpreter must be invoked with the exact protected bootstrap:

```powershell
& "C:\approved-install\python\python.exe" -I -S "C:\approved-install\host_bootstrap.py"
```

This bootstrap verifies the machine-pinned inventory, requests **non-auth**
native attestation, sets only approved module paths, suppresses bytecode writes,
then starts the existing `shovel run prd`. Native independently verifies the
signature, full installation, its own image, and the parent interpreter **and
exact isolated bootstrap command**. A signed `python.exe` alone is insufficient.
Do not use a source checkout, `uv run`, arbitrary `-c`/`-m`, or environment/
personal-settings overrides to create installation trust.

`Runtime` now assembles `NativeWamResolver`/`NativeWamBroker`. Before acquisition
the resolver requires the actual Engine-owned Execution claim lease and checks
the current claim, running ancestor lineage, deadlines and consumed approval.
Checks run before SDK dispatch and again before credential delivery, using the
same database guard as protected data use. It also checks the scoped Execution,
frozen connection requirement and read grants, current SecurityGate, signed exact binding and the
unchanged deadline (within the 300-second auth limit). Metadata identity rows
remain disabled/unavailable configuration; only installed authorization can
provide a runtime projection. Python workspace extensions must remain disabled.
This does not install a Provider or source/policy adapter.

Each acquisition launches one verified auth-only child with an explicit inherited
handle list: private control input and a separate binary credential output;
the child immediately disables further inheritance of those handles.
Nonce, complete request fingerprint and expiry bind the response. Stdout/stderr
are bounded and never carry credentials. Python never forwards native stderr.
The credential reaches only an expiring, one-use `AuthContext`; cancellation
requests native Cancel, late results are discarded, and a stuck **owned child**
may be terminated after cleanup grace. This is not proof that OS WAM/UI/cache
work stopped. Disconnect invalidates only the selected app profile, not OS SSO.
Repeated cancellation cannot interrupt owned-child/pipe cleanup. Installation
verification runs off the Host event loop, and children use the protected root
as their working directory.

Policy changes invalidate the existing bootstrap and require restart/revalidation.
No default trust root, self-signed production fallback, automatic trust
provisioning, token cache, HTTP listener or default enterprise enable is added.
