using System.IO;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace Shovel.Wam.Tests;

[TestClass]
public sealed class DeploymentTests
{
    private static MachineTrust Initial(string pin) => new()
    {
        Schema = 2,
        StoreRoot = @"C:\Program Files\ShovelSynthetic",
        Generation = 1,
        HighestRevision = 0,
        Signers = [pin],
        RevokedSigners = [],
        RevokedEnvelopes = [],
    };

    private static Installation Policy(MachineTrust state, int revision, string? root = null)
    {
        var names = new[]
        {
            @"python\python.exe", @"python\python314.dll", @"python\python3.dll",
            @"python\Lib\encodings\__init__.py", @"python\Lib\json\__init__.py",
            @"python\Lib\site-packages\shovel-0.1.2.dist-info\METADATA",
            @"native\Shovel.Wam.exe", @"native\coreclr.dll", @"native\hostpolicy.dll",
            @"native\System.Private.CoreLib.dll", @"native\PresentationFramework.dll",
            @"native\Shovel.Wam.runtimeconfig.json", "host_bootstrap.py",
        };
        return new Installation
        {
            Root = root ?? state.PackagePath(revision),
            Python = @"python\python.exe",
            Native = @"native\Shovel.Wam.exe",
            Bootstrap = "host_bootstrap.py",
            ModulePaths = [@"python\Lib", @"python\Lib\site-packages", @"python\DLLs"],
            Files = names.ToDictionary(name => name, _ => Installation.Hex(SHA256.HashData([42]))),
            Bindings = [],
            Revision = revision,
            ValidUntil = DateTimeOffset.UtcNow.AddMinutes(10).ToUnixTimeSeconds(),
            Runtime = new RuntimeInventory
            {
                PythonVersion = "3.14.2",
                PythonArchitecture = "win-amd64",
                PythonDll = @"python\python314.dll",
                StandardLibrary = @"python\Lib",
                SitePackages = @"python\Lib\site-packages",
                DotnetVersion = "10.0.14",
                LockSha256 = new string('a', 64),
                Distributions = [new RuntimeDistribution
                {
                    Name = "shovel", Version = "0.1.2",
                    Metadata = @"python\Lib\site-packages\shovel-0.1.2.dist-info\METADATA",
                }],
            },
        };
    }

    private sealed class Signer : IDisposable
    {
        private readonly RSA rsa = RSA.Create(2048);
        private readonly X509Certificate2 certificate;
        internal string Pin => Installation.Hex(certificate.GetCertHash(HashAlgorithmName.SHA256));
        internal Signer()
        {
            var request = new CertificateRequest("CN=Offline-Installation-Fixture", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
            request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(
                new OidCollection { new("1.3.6.1.5.5.7.3.3") }, true));
            certificate = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddHours(1));
        }
        internal byte[] Sign(Installation policy)
            => SignPayload(policy);
        internal byte[] SignPayload(object policy)
        {
            var payload = JsonSerializer.SerializeToUtf8Bytes(policy, Installation.Options);
            return JsonSerializer.SerializeToUtf8Bytes(new
            {
                payload = Convert.ToBase64String(payload),
                signature = Convert.ToBase64String(rsa.SignData(payload, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1)),
                certificate = Convert.ToBase64String(certificate.Export(X509ContentType.Cert)),
            });
        }
        public void Dispose() { certificate.Dispose(); rsa.Dispose(); }
    }

    [TestMethod]
    public void RolloutRequiresSeparateApprovalEnableAndMonotonicGeneration()
    {
        var state = new RolloutState
        {
            OrganizationId = "organization",
            ApprovalSigner = new string('a', 64),
            VerifierSigner = new string('b', 64),
        };
        Assert.ThrowsExactly<InvalidDataException>(() => state.Advance(1, "enable"));
        var approved = state.Advance(1, "approve", new string('c', 64), new string('d', 64), 1);
        Assert.AreEqual("approved", approved.Status);
        Assert.ThrowsExactly<InvalidDataException>(() => approved.Advance(1, "enable"));
        var enabled = approved.Advance(2, "enable", now: 1000);
        var paused = enabled.Advance(3, "pause");
        var resumed = paused.Advance(4, "enable", now: 1001);
        Assert.AreEqual("enabled", resumed.Status);
        Assert.AreEqual(5, resumed.Generation);
        Assert.AreNotEqual(enabled.Generation, resumed.Generation);
        var revoked = resumed.Advance(5, "revoke");
        Assert.ThrowsExactly<InvalidDataException>(() => revoked.Advance(6, "enable"));
        Assert.ThrowsExactly<InvalidDataException>(() =>
            revoked.Advance(6, "approve", new string('c', 64), new string('d', 64), 2));
        Assert.ThrowsExactly<InvalidDataException>(() =>
            revoked.Advance(6, "approve", new string('e', 64), new string('f', 64), 1));
        var next = revoked.Advance(6, "approve", new string('e', 64), new string('f', 64), 2);
        Assert.IsTrue(next.Revoked.Contains(new string('c', 64)));
        Assert.AreEqual("approved", next.Status);
    }

    [TestMethod]
    public void RolloutRejectsUntrustedExpiredAndWrongInstallationReceipts()
    {
        using var approver = new Signer();
        using var verifier = new Signer();
        var state = new RolloutState
        {
            OrganizationId = "organization",
            ApprovalSigner = approver.Pin,
            VerifierSigner = verifier.Pin,
        };
        var installation = Initial(approver.Pin).Advance(1, new ActiveInstallation
        {
            Revision = 1,
            SignerSha256 = approver.Pin,
            EnvelopeSha256 = new string('a', 64),
        });
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var report = verifier.SignPayload(new
        {
            purpose = "shovel.enterprise.live-report.v1",
            organization_id = "organization",
            revision = 1,
            observed_at = now - 1,
            expires_at = now + 60,
        });
        byte[] Approval(long expires, string digest) => approver.SignPayload(new
        {
            purpose = "shovel.enterprise.rollout-approval.v1",
            organization_id = "organization",
            revision = 1,
            issued_at = now,
            expires_at = expires,
            report_digest = Installation.Hex(SHA256.HashData(report)),
            plan = new { installation_digest = digest },
        });
        Assert.AreEqual(1, Rollout.Verify(state, Approval(now + 60, new string('a', 64)), report, installation));
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Rollout.Verify(state, Approval(now - 1, new string('a', 64)), report, installation));
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Rollout.Verify(state, Approval(now + 60, new string('b', 64)), report, installation));
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Rollout.Verify(state with { VerifierSigner = approver.Pin },
                Approval(now + 60, new string('a', 64)), report, installation));
    }

    [TestMethod]
    public void ControlledUpgradeRejectsReplayDowngradeAndStaleConcurrentWriter()
    {
        using var signer = new Signer();
        var initial = Initial(signer.Pin);
        var envelope = signer.Sign(Policy(initial, 1));
        var (_, first) = Deployment.Prepare(initial, envelope, 1);
        Assert.AreEqual(2, first.Generation);
        Assert.AreEqual(1, first.HighestRevision);
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(first, envelope, 2));
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(first, signer.Sign(Policy(first, 2)), 1));
        var (_, second) = Deployment.Prepare(first, signer.Sign(Policy(first, 2)), 2);
        Assert.AreEqual(2, second.HighestRevision);
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(second, envelope, 3));
        // A crash before publishing the new State value leaves the old immutable snapshot active.
        Assert.AreEqual(1, first.Active!.Revision);
        Assert.AreEqual(2, second.Active!.Revision);
    }

    [TestMethod]
    public void RevocationAndSignerRotationRetainHighWaterAndTombstones()
    {
        using var signer = new Signer();
        using var replacement = new Signer();
        var initial = Initial(signer.Pin);
        var envelope = signer.Sign(Policy(initial, 4));
        var (_, installed) = Deployment.Prepare(initial, envelope, 1);
        var revoked = installed.Advance(2, null, installed.Active!.EnvelopeSha256);
        Assert.IsNull(revoked.Active);
        Assert.AreEqual(4, revoked.HighestRevision);
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(revoked, envelope, 3));
        var rotated = revoked.Advance(3, null, addSigner: replacement.Pin);
        var signerRevoked = rotated.Advance(4, null, revokeSigner: signer.Pin);
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Deployment.Prepare(signerRevoked, signer.Sign(Policy(signerRevoked, 5)), 5));
        var (_, next) = Deployment.Prepare(signerRevoked, replacement.Sign(Policy(signerRevoked, 5)), 5);
        Assert.IsTrue(next.RevokedEnvelopes.Contains(installed.Active.EnvelopeSha256));
        Assert.IsTrue(next.RevokedSigners.Contains(signer.Pin));
        var revokedAgain = next.Advance(6, null, revokeSigner: replacement.Pin);
        Assert.IsNull(revokedAgain.Active);
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Deployment.Prepare(revokedAgain, replacement.Sign(Policy(revokedAgain, 6)), 7));
    }

    [TestMethod]
    public void CompleteRuntimeAndMachineDestinationAreMandatory()
    {
        using var signer = new Signer();
        var state = Initial(signer.Pin);
        var policy = Policy(state, 1);
        foreach (var file in new[] { @"python\python314.dll", @"native\coreclr.dll",
            @"python\Lib\site-packages\shovel-0.1.2.dist-info\METADATA" })
        {
            var digest = policy.Files[file];
            policy.Files.Remove(file);
            Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(state, signer.Sign(policy), 1));
            policy.Files[file] = digest;
        }
        Assert.ThrowsExactly<InvalidDataException>(() =>
            Deployment.Prepare(state, signer.Sign(Policy(state, 1, @"C:\user-selected")), 1));
        using var unknown = new Signer();
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.Prepare(state, unknown.Sign(policy), 1));
        var tampered = signer.Sign(policy);
        tampered[^2] ^= 1;
        Assert.Throws<JsonException>(() => Deployment.Prepare(state, tampered, 1));
    }

    [TestMethod]
    public void InventoryRejectsChangedMissingAndUnlistedRuntimeFiles()
    {
        var root = Path.Combine(Path.GetTempPath(), "shovel-install-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var policy = Policy(Initial(new string('a', 64)), 1, root);
            Directory.CreateDirectory(Path.Combine(root, "python", "DLLs"));
            foreach (var name in policy.Files.Keys)
            {
                var path = Path.Combine(root, name);
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllBytes(path, [42]);
            }
            policy.VerifyFiles(requireProtected: false);
            var dependency = Path.Combine(root, @"native\coreclr.dll");
            File.WriteAllBytes(dependency, [43]);
            Assert.ThrowsExactly<InvalidDataException>(() => policy.VerifyFiles(requireProtected: false));
            File.WriteAllBytes(dependency, [42]);
            File.WriteAllBytes(Path.Combine(root, "unlisted.dll"), [42]);
            Assert.ThrowsExactly<InvalidDataException>(() => policy.VerifyFiles(requireProtected: false));
            File.Delete(Path.Combine(root, "unlisted.dll"));
            File.Delete(dependency);
            Assert.ThrowsExactly<InvalidDataException>(() => policy.VerifyFiles(requireProtected: false));
        }
        finally { Directory.Delete(root, true); }
    }

    [TestMethod]
    public void RuntimeDependencyPathsCannotBeReplacedByPthOrExternalImports()
    {
        var policy = Policy(Initial(new string('a', 64)), 1);
        var node = JsonSerializer.SerializeToNode(policy, Installation.Options)!;
        const string metadata = @"python\Lib\site-packages\pywin32-312.dist-info\METADATA";
        node["files"]![metadata] = new string('a', 64);
        node["runtime"]!["distributions"]!.AsArray().Add(JsonSerializer.SerializeToNode(new RuntimeDistribution
        {
            Name = "pywin32",
            Version = "312",
            Metadata = metadata,
        }, Installation.Options));
        node["modulePaths"] = JsonSerializer.SerializeToNode(new[] { @"python\Lib", @"python\Lib\site-packages",
            @"python\DLLs", @"python\Lib\site-packages\win32", @"python\Lib\site-packages\win32\lib",
            @"python\Lib\site-packages\Pythonwin" });
        node["runtime"]!["dllDirectories"] = JsonSerializer.SerializeToNode(new[] { @"python\Lib\site-packages\pywin32_system32" });
        Deployment.ReadPayload(JsonSerializer.SerializeToUtf8Bytes(node));
        node["modulePaths"]!.AsArray().Add(@"C:\untrusted-modules");
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.ReadPayload(JsonSerializer.SerializeToUtf8Bytes(node)));
        node["modulePaths"]!.AsArray().RemoveAt(6);
        node["runtime"]!["dllDirectories"] = JsonSerializer.SerializeToNode(Array.Empty<string>());
        Assert.ThrowsExactly<InvalidDataException>(() => Deployment.ReadPayload(JsonSerializer.SerializeToUtf8Bytes(node)));
    }

    [TestMethod]
    public void AclChecksRejectNullDaclUserOwnerWriteAndParentDeletion()
    {
        Installation.DemandDescriptor(new RawSecurityDescriptor(
            "O:BAG:BAD:P(A;;FA;;;BA)(A;;FA;;;SY)(A;;FRFX;;;BU)"), 0x500d0156);
        Installation.DemandDescriptor(new RawSecurityDescriptor(
            "O:BAG:BAD:P(A;;KA;;;BA)(A;;KR;;;BU)"), 0x500d0026);
        foreach (var sddl in new[] { "O:BUG:BUD:P(A;;FRFX;;;BU)", "O:BAG:BAD:NO_ACCESS_CONTROL",
            "O:BAG:BAD:P(A;;FW;;;BU)", "O:BAG:BAD:P(A;;0x40;;;BU)" })
        {
            Assert.ThrowsExactly<InvalidDataException>(() =>
                Installation.DemandDescriptor(new RawSecurityDescriptor(sddl), 0x500d0156));
        }
        Assert.ThrowsExactly<InvalidDataException>(() => Installation.DemandDescriptor(
            new RawSecurityDescriptor("O:BAG:BAD:P(A;;0x40;;;BU)"), 0x500d0040));
    }

    [TestMethod]
    public void MachineStateRejectsLegacyDuplicateAndMalformedAnchors()
    {
        Assert.ThrowsExactly<InvalidDataException>(() => MachineTrust.Parse("{\"schema\":2,\"schema\":2}"));
        Assert.ThrowsExactly<JsonException>(() => MachineTrust.Parse("{\"revision\":1}"));
        var state = Initial(new string('a', 64));
        var json = JsonSerializer.Serialize(state, Installation.Options);
        Assert.AreEqual(1, MachineTrust.Parse(json).Generation);
        Assert.ThrowsExactly<InvalidDataException>(() =>
            MachineTrust.Parse(json.Replace("\"schema\":2", "\"schema\":1", StringComparison.Ordinal)));
    }

    [TestMethod]
    [DataRow(@"python\..\payload.dll")]
    [DataRow(@"python\bad.\module.py")]
    [DataRow(@"python\bad \module.py")]
    [DataRow(@"python\NUL.txt")]
    [DataRow(@"python\module.py:alternate")]
    public void WindowsPathAliasesAreRejected(string path) =>
        Assert.ThrowsExactly<InvalidDataException>(() => Installation.ValidateRelative(path));
}
