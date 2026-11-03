# Standalone Windows WAM component

This Windows-only component uses the public Windows WAM APIs, not MSAL or a
synthetic broker. It is **not integrated with Shovel's production Host**.
Compilation, synthetic tests and platform metadata checks do not establish live
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
