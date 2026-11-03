using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;
using Windows.Foundation;
using Windows.Security.Credentials;

[assembly: DoNotParallelize]

namespace Shovel.Wam.Tests;

[TestClass]
public sealed class WamTests
{
    private const string Sentinel = "SECRET-SENTINEL-NOT-A-REAL-TOKEN";

    private static Target Approved(int timeoutSeconds = 60) => new()
    {
        TenantId = Guid.Parse("11111111-1111-1111-1111-111111111111"),
        ClientId = Guid.Parse("22222222-2222-2222-2222-222222222222"),
        AccountId = "synthetic-account",
        Resource = "https://resource.example.invalid",
        Scopes = ["api://synthetic/read"],
        TimeoutSeconds = timeoutSeconds,
    };

    [TestMethod]
    public void RealRequestBuilderBindsTenantClientResourceAndScopesWithoutAcquiring()
    {
        var target = Approved();
        var provider = new WebAccountProvider(WamClient.ProviderId, "Synthetic provider",
            new Uri("https://login.microsoftonline.com/organizations"));
        var request = WindowsWam.CreateRequest(provider, target);
        Assert.AreEqual(target.ClientId.ToString("D"), request.ClientId);
        Assert.AreEqual(target.Authority, request.Properties["authority"]);
        Assert.AreEqual(target.Resource, request.Properties["resource"]);
        Assert.AreEqual("api://synthetic/read", request.Scope);
        Assert.AreEqual($"ms-appx-web://Microsoft.AAD.BrokerPlugin/{target.ClientId:D}",
            request.Properties["redirect_uri"]);
        Assert.IsTrue(Guid.TryParse(request.CorrelationId, out _));
        Assert.IsFalse(request.Properties.ContainsKey("validateAuthority"));
    }

    [TestMethod]
    public async Task MissingProviderNeverDispatchesAuthentication()
    {
        var platform = new FixturePlatform { Provider = null };
        var result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.Unavailable, result.Status);
        Assert.AreEqual(0, platform.Acquisitions);
    }

    [TestMethod]
    public async Task ChangedAccountFailsBeforeTokenRequest()
    {
        var platform = new FixturePlatform { AccountId = "other-account" };
        var result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.BindingRejected, result.Status);
        Assert.AreEqual(0, platform.Acquisitions);
    }

    [TestMethod]
    public async Task InteractiveRequiresOwnedWindowAndRechecksItBeforeDispatch()
    {
        var platform = new FixturePlatform { OwnedWindow = false };
        var result = await Client(platform).Authenticate(Approved(), Mode.Interactive, 123, CancellationToken.None);
        Assert.AreEqual(Status.InvalidWindow, result.Status);
        Assert.AreEqual(0, platform.ProviderCalls);
        platform = new FixturePlatform { LoseWindowAfterFirstCheck = true };
        result = await Client(platform).Authenticate(Approved(), Mode.Interactive, 123, CancellationToken.None);
        Assert.AreEqual(Status.InvalidWindow, result.Status);
        Assert.AreEqual(0, platform.Acquisitions);
    }

    [TestMethod]
    public async Task SilentInteractionRequirementDoesNotStartInteractiveFallback()
    {
        var platform = new FixturePlatform { Response = new(Status.InteractionRequired, false, null, null) };
        var result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.InteractionRequired, result.Status);
        Assert.AreEqual(1, platform.Acquisitions);
        Assert.AreEqual(Mode.Silent, platform.LastMode);
    }

    [TestMethod]
    public async Task SuccessRequiresMatchingResponseAndNeverProjectsAccountOrSecrets()
    {
        var platform = new FixturePlatform();
        var result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.Success, result.Status);
        Assert.IsFalse(result.ProductionReady);
        var json = JsonSerializer.Serialize(result, Program.Json);
        Assert.IsFalse(json.Contains("synthetic-account", StringComparison.Ordinal));
        Assert.IsFalse(json.Contains(Sentinel, StringComparison.Ordinal));
        platform.Response = new(Status.Success, true, Sentinel, WamClient.ProviderId);
        result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.BindingRejected, result.Status);
        Assert.IsFalse(JsonSerializer.Serialize(result, Program.Json).Contains(Sentinel, StringComparison.Ordinal));
        Assert.IsFalse(platform.Response.ToString().Contains(Sentinel, StringComparison.Ordinal));
    }

    [TestMethod]
    public async Task NativeFailureDoesNotLeakExceptionOrFallBack()
    {
        var platform = new FixturePlatform { ThrowOnAcquire = true };
        var result = await Client(platform).Authenticate(Approved(), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.NativeError, result.Status);
        Assert.AreEqual(1, platform.Acquisitions);
        Assert.IsFalse(JsonSerializer.Serialize(result, Program.Json).Contains(Sentinel, StringComparison.Ordinal));
    }

    [TestMethod]
    public async Task CancellationCallsNativeCancelAndDoesNotReturnResult()
    {
        using var cancellation = new CancellationTokenSource();
        var platform = new FixturePlatform { Pending = new FixtureOperation<Reply>() };
        var running = Client(platform).Authenticate(Approved(), Mode.Silent, 0, cancellation.Token);
        await platform.Started.Task.WaitAsync(TimeSpan.FromSeconds(2));
        cancellation.Cancel();
        var result = await running;
        Assert.AreEqual(Status.Canceled, result.Status);
        Assert.IsTrue(result.NativeOperationCanceled);
        Assert.AreEqual(1, platform.Pending.CancelCalls);
        Assert.IsTrue(platform.Pending.Closed.Task.IsCompleted);
    }

    [TestMethod]
    public async Task TimeoutCancelsNativeOperationWithoutExtendingBudget()
    {
        var operation = new FixtureOperation<Reply>();
        var result = await new Operations(_ => { }, TimeSpan.FromMilliseconds(50)).Run(
            () => operation, TimeSpan.FromMilliseconds(5), CancellationToken.None);
        Assert.AreEqual(OperationStatus.TimedOut, result.Status);
        Assert.IsTrue(result.NativeOperationCanceled);
        Assert.IsNull(result.Value);
        Assert.AreEqual(1, operation.CancelCalls);
    }

    [TestMethod]
    public async Task DiscoveryAndAcquisitionShareOriginalDeadline()
    {
        var platform = new FixturePlatform { BeforeAccount = () => Thread.Sleep(1100) };
        var result = await Client(platform).Authenticate(Approved(1), Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.TimedOut, result.Status);
        Assert.AreEqual(Stage.Acquisition, result.Stage);
        Assert.AreEqual(0, platform.Acquisitions);
    }

    [TestMethod]
    public async Task RealAsyncAdapterDoesNotDecodeLateResultAfterNativeCancel()
    {
        var native = new FixtureNativeOperation();
        var decoded = 0;
        var operation = new WindowsOperation<string, string>(native, value =>
        {
            decoded++;
            return value;
        });
        operation.Cancel();
        native.Complete();
        await Assert.ThrowsAsync<OperationCanceledException>(() => operation.Completion);
        Assert.AreEqual(1, native.CancelCalls);
        Assert.AreEqual(0, decoded);
        Assert.AreEqual(NativeState.Completed, operation.State);
        operation.Close();
        Assert.IsTrue(native.Closed);
    }

    [TestMethod]
    public async Task RealWindowOwnershipRejectsForeignThreadAndClosedHandle()
    {
        var finished = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var thread = new Thread(() =>
        {
            try
            {
                var window = new System.Windows.Window();
                var handle = new System.Windows.Interop.WindowInteropHelper(window).EnsureHandle();
                var platform = new WindowsWam();
                Assert.IsTrue(platform.OwnsWindow(handle));
                Assert.IsFalse(Task.Run(() => platform.OwnsWindow(handle)).GetAwaiter().GetResult());
                window.Close();
                Assert.IsFalse(platform.OwnsWindow(handle));
                finished.SetResult();
            }
            catch (Exception exception)
            {
                finished.SetException(exception);
            }
        })
        { IsBackground = true };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        await finished.Task.WaitAsync(TimeSpan.FromSeconds(5));
    }

    [TestMethod]
    public async Task IgnoredCancellationIsUnconfirmedAndLateResultIsDrained()
    {
        using var cancellation = new CancellationTokenSource();
        var operation = new FixtureOperation<Reply> { HonorCancel = false };
        var running = new Operations(_ => { }, TimeSpan.FromMilliseconds(20)).Run(
            () => operation, TimeSpan.FromSeconds(5), cancellation.Token);
        cancellation.Cancel();
        var result = await running;
        Assert.AreEqual(OperationStatus.CancellationUnconfirmed, result.Status);
        Assert.IsFalse(result.NativeOperationCanceled);
        Assert.IsNull(result.Value);
        Assert.IsFalse(operation.Closed.Task.IsCompleted);
        operation.Complete(new(Status.Success, true, Sentinel, WamClient.ProviderId));
        await operation.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
        Assert.IsNull(result.Value);
    }

    [TestMethod]
    public async Task LateNativeExceptionProducesOnlySafeDiagnosticAndCloses()
    {
        var diagnostics = new List<Diagnostic>();
        using var cancellation = new CancellationTokenSource();
        var operation = new FixtureOperation<Reply> { HonorCancel = false };
        var running = new Operations(diagnostics.Add, TimeSpan.FromMilliseconds(20)).Run(
            () => operation, TimeSpan.FromSeconds(5), cancellation.Token);
        cancellation.Cancel();
        await running;
        operation.Fail(new InvalidOperationException(Sentinel));
        await operation.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
        CollectionAssert.Contains(diagnostics, Diagnostic.LateNativeFailure);
        Assert.IsFalse(JsonSerializer.Serialize(diagnostics, Program.Json).Contains(Sentinel, StringComparison.Ordinal));
    }

    [TestMethod]
    public async Task CloseFailureCannotReturnSuccess()
    {
        var diagnostics = new List<Diagnostic>();
        var operation = new FixtureOperation<Reply> { ThrowOnClose = true };
        operation.Complete(new(Status.Success, true, "synthetic-account", WamClient.ProviderId));
        var result = await new Operations(diagnostics.Add, TimeSpan.FromMilliseconds(20)).Run(
            () => operation, TimeSpan.FromSeconds(5), CancellationToken.None);
        Assert.AreEqual(OperationStatus.NativeError, result.Status);
        Assert.IsNull(result.Value);
        CollectionAssert.Contains(diagnostics, Diagnostic.NativeCloseFailed);
    }

    [TestMethod]
    public async Task PreCanceledOrExpiredBudgetDoesNotCreateNativeOperation()
    {
        var starts = 0;
        INativeOperation<Reply> Start()
        {
            starts++;
            return new FixtureOperation<Reply>();
        }
        var operations = new Operations(_ => { }, TimeSpan.FromMilliseconds(20));
        var result = await operations.Run(Start, TimeSpan.Zero, CancellationToken.None);
        Assert.AreEqual(OperationStatus.TimedOut, result.Status);
        result = await operations.Run(Start, TimeSpan.FromSeconds(5), new CancellationToken(true));
        Assert.AreEqual(OperationStatus.Canceled, result.Status);
        Assert.AreEqual(0, starts);
    }

    [TestMethod]
    public async Task InvalidResourceAndDuplicateScopesFailBeforeProviderDiscovery()
    {
        var platform = new FixturePlatform();
        var target = Approved();
        var invalid = new Target
        {
            TenantId = target.TenantId,
            ClientId = target.ClientId,
            AccountId = target.AccountId,
            Resource = "http://resource.example.invalid",
            Scopes = target.Scopes,
        };
        var result = await Client(platform).Authenticate(invalid, Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.InvalidTarget, result.Status);
        invalid = new Target
        {
            TenantId = target.TenantId,
            ClientId = target.ClientId,
            AccountId = target.AccountId,
            Resource = target.Resource,
            Scopes = ["api://synthetic/read", "api://synthetic/read"],
        };
        result = await Client(platform).Authenticate(invalid, Mode.Silent, 0, CancellationToken.None);
        Assert.AreEqual(Status.InvalidTarget, result.Status);
        Assert.AreEqual(0, platform.ProviderCalls);
    }

    [TestMethod]
    public void TargetParserRejectsDuplicateFieldsCredentialsAndArbitraryAuthority()
    {
        var valid = JsonSerializer.Serialize(Approved(), new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        });
        var path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, valid);
            Assert.AreEqual(Approved().TenantId, Target.Read(path).TenantId);
            File.WriteAllText(path, valid[..^1] + ",\"accountId\":\"other\"}");
            Assert.ThrowsExactly<InvalidDataException>(() => Target.Read(path));
            File.WriteAllText(path, valid[..^1] + ",\"password\":\"" + Sentinel + "\"}");
            Assert.ThrowsExactly<InvalidDataException>(() => Target.Read(path));
            File.WriteAllText(path, valid[..^1] + ",\"authority\":\"https://other.example.invalid\"}");
            Assert.ThrowsExactly<InvalidDataException>(() => Target.Read(path));
        }
        finally
        {
            File.Delete(path);
        }
    }

    [TestMethod]
    public void CommandRequiresExplicitAuthenticationFlagWithoutEchoingArguments()
    {
        using var output = new StringWriter();
        var previous = Console.Out;
        try
        {
            Console.SetOut(output);
            Assert.AreEqual(2, Program.Main(["silent", "--target", Sentinel]));
        }
        finally
        {
            Console.SetOut(previous);
        }
        Assert.IsFalse(output.ToString().Contains(Sentinel, StringComparison.Ordinal));
    }

    private static WamClient Client(FixturePlatform platform) =>
        new(platform, _ => { }, TimeSpan.FromMilliseconds(50));

    [TestMethod]
    public async Task NativeCancelFailureIsExplicitAndLateResultStillCloses()
    {
        var diagnostics = new List<Diagnostic>();
        using var cancellation = new CancellationTokenSource();
        var operation = new FixtureOperation<Reply> { ThrowOnCancel = true };
        var running = new Operations(diagnostics.Add, TimeSpan.FromMilliseconds(20)).Run(
            () => operation, TimeSpan.FromSeconds(5), cancellation.Token);
        cancellation.Cancel();
        var result = await running;
        Assert.AreEqual(OperationStatus.CancellationUnconfirmed, result.Status);
        Assert.IsFalse(result.NativeOperationCanceled);
        CollectionAssert.Contains(diagnostics, Diagnostic.NativeCancelFailed);
        operation.Complete(new(Status.Success, true, Sentinel, WamClient.ProviderId));
        await operation.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
    }

    [TestMethod]
    public async Task UnreadableNativeStateCannotConfirmCancellation()
    {
        var diagnostics = new List<Diagnostic>();
        using var cancellation = new CancellationTokenSource();
        var operation = new FixtureOperation<Reply> { ThrowOnState = true };
        var running = new Operations(diagnostics.Add, TimeSpan.FromMilliseconds(20)).Run(
            () => operation, TimeSpan.FromSeconds(5), cancellation.Token);
        cancellation.Cancel();
        var result = await running;
        Assert.AreEqual(OperationStatus.NativeError, result.Status);
        Assert.IsFalse(result.NativeOperationCanceled);
        CollectionAssert.Contains(diagnostics, Diagnostic.NativeStatusFailed);
        Assert.IsTrue(operation.Closed.Task.IsCompleted);
    }

    private sealed class FixtureOperation<T> : INativeOperation<T>
    {
        private readonly TaskCompletionSource<T> completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public Task<T> Completion => completion.Task;
        private NativeState state = NativeState.Started;
        public NativeState State => ThrowOnState ? throw new InvalidOperationException(Sentinel) : state;
        internal bool HonorCancel { get; init; } = true;
        internal bool ThrowOnCancel { get; init; }
        internal bool ThrowOnState { get; init; }
        internal bool ThrowOnClose { get; init; }
        internal int CancelCalls { get; private set; }
        internal TaskCompletionSource Closed { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        internal void Complete(T value)
        {
            state = NativeState.Completed;
            completion.SetResult(value);
        }
        internal void Fail(Exception exception)
        {
            state = NativeState.Error;
            completion.SetException(exception);
        }
        public void Cancel()
        {
            CancelCalls++;
            if (ThrowOnCancel)
            {
                throw new InvalidOperationException(Sentinel);
            }
            if (HonorCancel && state == NativeState.Started)
            {
                state = NativeState.Canceled;
                completion.SetCanceled();
            }
        }
        public void Close()
        {
            Closed.TrySetResult();
            if (ThrowOnClose)
            {
                throw new InvalidOperationException(Sentinel);
            }
        }
    }

    private sealed class FixturePlatform : IWamPlatform
    {
        public bool Available => true;
        internal Provider? Provider { get; init; } = new(WamClient.ProviderId);
        internal string AccountId { get; init; } = "synthetic-account";
        internal bool OwnedWindow { get; init; } = true;
        internal bool LoseWindowAfterFirstCheck { get; init; }
        internal bool ThrowOnAcquire { get; init; }
        internal Action? BeforeAccount { get; init; }
        internal Reply Response { get; set; } = new(Status.Success, true, "synthetic-account", WamClient.ProviderId);
        internal FixtureOperation<Reply>? Pending { get; init; }
        internal TaskCompletionSource Started { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal int ProviderCalls { get; private set; }
        internal int Acquisitions { get; private set; }
        internal Mode LastMode { get; private set; }
        private int windowChecks;

        public bool OwnsWindow(nint window) => window != 0 && OwnedWindow
            && (!LoseWindowAfterFirstCheck || ++windowChecks == 1);

        public INativeOperation<Provider?> FindProvider()
        {
            ProviderCalls++;
            return Ready(Provider);
        }

        public INativeOperation<Account?> FindAccount(Provider provider, Target target)
        {
            BeforeAccount?.Invoke();
            return Ready<Account?>(new(AccountId, provider.Id, true));
        }

        public INativeOperation<Reply> Acquire(
            Provider provider, Account account, Target target, Mode mode, nint window)
        {
            Acquisitions++;
            LastMode = mode;
            Started.TrySetResult();
            if (ThrowOnAcquire)
            {
                throw new InvalidOperationException(Sentinel);
            }
            return Pending ?? Ready(Response);
        }

        private static FixtureOperation<T> Ready<T>(T value)
        {
            var result = new FixtureOperation<T>();
            result.Complete(value);
            return result;
        }
    }

    private sealed class FixtureNativeOperation : IAsyncOperation<string>
    {
        public uint Id => 1;
        public AsyncStatus Status { get; private set; } = AsyncStatus.Started;
        public Exception ErrorCode => new InvalidOperationException(Sentinel);
        public AsyncOperationCompletedHandler<string> Completed { get; set; } = null!;
        internal int CancelCalls { get; private set; }
        internal bool Closed { get; private set; }
        public string GetResults() => Sentinel;
        public void Cancel() => CancelCalls++;
        public void Close() => Closed = true;
        internal void Complete()
        {
            Status = AsyncStatus.Completed;
            Completed(this, Status);
        }
    }
}
