using System.Collections.Immutable;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Shovel.Wam;

internal sealed class Target
{
    public required Guid TenantId { get; init; }
    public required Guid ClientId { get; init; }
    public required string AccountId { get; init; }
    public required string Resource { get; init; }
    public required ImmutableArray<string> Scopes { get; init; }
    public int TimeoutSeconds { get; init; } = 60;

    [JsonIgnore]
    public string Authority => $"https://login.microsoftonline.com/{TenantId:D}";

    internal void Validate()
    {
        if (TenantId == Guid.Empty || ClientId == Guid.Empty
            || string.IsNullOrWhiteSpace(AccountId) || AccountId.Length > 1024
            || AccountId.Any(char.IsControl)
            || Scopes.IsDefault || Scopes.Length is < 1 or > 64
            || Scopes.Any(scope => string.IsNullOrWhiteSpace(scope) || scope.Length > 1024
                || scope.Any(char.IsWhiteSpace) || scope.Any(char.IsControl))
            || Scopes.Distinct(StringComparer.Ordinal).Count() != Scopes.Length
            || TimeoutSeconds is < 1 or > 300
            || !Uri.TryCreate(Resource, UriKind.Absolute, out var resource)
            || resource.Scheme != Uri.UriSchemeHttps || resource.Host.Length == 0
            || resource.UserInfo.Length != 0 || resource.Query.Length != 0
            || resource.Fragment.Length != 0 || Resource.Any(char.IsWhiteSpace)
            || Resource.Any(char.IsControl) || Resource.Contains('\\'))
        {
            throw new InvalidDataException("Invalid approved WAM target.");
        }
    }

    internal static Target Read(string path)
    {
        using var file = File.OpenRead(path);
        if (file.Length is < 1 or > 16384)
        {
            throw new InvalidDataException("Invalid target file size.");
        }
        using var document = JsonDocument.Parse(file);
        if (document.RootElement.ValueKind != JsonValueKind.Object
            || document.RootElement.EnumerateObject().Any(item => item.Name is not
                ("tenantId" or "clientId" or "accountId" or "resource" or "scopes" or "timeoutSeconds"))
            || document.RootElement.EnumerateObject().Select(item => item.Name)
                .GroupBy(name => name, StringComparer.Ordinal).Any(group => group.Count() != 1))
        {
            throw new InvalidDataException("Invalid target object.");
        }
        var target = document.Deserialize<Target>(new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        }) ?? throw new InvalidDataException("Missing approved WAM target.");
        target.Validate();
        return target;
    }

    public override string ToString() => "<WAM target: redacted>";
}
