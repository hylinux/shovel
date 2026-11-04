using System.Buffers.Binary;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Shovel.Wam;

[JsonConverter(typeof(CredentialConverter))]
internal sealed class Credential(string value) : IDisposable
{
    private string? value = value;
    internal async Task Write(Stream stream, string nonce, string fingerprint, CancellationToken cancellation)
    {
        var bytes = Encoding.UTF8.GetBytes(value ?? throw new InvalidOperationException());
        try
        {
            if (bytes.Length is < 1 or > 65536) { throw new InvalidDataException(); }
            var header = Convert.FromHexString(nonce + fingerprint);
            var length = new byte[4];
            BinaryPrimitives.WriteInt32BigEndian(length, bytes.Length);
            await stream.WriteAsync(header, cancellation);
            await stream.WriteAsync(length, cancellation);
            await stream.WriteAsync(bytes, cancellation);
            await stream.FlushAsync(cancellation);
        }
        finally { CryptographicOperations.ZeroMemory(bytes); Dispose(); }
    }
    public void Dispose() => value = null;
    public override string ToString() => "<WAM credential: redacted>";
}

internal sealed class CredentialConverter : JsonConverter<Credential>
{
    public override Credential Read(ref Utf8JsonReader reader, Type type, JsonSerializerOptions options) =>
        throw new NotSupportedException("Credentials cannot be serialized.");
    public override void Write(Utf8JsonWriter writer, Credential value, JsonSerializerOptions options) =>
        throw new NotSupportedException("Credentials cannot be serialized.");
}
