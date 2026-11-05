using System.IO;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Security.Principal;
using System.Text.Json;
using Microsoft.Win32;

namespace Shovel.Wam;

internal static class Deployment
{
    private const string FileSddl = "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;BU)";
    private const string DirectorySddl = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FRFX;;;BU)";
    private const string RegistrySddl = "O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;BU)";

    internal static int Run(string[] args)
    {
        try
        {
            if (args is ["inventory", var candidate, var sourceRoot])
            {
                ReadPayload(MachineTrust.ReadEnvelope(candidate)).VerifyFiles(Path.GetFullPath(sourceRoot), false);
            }
            else if (args is ["sign", var payload, var pin, var output])
            {
                Sign(payload, pin, output);
            }
            else
            {
                using var identity = WindowsIdentity.GetCurrent();
                if (!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
                {
                    throw new InvalidDataException();
                }
                using var serialization = new DeploymentLock();
                if (args is ["initialize", var storeRoot, var signer, "--approve-machine-trust"])
                {
                    Initialize(storeRoot, signer);
                }
                else
                {
                    var state = MachineTrust.Read();
                    Installation.DemandProtected(state.StoreRoot, 1);
                    Installation.DemandAncestorsProtected(state.StoreRoot);
                    using var lease = new FileStream(Path.Combine(state.StoreRoot, "deployment.lock"),
                        FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                    state = MachineTrust.Read();
                    if (args is ["activate", var source, var envelope, var expected])
                    {
                        Activate(state, source, envelope, int.Parse(expected, System.Globalization.CultureInfo.InvariantCulture));
                    }
                    else if (args is [var action, var digest, var generation]
                        && action is "revoke-envelope" or "revoke-signer" or "add-signer")
                    {
                        Commit(state, state.Advance(int.Parse(generation, System.Globalization.CultureInfo.InvariantCulture),
                            null, action == "revoke-envelope" ? digest : null,
                            action == "revoke-signer" ? digest : null, action == "add-signer" ? digest : null));
                    }
                    else { throw new InvalidDataException(); }
                }
            }
            Console.WriteLine(args[0] == "inventory"
                ? "{\"scope\":\"controlled_installation\",\"status\":\"candidate_verified\",\"production_ready\":false}"
                : "{\"scope\":\"controlled_installation\",\"status\":\"completed\",\"production_ready\":false}");
            return 0;
        }
        catch (Exception)
        {
            Console.Error.WriteLine("{\"scope\":\"controlled_installation\",\"status\":\"blocked\",\"production_ready\":false}");
            return 3;
        }
    }

    private static void Initialize(string root, string signer)
    {
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        if (!MachineTrust.IsDigest(signer) || !Path.IsPathFullyQualified(root)
            || root != Path.TrimEndingDirectorySeparator(Path.GetFullPath(root))
            || !root.StartsWith(programFiles + "\\", StringComparison.OrdinalIgnoreCase)
            || Directory.Exists(root) || File.Exists(root))
        {
            throw new InvalidDataException();
        }
        var parent = Path.GetDirectoryName(root) ?? throw new InvalidDataException();
        Installation.DemandProtected(parent, 1);
        Installation.DemandAncestorsProtected(parent);
        using var machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);
        using var existing = machine.OpenSubKey(Installation.RegistryPath);
        if (existing is not null) { throw new InvalidDataException(); }
        ProtectDirectory(Directory.CreateDirectory(root));
        ProtectDirectory(Directory.CreateDirectory(Path.Combine(root, "packages")));
        ProtectDirectory(Directory.CreateDirectory(Path.Combine(root, "policies")));
        using (var stream = new FileStream(Path.Combine(root, "deployment.lock"), FileMode.CreateNew,
            FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
        {
            stream.Flush(true);
        }
        ProtectFile(Path.Combine(root, "deployment.lock"));
        using var key = machine.CreateSubKey(Installation.RegistryPath, true);
        var security = new RegistrySecurity();
        security.SetSecurityDescriptorSddlForm(RegistrySddl);
        key.SetAccessControl(security);
        Installation.DemandProtected(@"MACHINE\" + Installation.RegistryPath, 4);
        var state = new MachineTrust
        {
            Schema = 2,
            StoreRoot = root,
            Generation = 1,
            HighestRevision = 0,
            Signers = [signer],
            RevokedSigners = [],
            RevokedEnvelopes = [],
        };
        state.Validate();
        key.SetValue("State", JsonSerializer.Serialize(state, Installation.Options), RegistryValueKind.String);
        key.Flush();
    }

    private static void Sign(string payloadPath, string signer, string output)
    {
        if (!MachineTrust.IsDigest(signer)) { throw new InvalidDataException(); }
        var payload = MachineTrust.ReadEnvelope(payloadPath);
        ReadPayload(payload);
        using var store = new X509Store(StoreName.My, StoreLocation.LocalMachine);
        store.Open(OpenFlags.ReadOnly | OpenFlags.OpenExistingOnly);
        var matches = store.Certificates.Where(certificate =>
            Installation.Hex(certificate.GetCertHash(HashAlgorithmName.SHA256)) == signer).ToArray();
        if (matches.Length != 1) { throw new InvalidDataException(); }
        using var certificate = matches[0];
        using var rsa = certificate.GetRSAPrivateKey() ?? throw new InvalidDataException();
        var envelope = JsonSerializer.SerializeToUtf8Bytes(new
        {
            payload = Convert.ToBase64String(payload),
            signature = Convert.ToBase64String(rsa.SignData(payload, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1)),
            certificate = Convert.ToBase64String(certificate.Export(X509ContentType.Cert)),
        });
        using var content = JsonDocument.Parse(payload);
        Installation.VerifyEnvelope(envelope, signer, content.RootElement.GetProperty("revision").GetInt32());
        using var file = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough);
        file.Write(envelope);
        file.Flush(true);
    }

    internal static Installation ReadPayload(byte[] bytes)
    {
        using var document = JsonDocument.Parse(bytes);
        Installation.RejectDuplicates(document.RootElement);
        var installation = document.Deserialize<Installation>(Installation.Options) ?? throw new InvalidDataException();
        installation.VerifyRuntime();
        return installation;
    }

    internal static (Installation Policy, MachineTrust Next) Prepare(MachineTrust state,
        byte[] envelope, int expectedGeneration)
    {
        if (expectedGeneration != state.Generation) { throw new InvalidDataException(); }
        using var document = JsonDocument.Parse(envelope);
        Installation.RejectDuplicates(document.RootElement);
        using var certificate = X509CertificateLoader.LoadCertificate(
            Convert.FromBase64String(document.RootElement.GetProperty("certificate").GetString() ?? ""));
        var signer = Installation.Hex(certificate.GetCertHash(HashAlgorithmName.SHA256));
        if (!state.Signers.Contains(signer) || state.RevokedSigners.Contains(signer)) { throw new InvalidDataException(); }
        using var payload = JsonDocument.Parse(Convert.FromBase64String(document.RootElement.GetProperty("payload").GetString() ?? ""));
        var revision = payload.RootElement.GetProperty("revision").GetInt32();
        var policy = Installation.VerifyEnvelope(envelope, signer, revision);
        policy.VerifyRuntime();
        if (!string.Equals(policy.Root, state.PackagePath(revision), StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException();
        }
        var next = state.Advance(expectedGeneration, new ActiveInstallation
        {
            EnvelopeSha256 = Installation.Hex(SHA256.HashData(envelope)),
            SignerSha256 = signer,
            Revision = revision,
        });
        return (policy, next);
    }

    private static void Activate(MachineTrust state, string source, string envelopePath, int expectedGeneration)
    {
        var bytes = MachineTrust.ReadEnvelope(envelopePath);
        var (policy, next) = Prepare(state, bytes, expectedGeneration);
        policy.VerifyFiles(Path.GetFullPath(source), false);
        var destination = state.PackagePath(policy.Revision);
        if (File.Exists(destination)) { throw new InvalidDataException(); }
        if (!Directory.Exists(destination))
        {
            var staging = Path.Combine(state.StoreRoot, "packages", ".staging-" + Guid.NewGuid().ToString("N"));
            ProtectDirectory(Directory.CreateDirectory(staging));
            CopyInventory(policy, source, staging);
            policy.VerifyFiles(staging);
            Directory.Move(staging, destination);
        }
        policy.VerifyFiles();
        var saved = state.EnvelopePath(next.Active!.EnvelopeSha256);
        if (!File.Exists(saved))
        {
            using (var file = new FileStream(saved, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
            {
                file.Write(bytes);
                file.Flush(true);
            }
            ProtectFile(saved);
        }
        else
        {
            Installation.DemandProtected(saved, 1);
            if (!MachineTrust.ReadEnvelope(saved).SequenceEqual(bytes)) { throw new InvalidDataException(); }
        }
        // The atomic registry value is the commit point; interrupted staging never authorizes a package.
        Commit(state, next);
    }

    internal static void CopyInventory(Installation policy, string source, string staging)
    {
        foreach (var pair in policy.Files)
        {
            var from = policy.FilePath(pair.Key, source);
            var to = Path.Combine(staging, pair.Key);
            for (var directory = Directory.GetParent(to); directory is not null
                && directory.FullName.StartsWith(staging + "\\", StringComparison.OrdinalIgnoreCase); directory = directory.Parent)
            {
                ProtectDirectory(Directory.CreateDirectory(directory.FullName));
            }
            using (var input = File.OpenRead(from))
            using (var output = new FileStream(to, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536, FileOptions.WriteThrough))
            {
                input.CopyTo(output);
                output.Flush(true);
            }
            ProtectFile(to);
        }
    }

    private static void Commit(MachineTrust previous, MachineTrust next)
    {
        next.Validate();
        MachineTrust.DemandUnchanged(previous);
        using var key = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)
            .OpenSubKey(Installation.RegistryPath, true) ?? throw new InvalidDataException();
        key.SetValue("State", JsonSerializer.Serialize(next, Installation.Options), RegistryValueKind.String);
        key.Flush();
    }

    internal static void ProtectDirectory(DirectoryInfo directory)
    {
        var security = new DirectorySecurity();
        security.SetSecurityDescriptorSddlForm(DirectorySddl);
        directory.SetAccessControl(security);
    }

    internal static void ProtectFile(string path)
    {
        var security = new FileSecurity();
        security.SetSecurityDescriptorSddlForm(FileSddl);
        new FileInfo(path).SetAccessControl(security);
    }

    private sealed class DeploymentLock : IDisposable
    {
        private readonly Mutex mutex = new(false, @"Global\Shovel.ControlledDeployment.v2");
        internal DeploymentLock()
        {
            try
            {
                if (!mutex.WaitOne(TimeSpan.FromSeconds(10))) { throw new InvalidDataException(); }
            }
            catch
            {
                mutex.Dispose();
                throw;
            }
        }
        public void Dispose()
        {
            mutex.ReleaseMutex();
            mutex.Dispose();
        }
    }
}
