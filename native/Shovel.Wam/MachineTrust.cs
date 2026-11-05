using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Win32;

namespace Shovel.Wam;

internal sealed class RuntimeDistribution
{
    public required string Name { get; init; }
    public required string Version { get; init; }
    public required string Metadata { get; init; }
}

internal sealed class RuntimeInventory
{
    public required string PythonVersion { get; init; }
    public required string PythonArchitecture { get; init; }
    public required string PythonDll { get; init; }
    public required string StandardLibrary { get; init; }
    public required string SitePackages { get; init; }
    public required string DotnetVersion { get; init; }
    public required string LockSha256 { get; init; }
    public required RuntimeDistribution[] Distributions { get; init; }
    public string[] DllDirectories { get; init; } = [];

    internal void Validate(Installation installation)
    {
        if (PythonArchitecture != "win-amd64"
            || !Version.TryParse(PythonVersion, out var python) || python.Major != 3 || python.Minor < 14
            || !Version.TryParse(DotnetVersion, out var dotnet) || dotnet.Major != 10
            || !MachineTrust.IsDigest(LockSha256) || Distributions.Length is < 1 or > 10000
            || Distributions.Select(item => item.Metadata).Distinct(StringComparer.OrdinalIgnoreCase).Count() != Distributions.Length
            || Distributions.Select(item => item.Name).Distinct(StringComparer.OrdinalIgnoreCase).Count() != Distributions.Length)
        {
            throw new InvalidDataException();
        }
        var files = installation.Files;
        var pythonDirectory = Path.GetDirectoryName(installation.Python) ?? "";
        var nativeDirectory = Path.GetDirectoryName(installation.Native) ?? "";
        if (StandardLibrary != Path.Combine(pythonDirectory, "Lib")
            || SitePackages != Path.Combine(StandardLibrary, "site-packages")
            || PythonDll != Path.Combine(pythonDirectory, $"python{python.Major}{python.Minor}.dll"))
        {
            throw new InvalidDataException();
        }
        foreach (var file in new[] { PythonDll, Path.Combine(pythonDirectory, "python3.dll"),
            Path.Combine(StandardLibrary, "encodings", "__init__.py"),
            Path.Combine(StandardLibrary, "json", "__init__.py"),
            Path.Combine(nativeDirectory, "coreclr.dll"), Path.Combine(nativeDirectory, "hostpolicy.dll"),
            Path.Combine(nativeDirectory, "System.Private.CoreLib.dll"),
            Path.Combine(nativeDirectory, "PresentationFramework.dll"),
            Path.Combine(nativeDirectory, Path.GetFileNameWithoutExtension(installation.Native) + ".runtimeconfig.json") })
        {
            Installation.ValidateRelative(file);
            if (!files.ContainsKey(file)) { throw new InvalidDataException(); }
        }
        var modules = new List<string> { StandardLibrary, SitePackages, Path.Combine(pythonDirectory, "DLLs") };
        var dlls = Array.Empty<string>();
        if (Distributions.Any(item => item.Name == "pywin32"))
        {
            modules.AddRange([Path.Combine(SitePackages, "win32"), Path.Combine(SitePackages, "win32", "lib"),
                Path.Combine(SitePackages, "Pythonwin")]);
            dlls = [Path.Combine(SitePackages, "pywin32_system32")];
        }
        if (!installation.ModulePaths.SequenceEqual(modules) || !DllDirectories.SequenceEqual(dlls)
            || !Distributions.Any(item => item.Name == "shovel"))
        {
            throw new InvalidDataException();
        }
        foreach (var item in Distributions)
        {
            Installation.ValidateRelative(item.Metadata);
            if (string.IsNullOrWhiteSpace(item.Name) || string.IsNullOrWhiteSpace(item.Version)
                || !item.Metadata.StartsWith(SitePackages + "\\", StringComparison.Ordinal)
                || !item.Metadata.EndsWith(".dist-info\\METADATA", StringComparison.Ordinal)
                || !files.ContainsKey(item.Metadata))
            {
                throw new InvalidDataException();
            }
        }
        var metadata = files.Keys.Where(path => path.StartsWith(SitePackages + "\\", StringComparison.Ordinal)
            && path.EndsWith(".dist-info\\METADATA", StringComparison.Ordinal)
            && path[(SitePackages.Length + 1)..].Split('\\').Length == 2).ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (!metadata.SetEquals(Distributions.Select(item => item.Metadata))) { throw new InvalidDataException(); }
    }
}

internal sealed class ActiveInstallation
{
    public required string EnvelopeSha256 { get; init; }
    public required string SignerSha256 { get; init; }
    public required int Revision { get; init; }
}

internal sealed class MachineTrust
{
    public required int Schema { get; init; }
    public required string StoreRoot { get; init; }
    public required int Generation { get; init; }
    public required int HighestRevision { get; init; }
    public required string[] Signers { get; init; }
    public required string[] RevokedSigners { get; init; }
    public required string[] RevokedEnvelopes { get; init; }
    public ActiveInstallation? Active { get; init; }

    internal static bool IsDigest(string value) =>
        value.Length == 64 && value.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');

    internal static MachineTrust Parse(string json)
    {
        if (json.Length > 1024 * 1024) { throw new InvalidDataException(); }
        using var document = JsonDocument.Parse(json);
        Installation.RejectDuplicates(document.RootElement);
        var state = document.Deserialize<MachineTrust>(Installation.Options) ?? throw new InvalidDataException();
        state.Validate();
        return state;
    }

    internal void Validate()
    {
        if (Schema != 2 || Generation < 1 || HighestRevision < 0
            || !Path.IsPathFullyQualified(StoreRoot) || StoreRoot.StartsWith(@"\\", StringComparison.Ordinal)
            || StoreRoot != Path.TrimEndingDirectorySeparator(Path.GetFullPath(StoreRoot))
            || Signers.Length is < 1 or > 100 || RevokedSigners.Length > 100 || RevokedEnvelopes.Length > 10000)
        {
            throw new InvalidDataException();
        }
        foreach (var group in new[] { Signers, RevokedSigners, RevokedEnvelopes })
        {
            if (group.Any(value => !IsDigest(value)) || group.Distinct().Count() != group.Length)
            {
                throw new InvalidDataException();
            }
        }
        if (Active is not null && (Active.Revision != HighestRevision || !IsDigest(Active.EnvelopeSha256)
            || !Signers.Contains(Active.SignerSha256) || RevokedSigners.Contains(Active.SignerSha256)
            || RevokedEnvelopes.Contains(Active.EnvelopeSha256)))
        {
            throw new InvalidDataException();
        }
    }

    internal static MachineTrust Read()
    {
        Installation.DemandProtected(@"MACHINE\" + Installation.RegistryPath, 4);
        using var key = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)
            .OpenSubKey(Installation.RegistryPath) ?? throw new InvalidDataException();
        return Parse(key.GetValue("State", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string
            ?? throw new InvalidDataException());
    }

    internal static void DemandUnchanged(MachineTrust state)
    {
        if (JsonSerializer.Serialize(Read(), Installation.Options) != JsonSerializer.Serialize(state, Installation.Options))
        {
            throw new InvalidDataException();
        }

    }

    internal static FileStream AcquireReadLease()
    {
        var state = Read();
        var path = Path.Combine(state.StoreRoot, "deployment.lock");
        Installation.DemandAncestorsProtected(path);
        Installation.DemandProtected(path, 1);
        var lease = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        try { DemandUnchanged(state); return lease; }
        catch { lease.Dispose(); throw; }
    }
    internal string PackagePath(int revision) => Path.Combine(StoreRoot, "packages", revision.ToString(System.Globalization.CultureInfo.InvariantCulture));
    internal string EnvelopePath(string digest) => Path.Combine(StoreRoot, "policies", digest + ".json");

    internal Installation LoadActive()
    {
        Validate();
        var active = Active ?? throw new InvalidDataException();
        var path = EnvelopePath(active.EnvelopeSha256);
        Installation.DemandAncestorsProtected(path);
        Installation.DemandProtected(path, 1);
        var bytes = ReadEnvelope(path);
        if (Installation.Hex(SHA256.HashData(bytes)) != active.EnvelopeSha256) { throw new InvalidDataException(); }
        var installation = Installation.VerifyEnvelope(bytes, active.SignerSha256, active.Revision);
        if (!string.Equals(installation.Root, PackagePath(active.Revision), StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException();
        }
        installation.VerifyRuntime();
        installation.BindDigest(active.EnvelopeSha256);
        return installation;
    }

    internal static byte[] ReadEnvelope(string path)
    {
        using var input = File.OpenRead(path);
        if (input.Length is < 1 or > 8 * 1024 * 1024) { throw new InvalidDataException(); }
        using var reader = new BinaryReader(input);
        var bytes = reader.ReadBytes(8 * 1024 * 1024 + 1);
        if (bytes.Length > 8 * 1024 * 1024) { throw new InvalidDataException(); }
        return bytes;
    }

    internal MachineTrust Advance(int expectedGeneration, ActiveInstallation? active,
        string? revokeEnvelope = null, string? revokeSigner = null, string? addSigner = null)
    {
        Validate();
        if (expectedGeneration != Generation || Generation == int.MaxValue) { throw new InvalidDataException(); }
        var signers = addSigner is null ? Signers : Signers.Append(addSigner).Distinct().ToArray();
        var revokedSigners = revokeSigner is null ? RevokedSigners : RevokedSigners.Append(revokeSigner).Distinct().ToArray();
        var revokedEnvelopes = revokeEnvelope is null ? RevokedEnvelopes : RevokedEnvelopes.Append(revokeEnvelope).Distinct().ToArray();
        if (active is not null && (active.Revision <= HighestRevision || !signers.Contains(active.SignerSha256)
            || revokedSigners.Contains(active.SignerSha256) || revokedEnvelopes.Contains(active.EnvelopeSha256)))
        {
            throw new InvalidDataException();
        }
        var preserve = active is null && revokeEnvelope is null && revokeSigner is null ? Active : null;
        if (active is null && Active is not null && revokeEnvelope != Active.EnvelopeSha256
            && revokeSigner != Active.SignerSha256) { preserve = Active; }
        var result = new MachineTrust
        {
            Schema = Schema,
            StoreRoot = StoreRoot,
            Generation = Generation + 1,
            HighestRevision = active?.Revision ?? HighestRevision,
            Signers = signers,
            RevokedSigners = revokedSigners,
            RevokedEnvelopes = revokedEnvelopes,
            Active = active ?? preserve,
        };
        result.Validate();
        return result;
    }
}
