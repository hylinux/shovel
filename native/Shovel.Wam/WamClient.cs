using System.Diagnostics;
using System.IO;
using System.Text.Json.Serialization;
using Windows.Security.Authentication.Web.Core;
using Windows.Security.Credentials;

namespace Shovel.Wam;

internal enum Mode { Silent, Interactive }
internal enum Status
{
    Success, Unavailable, InvalidTarget, InvalidWindow, AccountUnavailable, BindingRejected,
    InteractionRequired, ProviderError, Canceled, TimedOut, NativeError, CancellationUnconfirmed
}
internal enum Stage { Platform, Validation, Provider, Account, Acquisition }

internal sealed record Report(Status Status, Stage Stage, bool NativeOperationCanceled = false)
{
    public int FormatVersion => 1;
    public string Scope => "standalone_wam";
    public bool ProductionReady => false;
    public bool LiveAcceptanceComplete => false;
}

internal sealed class Provider(string id, WebAccountProvider? native = null)
{
    internal string Id { get; } = id;
    internal WebAccountProvider? Native { get; } = native;
    public override string ToString() => "<WAM provider: redacted>";
}

internal sealed class Account(string id, string providerId, bool connected, WebAccount? native = null)
{
    internal string Id { get; } = id;
    internal string ProviderId { get; } = providerId;
    internal bool Connected { get; } = connected;
    internal WebAccount? Native { get; } = native;
    public override string ToString() => "<WAM account: redacted>";
}

internal sealed record Reply(Status Status, bool HasToken, string? AccountId, string? ProviderId,
    [property: JsonIgnore] Credential? Credential = null) : IDisposable
{
    public override string ToString() => "<WAM reply: redacted>";
    public void Dispose() => Credential?.Dispose();
}

internal interface IWamPlatform
{
    bool Available { get; }
    bool OwnsWindow(nint window);
    INativeOperation<Provider?> FindProvider();
    INativeOperation<Account?> FindAccount(Provider provider, Target target);
    INativeOperation<Reply> Acquire(Provider provider, Account account, Target target, Mode mode, nint window);
}

internal sealed class WamClient(IWamPlatform platform, Action<Diagnostic> diagnostic, TimeSpan cancellationGrace)
{
    internal const string ProviderId = "https://login.microsoft.com";

    internal async Task<Report> Authenticate(Target target, Mode mode, nint window, CancellationToken cancellation,
        Func<Credential, Task>? consume = null)
    {
        try
        {
            target.Validate();
            if (!Enum.IsDefined(mode))
            {
                return new(Status.InvalidTarget, Stage.Validation);
            }
        }
        catch (InvalidDataException)
        {
            return new(Status.InvalidTarget, Stage.Validation);
        }
        if (!platform.Available)
        {
            return new(Status.Unavailable, Stage.Platform);
        }
        if (mode == Mode.Interactive && !platform.OwnsWindow(window))
        {
            return new(Status.InvalidWindow, Stage.Validation);
        }
        var clock = Stopwatch.StartNew();
        var budget = TimeSpan.FromSeconds(target.TimeoutSeconds);
        var operations = new Operations(diagnostic, cancellationGrace);
        var provider = await operations.Run(platform.FindProvider, budget - clock.Elapsed, cancellation);
        if (provider.Status != OperationStatus.Success)
        {
            return Failure(provider, Stage.Provider);
        }
        if (provider.Value is null || provider.Value.Id != ProviderId)
        {
            return new(Status.Unavailable, Stage.Provider);
        }
        var account = await operations.Run(
            () => platform.FindAccount(provider.Value, target), budget - clock.Elapsed, cancellation);
        if (account.Status != OperationStatus.Success)
        {
            return Failure(account, Stage.Account);
        }
        if (account.Value is null || !account.Value.Connected)
        {
            return new(Status.AccountUnavailable, Stage.Account);
        }
        if (account.Value.Id != target.AccountId || account.Value.ProviderId != ProviderId)
        {
            return new(Status.BindingRejected, Stage.Account);
        }
        if (mode == Mode.Interactive && !platform.OwnsWindow(window))
        {
            return new(Status.InvalidWindow, Stage.Acquisition);
        }
        var reply = await operations.Run(
            () => platform.Acquire(provider.Value, account.Value, target, mode, window),
            budget - clock.Elapsed, cancellation);
        if (reply.Status != OperationStatus.Success)
        {
            return Failure(reply, Stage.Acquisition);
        }
        using var secret = reply.Value;
        if (clock.Elapsed >= budget || cancellation.IsCancellationRequested)
        {
            return new(cancellation.IsCancellationRequested ? Status.Canceled : Status.TimedOut, Stage.Acquisition);
        }
        if (reply.Value is null)
        {
            return new(Status.NativeError, Stage.Acquisition);
        }
        if (reply.Value.Status != Status.Success)
        {
            return new(reply.Value.Status, Stage.Acquisition);
        }
        if (!reply.Value.HasToken || reply.Value.AccountId != target.AccountId
            || reply.Value.ProviderId != ProviderId)
        {
            return new(Status.BindingRejected, Stage.Acquisition);
        }
        if (consume is not null)
        {
            if (reply.Value.Credential is null) { return new(Status.NativeError, Stage.Acquisition); }
            await consume(reply.Value.Credential);
        }
        return new(Status.Success, Stage.Acquisition);
    }

    private static Report Failure<T>(OperationResult<T> result, Stage stage) => new(
        result.Status switch
        {
            OperationStatus.Canceled => Status.Canceled,
            OperationStatus.TimedOut => Status.TimedOut,
            OperationStatus.CancellationUnconfirmed => Status.CancellationUnconfirmed,
            _ => Status.NativeError,
        }, stage, result.NativeOperationCanceled);
}
