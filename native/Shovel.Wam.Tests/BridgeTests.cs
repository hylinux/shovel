using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace Shovel.Wam.Tests;

[TestClass]
public sealed class BridgeTests
{
    [TestMethod]
    public void InheritedHandlesCannotLeakToFurtherChildren()
    {
        using var pipe = new AnonymousPipeServerStream(PipeDirection.Out, HandleInheritability.Inheritable);
        var handle = pipe.ClientSafePipeHandle.DangerousGetHandle();
        HostBridge.MakePrivate(handle);
        Assert.IsTrue(GetHandleInformation(handle, out var flags));
        Assert.AreEqual(0U, flags & 1);
    }
    [DllImport("kernel32.dll", ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetHandleInformation(nint handle, out uint flags);

    [TestMethod]
    public void SignedEnvelopeRejectsWrongSignerTamperAndRollback()
    {
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest("CN=Synthetic", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        var purposes = new OidCollection { new("1.3.6.1.5.5.7.3.3") };
        request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(purposes, true));
        using var certificate = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddHours(1));
        var payload = JsonSerializer.SerializeToUtf8Bytes(new
        {
            root = @"C:\synthetic",
            python = "python.exe",
            native = "Shovel.Wam.exe",
            bootstrap = "host_bootstrap.py",
            modulePaths = new[] { "app" },
            files = new Dictionary<string, string> { ["python.exe"] = new string('a', 64) },
            bindings = Array.Empty<object>(),
            revision = 2,
            validUntil = DateTimeOffset.UtcNow.AddMinutes(10).ToUnixTimeSeconds(),
        });
        byte[] Envelope(byte[] signature) => JsonSerializer.SerializeToUtf8Bytes(new
        {
            payload = Convert.ToBase64String(payload),
            signature = Convert.ToBase64String(signature),
            certificate = Convert.ToBase64String(certificate.Export(X509ContentType.Cert)),
        });
        var signature = rsa.SignData(payload, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        var pin = Installation.Hex(certificate.GetCertHash(HashAlgorithmName.SHA256));
        Assert.AreEqual(2, Installation.VerifyEnvelope(Envelope(signature), pin, 2).Revision);
        Assert.ThrowsExactly<InvalidDataException>(() => Installation.VerifyEnvelope(Envelope(signature), new string('b', 64), 2));
        Assert.ThrowsExactly<InvalidDataException>(() => Installation.VerifyEnvelope(Envelope(signature), pin, 3));
        signature[0] ^= 1;
        Assert.ThrowsExactly<InvalidDataException>(() => Installation.VerifyEnvelope(Envelope(signature), pin, 2));
    }

    [TestMethod]
    public void SignedPythonAloneIsNotAnAuthorizedCaller()
    {
        const string python = @"C:\trusted\python.exe", bootstrap = @"C:\trusted\host_bootstrap.py";
        Assert.IsTrue(Installation.CallerMatches(python, [python, "-I", "-S", "-B", bootstrap], python, bootstrap));
        Assert.IsFalse(Installation.CallerMatches(python, [python, "-I", "-S", bootstrap], python, bootstrap));
        Assert.IsFalse(Installation.CallerMatches(python, [python, "-c", "arbitrary"], python, bootstrap));
        Assert.IsFalse(Installation.CallerMatches(python, [python, "-I", "-S", "-B", @"C:\untrusted.py"], python, bootstrap));
    }

    [TestMethod]
    public async Task CredentialUsesBoundBinaryChannelAndCannotSerializeOrReuse()
    {
        using var credential = new Credential("FAKE-WAM-SECRET");
        using var stream = new MemoryStream();
        Assert.ThrowsExactly<NotSupportedException>(() => JsonSerializer.Serialize(credential));
        await credential.Write(stream, new string('a', 64), new string('b', 64), CancellationToken.None);
        Assert.AreEqual(64 + 4 + 15, stream.Length);
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            credential.Write(stream, new string('a', 64), new string('b', 64), CancellationToken.None));
    }

    [TestMethod]
    public void AcceptanceDoesNotTurnMetadataSuccessIntoLiveProof()
    {
        var absent = new AcceptanceReceipt(AcceptanceCase.Silent, new(Status.Success, Stage.Platform), 0);
        Assert.IsFalse(absent.ExpectedOutcomeObserved);
        var observed = new AcceptanceReceipt(AcceptanceCase.Cancel, new(Status.Canceled, Stage.Acquisition, true), 1);
        Assert.IsTrue(observed.ExpectedOutcomeObserved);
        Assert.IsFalse(observed.LiveAcceptanceComplete);
        Assert.IsTrue(observed.RequiresIndependentUiAndOrganizationEvidence);
    }

    [TestMethod]
    public void InvalidFrameAndUserWritableInstallationAreRejected()
    {
        using var stream = new MemoryStream([0, 0, 0, 0]);
        Assert.ThrowsExactly<InvalidDataException>(() => HostBridge.ReadFrame(stream, 128));
        var path = Path.GetTempFileName();
        try { Assert.ThrowsExactly<InvalidDataException>(() => Installation.DemandProtected(path, 1)); }
        finally { File.Delete(path); }
    }
}
