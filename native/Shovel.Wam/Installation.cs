using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace Shovel.Wam;

internal sealed class InstalledBinding
{
    public required JsonElement Prototype { get; init; }
    public required Target Target { get; init; }
}

internal sealed class Installation
{
    internal const string RegistryPath = @"SOFTWARE\Shovel\WamTrust";
    internal static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        UnmappedMemberHandling = System.Text.Json.Serialization.JsonUnmappedMemberHandling.Disallow,
        MaxDepth = 32,
    };
    public required string Root { get; init; }
    public required string Python { get; init; }
    public required string Native { get; init; }
    public required string Bootstrap { get; init; }
    public required string[] ModulePaths { get; init; }
    public required Dictionary<string, string> Files { get; init; }
    public required InstalledBinding[] Bindings { get; init; }
    public required int Revision { get; init; }
    public required long ValidUntil { get; init; }
    public RuntimeInventory? Runtime { get; init; }
    internal string PolicyDigest { get; private set; } = "";
    internal int AnchorGeneration { get; private set; }

    internal static Installation Load()
    {
        var state = MachineTrust.Read();
        var result = state.LoadActive();
        result.AnchorGeneration = state.Generation;
        result.VerifyFiles();
        MachineTrust.DemandUnchanged(state);
        if (!string.Equals(result.FilePath(result.Native), Environment.ProcessPath, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException();
        }
        result.VerifyCaller();
        return result;
    }

    internal static Installation VerifyEnvelope(byte[] bytes, string signer, int revision)
    {
        using var document = JsonDocument.Parse(bytes);
        var root = document.RootElement;
        RejectDuplicates(root);
        if (root.EnumerateObject().Count() != 3)
        {
            throw new InvalidDataException();
        }
        var payload = Convert.FromBase64String(root.GetProperty("payload").GetString() ?? "");
        var signature = Convert.FromBase64String(root.GetProperty("signature").GetString() ?? "");
        using var certificate = X509CertificateLoader.LoadCertificate(
            Convert.FromBase64String(root.GetProperty("certificate").GetString() ?? ""));
        using var rsa = certificate.GetRSAPublicKey() ?? throw new InvalidDataException();
        var now = DateTime.UtcNow;
        if (Hex(certificate.GetCertHash(HashAlgorithmName.SHA256)) != signer
            || certificate.NotBefore.ToUniversalTime() > now || certificate.NotAfter.ToUniversalTime() <= now
            || !certificate.Extensions.OfType<X509EnhancedKeyUsageExtension>().Any(extension =>
                extension.EnhancedKeyUsages.Cast<Oid>().Any(oid => oid.Value == "1.3.6.1.5.5.7.3.3"))
            || rsa.KeySize < 2048 || !rsa.VerifyData(payload, signature, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1))
        {
            throw new InvalidDataException();
        }
        using var content = JsonDocument.Parse(payload);
        RejectDuplicates(content.RootElement);
        var installation = content.Deserialize<Installation>(Options) ?? throw new InvalidDataException();
        if (installation.Revision < 1 || installation.Revision != revision
            || installation.ValidUntil <= DateTimeOffset.UtcNow.ToUnixTimeSeconds()
            || installation.ValidUntil > new DateTimeOffset(certificate.NotAfter.ToUniversalTime()).ToUnixTimeSeconds()
            || installation.Files.Count is < 1 or > 100000)
        {
            throw new InvalidDataException();
        }
        foreach (var binding in installation.Bindings)
        {
            binding.Target.Validate();
        }
        return installation;
    }

    internal string FilePath(string relative, string? root = null)
    {
        ValidateRelative(relative);
        var path = Path.GetFullPath(Path.Combine(root ?? Root, relative));
        var current = Path.GetFullPath(root ?? Root);
        if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException();
        }
        foreach (var part in relative.Split('\\'))
        {
            current = Path.Combine(current, part);
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException();
            }
        }
        return path;
    }

    internal static void ValidateRelative(string relative)
    {
        if (string.IsNullOrEmpty(relative) || Path.IsPathRooted(relative) || relative.Contains('/')
            || relative.Contains(':') || relative.Split('\\').Any(part =>
                part is "" or "." or ".." || part != part.TrimEnd(' ', '.')
                || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || new[] { "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
                    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
                    "LPT6", "LPT7", "LPT8", "LPT9" }.Contains(part.Split('.')[0], StringComparer.OrdinalIgnoreCase)))
        {
            throw new InvalidDataException();
        }
    }

    internal void VerifyFiles(string? stagingRoot = null, bool requireProtected = true)
    {
        var root = stagingRoot ?? Root;
        if (!Path.IsPathFullyQualified(root)) { throw new InvalidDataException(); }
        if (requireProtected) { DemandAncestorsProtected(root); }
        var actual = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var pending = new Stack<string>();
        pending.Push(root);
        while (pending.TryPop(out var directory))
        {
            if (requireProtected) { DemandProtected(directory, 1); }
            if ((File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException();
            }
            foreach (var entry in Directory.EnumerateFileSystemEntries(directory))
            {
                var attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0) { throw new InvalidDataException(); }
                if ((attributes & FileAttributes.Directory) != 0) { pending.Push(entry); }
                else { actual.Add(Path.GetRelativePath(root, entry)); }
                if (actual.Count > 100000) { throw new InvalidDataException(); }
            }
        }
        if (!actual.SetEquals(Files.Keys) || Files.Keys.Distinct(StringComparer.OrdinalIgnoreCase).Count() != Files.Count)
        {
            throw new InvalidDataException();
        }
        foreach (var pair in Files)
        {
            var path = FilePath(pair.Key, root);
            if (requireProtected) { DemandProtected(path, 1); }
            using var stream = File.OpenRead(path);
            if (Hex(SHA256.HashData(stream)) != pair.Value)
            {
                throw new InvalidDataException();
            }
        }
        if (!Files.ContainsKey(Python) || !Files.ContainsKey(Native) || !Files.ContainsKey(Bootstrap))
        {
            throw new InvalidDataException();
        }
        foreach (var path in ModulePaths.Concat(Runtime?.DllDirectories ?? []))
        {
            if (!Directory.Exists(FilePath(path, root)))
            {
                throw new InvalidDataException();
            }
        }

    }

    internal void VerifyRuntime()
    {
        if (Runtime is null) { throw new InvalidDataException(); }
        Runtime.Validate(this);
    }

    internal void BindDigest(string digest) => PolicyDigest = digest;

    internal static bool CallerMatches(string image, string[] arguments, string python, string bootstrap) =>
        string.Equals(image, python, StringComparison.OrdinalIgnoreCase)
        && arguments.Length == 5 && string.Equals(arguments[0], python, StringComparison.OrdinalIgnoreCase)
        && arguments[1] == "-I" && arguments[2] == "-S" && arguments[3] == "-B"
        && string.Equals(arguments[4], bootstrap, StringComparison.OrdinalIgnoreCase);

    private void VerifyCaller()
    {
        using var search = new ManagementObjectSearcher(
            $"SELECT ParentProcessId FROM Win32_Process WHERE ProcessId={Environment.ProcessId}");
        using var own = search.Get();
        var parent = own.Cast<ManagementObject>().Single()["ParentProcessId"];
        using var lookup = new ManagementObjectSearcher(
            $"SELECT ExecutablePath,CommandLine FROM Win32_Process WHERE ProcessId={Convert.ToUInt32(parent)}");
        using var rows = lookup.Get();
        using var process = rows.Cast<ManagementObject>().Single();
        var command = (string)process["CommandLine"];
        var pointer = CommandLineToArgvW(command, out var count);
        if (pointer == 0)
        {
            throw new InvalidDataException();
        }
        try
        {
            var args = Enumerable.Range(0, count)
                .Select(index => Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, index * IntPtr.Size)) ?? "").ToArray();
            if (!CallerMatches((string)process["ExecutablePath"], args, FilePath(Python), FilePath(Bootstrap)))
            {
                throw new InvalidDataException();
            }
        }
        finally { LocalFree(pointer); }
    }

    internal Target Resolve(JsonElement request)
    {
        RejectDuplicates(request);
        var candidate = JsonNode.Parse(request.GetRawText())?.AsObject() ?? throw new InvalidDataException();
        foreach (var field in new[] { "request_id", "execution_id", "correlation_id" })
        {
            candidate.Remove(field);
        }
        var matches = Bindings.Where(binding =>
        {
            var prototype = JsonNode.Parse(binding.Prototype.GetRawText())!.AsObject();
            foreach (var field in new[] { "request_id", "execution_id", "correlation_id" })
            {
                prototype.Remove(field);
            }
            return JsonNode.DeepEquals(candidate, prototype);
        }).ToArray();
        if (matches.Length != 1 || request.GetProperty("interaction_mode").GetString() != "silent"
            || request.GetProperty("force_refresh").GetBoolean()
            || request.GetProperty("claims_challenge_ref").ValueKind != JsonValueKind.Null)
        {
            throw new InvalidDataException();
        }
        var target = matches[0].Target;
        var identity = request.GetProperty("identity");
        if (identity.GetProperty("tenant_id").GetString() != target.TenantId.ToString("D")
            || request.GetProperty("authority").GetString() != target.Authority
            || request.GetProperty("audience").GetString() != target.Resource
            || !request.GetProperty("allowed_scopes").EnumerateArray().Select(item => item.GetString())
                .SequenceEqual(target.Scopes))
        {
            throw new InvalidDataException();
        }
        return target;
    }

    internal static void RejectDuplicates(JsonElement item)
    {
        if (item.ValueKind == JsonValueKind.Object)
        {
            if (item.EnumerateObject().Select(property => property.Name).Distinct(StringComparer.Ordinal).Count()
                != item.EnumerateObject().Count())
            {
                throw new InvalidDataException();
            }
            foreach (var property in item.EnumerateObject()) { RejectDuplicates(property.Value); }
        }
        else if (item.ValueKind == JsonValueKind.Array)
        {
            foreach (var value in item.EnumerateArray()) { RejectDuplicates(value); }
        }
    }

    internal static string Hex(byte[] bytes) => Convert.ToHexStringLower(bytes);
    private static readonly HashSet<string> Writers =
        ["S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"];

    internal static void DemandProtected(string path, uint kind)
        => DemandSecurity(path, kind, kind == 4 ? 0x500d0026 : 0x500d0156);

    internal static void DemandAncestorsProtected(string path)
    {
        for (var directory = Directory.GetParent(path); directory is not null; directory = directory.Parent)
        {
            if ((directory.Attributes & FileAttributes.ReparsePoint) != 0) { throw new InvalidDataException(); }
            DemandSecurity(directory.FullName, 1, 0x500d0040);
        }
    }

    private static void DemandSecurity(string path, uint kind, int mask)
    {
        var code = GetNamedSecurityInfoW(path, kind, 5, out var owner, out _, out _, out _, out var descriptor);
        if (code != 0) { throw new InvalidDataException(); }
        try
        {
            var bytes = new byte[GetSecurityDescriptorLength(descriptor)];
            Marshal.Copy(descriptor, bytes, 0, bytes.Length);
            if (owner == 0) { throw new InvalidDataException(); }
            DemandDescriptor(new RawSecurityDescriptor(bytes, 0), mask);
        }
        finally { LocalFree(descriptor); }
    }

    internal static void DemandDescriptor(RawSecurityDescriptor security, int mask)
    {
        if (security.Owner is null || !Writers.Contains(security.Owner.Value) || security.DiscretionaryAcl is null)
        {
            throw new InvalidDataException();
        }
        foreach (var ace in security.DiscretionaryAcl)
        {
            if ((ace.AceFlags & AceFlags.InheritOnly) != 0) { continue; }
            if (ace is not QualifiedAce entry) { throw new InvalidDataException(); }
            if (entry.AceQualifier == AceQualifier.AccessAllowed && (entry.AccessMask & mask) != 0
                && !Writers.Contains(entry.SecurityIdentifier.Value))
            {
                throw new InvalidDataException();
            }
        }
    }
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern uint GetNamedSecurityInfoW(string path, uint kind, uint flags, out nint owner,
        out nint group, out nint dacl, out nint sacl, out nint descriptor);
    [DllImport("advapi32.dll", ExactSpelling = true)]
    private static extern uint GetSecurityDescriptorLength(nint descriptor);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern nint CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll", ExactSpelling = true)]
    private static extern nint LocalFree(nint memory);
}
