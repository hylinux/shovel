using System.IO;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;
using Microsoft.Win32;

namespace Shovel.Wam;

internal sealed record RolloutState
{
    public int Schema { get; init; } = 1;
    public required string OrganizationId { get; init; }
    public required string ApprovalSigner { get; init; }
    public required string VerifierSigner { get; init; }
    public int Generation { get; init; } = 1;
    public int HighestRevision { get; init; }
    public string Status { get; init; } = "unapproved";
    public string? ApprovalDigest { get; init; }
    public string? ReportDigest { get; init; }
    public long? EnabledAt { get; init; }
    public string[] Revoked { get; init; } = [];

    internal void Validate()
    {
        if (Schema != 1 || Generation < 1 || HighestRevision < 0
            || string.IsNullOrWhiteSpace(OrganizationId) || OrganizationId.Length > 200
            || !MachineTrust.IsDigest(ApprovalSigner) || !MachineTrust.IsDigest(VerifierSigner)
            || ApprovalSigner == VerifierSigner
            || Revoked.Length > 10000 || Revoked.Distinct().Count() != Revoked.Length
            || Revoked.Any(item => !MachineTrust.IsDigest(item))
            || Status is not ("unapproved" or "approved" or "enabled" or "paused" or "revoked")
            || (Status == "unapproved" && (HighestRevision != 0 || ApprovalDigest is not null || ReportDigest is not null))
            || (Status != "unapproved" && (HighestRevision < 1
                || !MachineTrust.IsDigest(ApprovalDigest ?? "") || !MachineTrust.IsDigest(ReportDigest ?? "")))
            || (Status == "enabled" && EnabledAt is null)
            || (Status is "approved" or "enabled" or "paused" && Revoked.Contains(ApprovalDigest)))
        {
            throw new InvalidDataException();
        }
    }

    internal RolloutState Advance(int expected, string action, string? approval = null,
        string? report = null, int revision = 0, long now = 0)
    {
        Validate();
        if (expected != Generation) { throw new InvalidDataException(); }
        RolloutState next;
        if (action == "approve" && MachineTrust.IsDigest(approval ?? "")
            && MachineTrust.IsDigest(report ?? "") && revision > HighestRevision && !Revoked.Contains(approval))
        {
            next = this with { Status = "approved", ApprovalDigest = approval, ReportDigest = report, HighestRevision = revision };
        }
        else if (action == "enable" && Status is "approved" or "paused")
        {
            next = this with { Status = "enabled", EnabledAt = now };
        }
        else if (action == "pause" && Status == "enabled")
        {
            next = this with { Status = "paused" };
        }
        else if (action == "revoke" && Status is "approved" or "enabled" or "paused")
        {
            next = this with { Status = "revoked", Revoked = [.. Revoked, ApprovalDigest!] };
        }
        else { throw new InvalidDataException(); }
        next = next with { Generation = checked(Generation + 1) };
        next.Validate();
        return next;
    }
}

internal static class Rollout
{
    internal const string RegistryPath = @"SOFTWARE\Shovel\RolloutTrust";

    internal static RolloutState Read()
    {
        Installation.DemandProtected(@"MACHINE\" + RegistryPath, 4);
        using var machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
        using var key = machine.OpenSubKey(RegistryPath) ?? throw new InvalidDataException();
        var raw = key.GetValue("State") as string ?? throw new InvalidDataException();
        if (raw.Length > 1024 * 1024) { throw new InvalidDataException(); }
        using var document = JsonDocument.Parse(raw);
        Installation.RejectDuplicates(document.RootElement);
        var state = document.Deserialize<RolloutState>(Installation.Options) ?? throw new InvalidDataException();
        state.Validate();
        return state;
    }

    internal static int Run(string[] args)
    {
        try
        {
            if (args is ["show"])
            {
                var current = Read();
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    scope = "enterprise_rollout",
                    status = current.Status,
                    generation = current.Generation,
                    revision = current.HighestRevision,
                    approval_sha256 = current.ApprovalDigest,
                    report_sha256 = current.ReportDigest,
                    production_ready = false,
                }));
                return 0;
            }
            if (args is ["preview", var previewApproval, var previewReport])
            {
                var state = Read();
                var machine = MachineTrust.Read();
                var approval = MachineTrust.ReadEnvelope(previewApproval);
                var report = MachineTrust.ReadEnvelope(previewReport);
                var revision = Verify(state, approval, report, machine);
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    scope = "enterprise_rollout",
                    status = "review_only",
                    generation = state.Generation,
                    revision,
                    approval_sha256 = Installation.Hex(SHA256.HashData(approval)),
                    report_sha256 = Installation.Hex(SHA256.HashData(report)),
                    production_ready = false,
                }));
                return 0;
            }
            using var identity = WindowsIdentity.GetCurrent();
            if (!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
            {
                throw new InvalidDataException();
            }
            var installation = MachineTrust.Read();
            installation.LoadActive().VerifyFiles();
            var lockPath = Path.Combine(installation.StoreRoot, "deployment.lock");
            Installation.DemandAncestorsProtected(lockPath);
            Installation.DemandProtected(lockPath, 1);
            using var lease = new FileStream(lockPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            MachineTrust.DemandUnchanged(installation);
            if (args is ["initialize", var organization, var approver, var verifier, "--approve-rollout-trust"])
            {
                var state = new RolloutState
                {
                    OrganizationId = organization,
                    ApprovalSigner = approver,
                    VerifierSigner = verifier,
                };
                state.Validate();
                using var machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
                using var existing = machine.OpenSubKey(RegistryPath);
                if (existing is not null) { throw new InvalidDataException(); }
                using var key = machine.CreateSubKey(RegistryPath, true);
                var acl = new RegistrySecurity();
                acl.SetSecurityDescriptorSddlForm("O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;BU)");
                key.SetAccessControl(acl);
                Installation.DemandProtected(@"MACHINE\" + RegistryPath, 4);
                Write(key, state);
            }
            else
            {
                var state = Read();
                RolloutState next;
                if (args is ["approve", var approvalPath, var reportPath, var generation,
                    var expectedApproval, var expectedReport, "--confirm"])
                {
                    var approval = MachineTrust.ReadEnvelope(approvalPath);
                    var report = MachineTrust.ReadEnvelope(reportPath);
                    if (Installation.Hex(SHA256.HashData(approval)) != expectedApproval
                        || Installation.Hex(SHA256.HashData(report)) != expectedReport)
                    {
                        throw new InvalidDataException();
                    }
                    var revision = Verify(state, approval, report, installation);
                    next = state.Advance(int.Parse(generation, System.Globalization.CultureInfo.InvariantCulture),
                        "approve", Installation.Hex(SHA256.HashData(approval)),
                        Installation.Hex(SHA256.HashData(report)), revision);
                    Save(installation, approval);
                    Save(installation, report);
                }
                else if (args is [var action, var generationText, var expectedDigest, "--confirm"]
                    && action is "enable" or "pause" or "revoke")
                {
                    if (state.ApprovalDigest != expectedDigest) { throw new InvalidDataException(); }
                    if (action == "enable")
                    {
                        var approval = MachineTrust.ReadEnvelope(installation.EnvelopePath(state.ApprovalDigest!));
                        var report = MachineTrust.ReadEnvelope(installation.EnvelopePath(state.ReportDigest!));
                        if (Installation.Hex(SHA256.HashData(approval)) != state.ApprovalDigest
                            || Installation.Hex(SHA256.HashData(report)) != state.ReportDigest
                            || Verify(state, approval, report, installation) != state.HighestRevision)
                        {
                            throw new InvalidDataException();
                        }
                    }
                    next = state.Advance(int.Parse(generationText, System.Globalization.CultureInfo.InvariantCulture),
                        action, now: DateTimeOffset.UtcNow.ToUnixTimeSeconds());
                }
                else { throw new InvalidDataException(); }
                if (JsonSerializer.Serialize(Read(), Installation.Options) != JsonSerializer.Serialize(state, Installation.Options))
                {
                    throw new InvalidDataException();
                }
                MachineTrust.DemandUnchanged(installation);
                using var machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
                using var key = machine.OpenSubKey(RegistryPath, true) ?? throw new InvalidDataException();
                Write(key, next);
            }
            Console.WriteLine("{\"scope\":\"enterprise_rollout\",\"status\":\"completed\",\"production_ready\":false}");
            return 0;
        }
        catch (Exception)
        {
            Console.Error.WriteLine("{\"scope\":\"enterprise_rollout\",\"status\":\"blocked\",\"production_ready\":false}");
            return 3;
        }
    }

    internal static int Verify(RolloutState state, byte[] approvalBytes, byte[] reportBytes, MachineTrust installation)
    {
        if (approvalBytes.Length > 1024 * 1024 || reportBytes.Length > 1024 * 1024)
        {
            throw new InvalidDataException();
        }
        var (approval, approvalExpiry) = Installation.VerifySignedPayload(approvalBytes, state.ApprovalSigner);
        var (report, reportExpiry) = Installation.VerifySignedPayload(reportBytes, state.VerifierSigner);
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var revision = approval.GetProperty("revision").GetInt32();
        if (approval.GetProperty("purpose").GetString() != "shovel.enterprise.rollout-approval.v1"
            || report.GetProperty("purpose").GetString() != "shovel.enterprise.live-report.v1"
            || approval.GetProperty("organization_id").GetString() != state.OrganizationId
            || report.GetProperty("organization_id").GetString() != state.OrganizationId
            || report.GetProperty("revision").GetInt32() != revision
            || report.GetProperty("observed_at").GetDouble() > approval.GetProperty("issued_at").GetDouble()
            || approval.GetProperty("issued_at").GetDouble() > now
            || approval.GetProperty("expires_at").GetDouble() <= now
            || approval.GetProperty("expires_at").GetDouble() > Math.Min(approvalExpiry, report.GetProperty("expires_at").GetDouble())
            || report.GetProperty("expires_at").GetDouble() > reportExpiry
            || approval.GetProperty("report_digest").GetString() != Installation.Hex(SHA256.HashData(reportBytes))
            || approval.GetProperty("plan").GetProperty("installation_digest").GetString() != installation.Active?.EnvelopeSha256
            || state.Revoked.Contains(Installation.Hex(SHA256.HashData(approvalBytes))))
        {
            throw new InvalidDataException();
        }
        // The Python verifier additionally checks exact plan/evidence hashes and every fixed control.
        // Machine enablement never substitutes for that Runtime check.
        return revision;
    }

    private static void Save(MachineTrust installation, byte[] bytes)
    {
        var path = installation.EnvelopePath(Installation.Hex(SHA256.HashData(bytes)));
        if (File.Exists(path))
        {
            Installation.DemandProtected(path, 1);
            if (!MachineTrust.ReadEnvelope(path).SequenceEqual(bytes)) { throw new InvalidDataException(); }
            return;
        }
        using (var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None,
            4096, FileOptions.WriteThrough))
        {
            file.Write(bytes);
            file.Flush(true);
        }
        Deployment.ProtectFile(path);
    }

    private static void Write(RegistryKey key, RolloutState state)
    {
        state.Validate();
        key.SetValue("State", JsonSerializer.Serialize(state, Installation.Options), RegistryValueKind.String);
        key.Flush();
    }
}
