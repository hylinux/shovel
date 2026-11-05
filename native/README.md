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
a 64-bit **HKLM** `SOFTWARE\Shovel\WamTrust` key. The Host never provisions trust
or grants permissions. Separate operator-only deployment commands can sign with
an existing organization key and initialize the protected machine anchor.
Administrator provisioning must independently approve:

| Registry value | Meaning |
|---|---|
| `State` (REG_SZ) | Strict schema-2 JSON, replaced as one atomic registry value and flushed |

`State` contains `storeRoot`, `generation`, `highestRevision`, `signers`,
`revokedSigners`, `revokedEnvelopes`, and nullable `active`
(`envelopeSha256`, `signerSha256`, `revision`). Digests are lowercase SHA-256.
It is machine-only, never HKCU/TOML. Missing/legacy/duplicate/unknown state,
revoked or absent activation, stale generation and invalid ACLs fail closed.
Legacy four-value anchors are deliberately not silently imported or recreated;
an existing installation requires a separately approved administrative migration.
Do not delete an existing anchor to reset its revision/revocation history.

Envelope JSON has exactly `payload`, `signature`, `certificate`, each Base64.
The payload is UTF-8 JSON; the signature is RSA PKCS#1 SHA-256 over its exact
bytes. The pinned certificate must be currently valid and have code-signing EKU,
with an RSA key of at least 2048 bits. Pinning is the authority, not self-issued
certificate installation or a certificate supplied by TOML.

Payload fields are `root`, `python`, `native`, `bootstrap`, `modulePaths`, `files`,
`bindings`, `revision`, `validUntil`, `runtime`. `validUntil` is UTC Unix seconds and cannot
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
`runtime` records Python/.NET versions, the committed `uv.lock` SHA-256, every
installed distribution name/version/metadata path, explicit DLL directories and
the interpreter/stdlib/site-packages paths. Self-contained .NET Core and Windows
Desktop files are mandatory; a framework-dependent ZIP is not installable.

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
& "C:\approved-install\python\python.exe" -I -S -B "C:\approved-install\host_bootstrap.py"
```

This bootstrap verifies the machine-pinned inventory, requests **non-auth**
native attestation, sets only approved module paths, suppresses bytecode writes,
then starts the existing `shovel run prd`. Native independently verifies the
signature, full installation, its own image, and the parent interpreter **and
exact isolated bootstrap command**. A signed `python.exe` alone is insufficient.
`-B` is mandatory before interpreter startup: setting `dont_write_bytecode` only
inside the bootstrap is too late to prevent import-time inventory mutations.
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

Policy or machine-generation changes invalidate the existing bootstrap and require restart/revalidation.
No default trust root, self-signed production fallback, automatic trust
provisioning, token cache, HTTP listener or default enterprise enable is added.

The protected bootstrap can also read the separate administrator-provisioned
`HKLM\SOFTWARE\Shovel\EnterpriseTrust` organization policy anchor. This is not a
personal-setting override and does not inherit WAM sign-in approval. Its
metadata-only policy, registry fields, catalog, durable source fences and
limitations are documented in
[H3 organization/Kusto metadata](../Documents/enterprise_security_analysis.md#23-h3-首批组织信任与-kusto-元数据适配器).
Missing policy leaves the catalog empty. No organization keys/certificates or
live Kusto/WAM acceptance are provisioned by this bootstrap.

## Candidate package and CI evidence

CI now publishes a self-contained unsigned native component ZIP and a
sanitized contract-test receipt bound to its SHA-256 and source commit. The
Windows Python lane exercises real inherited-handle child processes with
synthetic credentials, including replay, forged public/private bindings,
duplicate JSON, truncation, cancellation and one-use acquisition concurrency.
These children are protocol fixtures, not the real WAM executable or an accepted
installed Host. Native contract tests still perform no account/token calls.

A separate main-branch provenance job attests candidate wheel/ZIP and sanitized
receipts; PR jobs have no attestation write authority. It does not sign an
organization installation envelope, provision HKLM, enable connections or
approve real authentication. A separate complete offline candidate contains the
interpreter, locked non-editable dependencies, native runtime and unsigned
installation payload. CI exercises isolated imports, including Outlook's native
modules, and attests the complete candidate ZIP. Neither ZIP is a trusted
organization installation. Four-lane qualification
and the review-only minimum-permission proposal are documented in
[the provider guide](../Documents/enterprise_provider_guide.md#10-离线端到端编排候选制品与开放前审查).
Live WAM, Kusto and storage/key acceptance remain outstanding.

## Complete offline assembly and administrative lifecycle

The builder and deployment commands are implemented, but are **not run against
the developer's machine trust, certificate store or Program Files by tests**.
The installed envelope authenticates every file through the organization's
certificate; this is not an Authenticode signature on each bundled executable.
First independently verify the release provenance, package digests and approved
organization certificate fingerprint using your organization's deployment
process. Do not elevate an arbitrary unsigned download to bootstrap trust.

Prepare an approved complete x64 Python 3.14+ home (not a venv, without `_pth`
overrides), a separate **wheel-installed**, non-editable runtime environment
from `uv.lock`, and a self-contained native publish:

```powershell
# From native\
dotnet restore Shovel.Wam\Shovel.Wam.csproj --configfile NuGet.Config --locked-mode --runtime win-x64 -p:SelfContained=true
dotnet publish Shovel.Wam\Shovel.Wam.csproj --no-restore -c Release --runtime win-x64 --self-contained true -o ..\.tools\controlled-native
```

For the dependency environment, use `uv sync --locked --extra outlook --no-dev
--no-editable --no-install-project` with a separate
`UV_PROJECT_ENVIRONMENT`, then install the approved wheel with `uv pip install
--python <environment-python> --no-deps <wheel>`. Do not use `--all-extras`:
that includes development tools. Assembly never downloads dependencies or
certificates, changes ACLs/HKLM, authenticates WAM, or opens production.
It executes the explicitly supplied approved interpreter only to probe its
version/bitness. It inventories all copied files, checks application bytes
against the wheel, dependency closure/versions against the lock and installed
VCS commit provenance, and rejects editable packages, reparse points, case/path
aliases, missing stdlib or shared-framework native publishes.

From the repository root, with independently approved local input paths:

```powershell
uv run python scripts\build_controlled_installation.py `
  --python-home "C:\approved-input\python" `
  --site-packages "C:\approved-input\runtime\Lib\site-packages" `
  --native ".tools\controlled-native" --wheel "C:\approved-input\shovel.whl" `
  --lock uv.lock --output ".tools\installation-review" `
  --install-root "C:\Program Files\Shovel\packages\1" `
  --revision 1 --valid-until <approved-unix-seconds>
```

The new output contains `payload\` and `installation-payload.json`.
No input or existing output is overwritten. Interpreter-global site-packages
are excluded; only the separately approved dependency environment is copied.
No `.pth` is executed. Pywin32's known module paths/DLL directory are explicitly
inventoried/configured rather than relying on `site` or pywin32 registration.
Optional `--bindings` supplies an independently approved binding JSON array;
the default is empty, **not** a default account, connection or permission.
Store user configuration, databases, logs and keys outside the immutable tree.

An authorized signing operator can use an existing non-exportable RSA
code-signing key in `LocalMachine\My` (including a supported CNG/KSP key):

```powershell
& "C:\verified-tools\Shovel.Wam.exe" deployment inventory `
  ".tools\installation-review\installation-payload.json" ".tools\installation-review\payload"
& "C:\verified-tools\Shovel.Wam.exe" deployment sign `
  ".tools\installation-review\installation-payload.json" `
  "<approved-certificate-der-sha256>" ".tools\installation-envelope.json"
```

`inventory` is a read-only candidate check using the same native runtime schema
and complete-file verifier as activation; it never creates machine trust or
claims organization approval.
The command never generates/imports a certificate or exports a private key.
It signs the exact payload bytes and verifies its signature, pin, code-signing
EKU, certificate/policy lifetime and minimum RSA size. An organization can
instead produce the same envelope format in its existing signing service.
Organization approval must review the payload, dependency inventory and
provenance; signing a malformed/incomplete input is not deployment acceptance.

**Only after separate machine-provisioning approval**, an elevated
administrator uses the verified deployment tool:

```powershell
& "C:\verified-tools\Shovel.Wam.exe" deployment initialize `
  "C:\Program Files\Shovel" "<approved-certificate-der-sha256>" --approve-machine-trust
& "C:\verified-tools\Shovel.Wam.exe" deployment activate `
  ".tools\installation-review\payload" ".tools\installation-envelope.json" 1
```

Initialize is first-install-only: it rejects an existing key/directory and
creates a protected store, lock, policies/packages directories and HKLM state.
Only SYSTEM/Administrators can write; Users receive read/execute only, with
protected DACLs and administrator ownership. Initialization creates no active
installation or enterprise grants. Activation checks expected generation,
organization signer, expiration, complete inventory and exact destination
`<storeRoot>\packages\<revision>`. It copies into a fresh protected staging
directory, rehashes the copied bytes, publishes an immutable version/policy, and
only then commits the single HKLM State value. User-writable ancestors, child
deletion rights, alternate streams, path aliases and junctions are rejected.

Upgrade assembles/signs a **strictly higher revision** for its corresponding
new immutable directory, then activates with the current generation. Existing
directories/files are never overwritten. An interrupted pre-commit deployment
leaves the old anchor active. Retrying may reconcile an already-published
directory/envelope only after exact byte/ACL revalidation; corrupt staging is
never trusted, automatically deleted or repaired. After a successful commit,
use the new protected interpreter/bootstrap and restart/revalidate the Host.
Machine-state and credential-release leases serialize administrative writes;
an overlapping writer fails explicitly and must be retried, not merged.

Revocation and signer rotation require the current expected generation:

```powershell
& "C:\verified-tools\Shovel.Wam.exe" deployment revoke-envelope "<envelope-sha256>" <generation>
& "C:\verified-tools\Shovel.Wam.exe" deployment add-signer "<new-approved-certificate-sha256>" <generation>
& "C:\verified-tools\Shovel.Wam.exe" deployment revoke-signer "<old-certificate-sha256>" <generation>
```

Each successful transition increments generation. Matching revocation clears
activation, retains the highest revision and permanent tombstones, and fences
old Hosts/late credentials. The organization-policy loader also rechecks the
Host's initial installation generation/digest/expiry on every policy resolution,
so a token already issued before revocation cannot authorize a later result
release through an old Host. This does not undo an already-dispatched request.
Adding a signer does not un-revoke it or activate
anything. Rotate to a new approved signer before revoking the old one.
Replayed/older signed manifests and stale concurrent writers are rejected.
Revision is the organization's monotonic release sequence, not a heuristic
comparison of arbitrary package version strings. There is no reset,
auto-downgrade, uninstall erasure claim or permission-enabling command.

Offline tests cover signature/pin tampering, complete interpreter/native/
dependency inventory, ACL descriptors and real unprotected-file rejection,
upgrade/replay/stale-writer/retained-state crash windows, revocation and signer
rotation. Actual organization signing, elevated Program Files/HKLM deployment,
power-loss recovery and real installed WAM remain operator acceptance gates.
Local state cannot defend against an administrator/OS restoring an entire old
hive/disk snapshot; that requires an independently approved TPM/remote monotonic
authority. Windows system DLLs/kernel and OS servicing remain trusted platform
dependencies, not files claimed to be bundled or physically erased.
