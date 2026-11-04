using System.Buffers.Binary;
using System.IO;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using Microsoft.Win32.SafeHandles;

namespace Shovel.Wam;

internal static class HostBridge
{
    internal static int Run(string inputHandle, string secretHandle)
    {
        using var input = new FileStream(new SafeFileHandle(nint.Parse(inputHandle), true), FileAccess.Read);
        using var secret = new FileStream(new SafeFileHandle(nint.Parse(secretHandle), true), FileAccess.Write);
        MakePrivate(input.SafeFileHandle.DangerousGetHandle());
        MakePrivate(secret.SafeFileHandle.DangerousGetHandle());
        var policy = Installation.Load();
        using var cancellation = new CancellationTokenSource();
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var report = new Report(Status.NativeError, Stage.Platform);
        string nonce = "", fingerprint = "";
        double expires = 0;
        app.Startup += async (_, _) =>
        {
            try
            {
                var bytes = await Task.Run(() => ReadFrame(input, 32768));
                using var document = JsonDocument.Parse(bytes);
                Installation.RejectDuplicates(document.RootElement);
                var root = document.RootElement;
                if (root.EnumerateObject().Count() != 4 || root.GetProperty("version").GetInt32() != 1)
                {
                    throw new InvalidDataException();
                }
                nonce = root.GetProperty("nonce").GetString() ?? "";
                if (nonce.Length != 64 || Convert.FromHexString(nonce).Length != 32)
                {
                    throw new InvalidDataException();
                }
                expires = root.GetProperty("deadline").GetDouble();
                var remaining = expires - Now();
                if (!double.IsFinite(remaining) || remaining <= 0 || remaining > 300 || expires > policy.ValidUntil)
                {
                    throw new InvalidDataException();
                }
                var requestBytes = Convert.FromBase64String(root.GetProperty("request").GetString() ?? "");
                if (requestBytes.Length > 16384) { throw new InvalidDataException(); }
                fingerprint = Installation.Hex(SHA256.HashData(requestBytes));
                using var request = JsonDocument.Parse(requestBytes);
                var target = policy.Resolve(request.RootElement);
                cancellation.CancelAfter(TimeSpan.FromSeconds(remaining));
                _ = Task.Run(() =>
                {
                    try
                    {
                        using var cancel = JsonDocument.Parse(ReadFrame(input, 128));
                        if (cancel.RootElement.GetProperty("cancel").GetString() != nonce)
                        {
                            throw new InvalidDataException();
                        }
                    }
                    catch (Exception)
                    {
                        Console.Error.WriteLine("{\"diagnostic\":\"control_channel_closed_or_invalid\"}");
                    }
                    finally
                    {
                        try { cancellation.Cancel(); }
                        catch (ObjectDisposedException)
                        {
                            Console.Error.WriteLine("{\"diagnostic\":\"control_channel_closed_or_invalid\"}");
                        }
                    }
                });
                var client = new WamClient(new WindowsWam(), diagnostic =>
                    Console.Error.WriteLine(JsonSerializer.Serialize(new { diagnostic }, Program.Json)),
                    TimeSpan.FromSeconds(2));
                var bounded = new Target
                {
                    TenantId = target.TenantId,
                    ClientId = target.ClientId,
                    AccountId = target.AccountId,
                    Resource = target.Resource,
                    Scopes = target.Scopes,
                    TimeoutSeconds = Math.Min(target.TimeoutSeconds, (int)Math.Ceiling(remaining)),
                };
                report = await client.Authenticate(bounded, Mode.Silent, 0, cancellation.Token, async credential =>
                {
                    var current = Installation.Load();
                    if (current.PolicyDigest != policy.PolicyDigest || current.Revision != policy.Revision
                        || Now() >= expires || cancellation.IsCancellationRequested)
                    {
                        throw new InvalidDataException();
                    }
                    current.Resolve(request.RootElement);
                    await credential.Write(secret, nonce, fingerprint, cancellation.Token);
                });
            }
            catch (Exception)
            {
                report = new(Status.NativeError, Stage.Acquisition);
            }
            finally { app.Shutdown(); }
        };
        app.Run();
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            version = 1,
            nonce,
            fingerprint,
            expires_at = expires,
            status = report.Status,
            native_operation_canceled = report.NativeOperationCanceled,
            scope = "wam_host_bridge",
            production_ready = false,
            live_acceptance_complete = false,
        }, Program.Json));
        return report.Status == Status.Success ? 0 : 3;
    }

    private static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
    internal static void MakePrivate(nint handle)
    {
        if (!SetHandleInformation(handle, 1, 0)) { throw new InvalidDataException(); }
    }
    [DllImport("kernel32.dll", ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetHandleInformation(nint handle, uint mask, uint flags);
    internal static byte[] ReadFrame(Stream stream, int maximum)
    {
        Span<byte> header = stackalloc byte[4];
        stream.ReadExactly(header);
        var size = BinaryPrimitives.ReadInt32BigEndian(header);
        if (size < 1 || size > maximum) { throw new InvalidDataException(); }
        var result = new byte[size];
        stream.ReadExactly(result);
        return result;
    }
}
