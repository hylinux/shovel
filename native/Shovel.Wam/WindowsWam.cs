using System.Runtime.InteropServices;
using Windows.Foundation;
using Windows.Foundation.Metadata;
using Windows.Security.Authentication.Web.Core;
using Windows.Security.Credentials;

namespace Shovel.Wam;

internal sealed class WindowsWam : IWamPlatform
{
    public bool Available => OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000)
        && Environment.Is64BitProcess && Environment.UserInteractive
        && ApiInformation.IsTypePresent("Windows.Security.Authentication.Web.Core.WebAuthenticationCoreManager");

    public bool OwnsWindow(nint window) => window != 0 && IsWindow(window)
        && GetWindowThreadProcessId(window, out var process) == GetCurrentThreadId()
        && process == Environment.ProcessId;

    public INativeOperation<Provider?> FindProvider() => new WindowsOperation<WebAccountProvider, Provider?>(
        WebAuthenticationCoreManager.FindAccountProviderAsync(WamClient.ProviderId, "organizations"),
        provider => provider is null ? null : new Provider(provider.Id, provider));

    public INativeOperation<Account?> FindAccount(Provider provider, Target target) =>
        new WindowsOperation<WebAccount, Account?>(
            WebAuthenticationCoreManager.FindAccountAsync(
                provider.Native ?? throw new InvalidOperationException(), target.AccountId),
            account => account is null ? null : new Account(
                account.Id, account.WebAccountProvider.Id, account.State == WebAccountState.Connected, account));

    internal static WebTokenRequest CreateRequest(WebAccountProvider provider, Target target)
    {
        target.Validate();
        var request = new WebTokenRequest(provider, string.Join(" ", target.Scopes), target.ClientId.ToString("D"))
        {
            CorrelationId = Guid.NewGuid().ToString("D"),
        };
        request.Properties.Add("authority", target.Authority);
        request.Properties.Add("resource", target.Resource);
        request.Properties.Add("redirect_uri", $"ms-appx-web://Microsoft.AAD.BrokerPlugin/{target.ClientId:D}");
        return request;
    }

    public INativeOperation<Reply> Acquire(
        Provider provider, Account account, Target target, Mode mode, nint window)
    {
        var request = CreateRequest(provider.Native ?? throw new InvalidOperationException(), target);
        var nativeAccount = account.Native ?? throw new InvalidOperationException();
        var operation = mode == Mode.Silent
            ? WebAuthenticationCoreManager.GetTokenSilentlyAsync(request, nativeAccount)
            : WebAuthenticationCoreManagerInterop.RequestTokenWithWebAccountForWindowAsync(
                OwnsWindow(window) ? window : throw new InvalidOperationException(), request, nativeAccount);
        return new WindowsOperation<WebTokenRequestResult, Reply>(operation, Decode);
    }

    private static Reply Decode(WebTokenRequestResult result)
    {
        var status = result.ResponseStatus switch
        {
            WebTokenRequestStatus.Success => Status.Success,
            WebTokenRequestStatus.UserCancel => Status.Canceled,
            WebTokenRequestStatus.AccountSwitch => Status.BindingRejected,
            WebTokenRequestStatus.UserInteractionRequired => Status.InteractionRequired,
            WebTokenRequestStatus.AccountProviderNotAvailable => Status.Unavailable,
            _ => Status.ProviderError,
        };
        if (status != Status.Success || result.ResponseData.Count != 1)
        {
            return new(status == Status.Success ? Status.BindingRejected : status, false, null, null);
        }
        var response = result.ResponseData[0];
        // The standalone component never returns or persists the credential.
        return new(Status.Success, !string.IsNullOrWhiteSpace(response.Token),
            response.WebAccount?.Id, response.WebAccount?.WebAccountProvider.Id);
    }

    [DllImport("user32.dll", ExactSpelling = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindow(nint window);

    [DllImport("user32.dll", ExactSpelling = true)]
    private static extern uint GetWindowThreadProcessId(nint window, out uint process);

    [DllImport("kernel32.dll", ExactSpelling = true)]
    private static extern uint GetCurrentThreadId();
}

internal sealed class WindowsOperation<TNative, T> : INativeOperation<T>
{
    private readonly IAsyncOperation<TNative> operation;
    private volatile bool cancellationRequested;
    public Task<T> Completion { get; }
    public NativeState State => operation.Status switch
    {
        AsyncStatus.Started => NativeState.Started,
        AsyncStatus.Completed => NativeState.Completed,
        AsyncStatus.Canceled => NativeState.Canceled,
        _ => NativeState.Error,
    };

    internal WindowsOperation(IAsyncOperation<TNative> operation, Func<TNative, T> decode)
    {
        this.operation = operation;
        Completion = Decode(decode);
    }

    private async Task<T> Decode(Func<TNative, T> decode)
    {
        var result = await operation.AsTask().ConfigureAwait(false);
        if (cancellationRequested)
        {
            throw new OperationCanceledException();
        }
        return decode(result);
    }

    public void Cancel()
    {
        cancellationRequested = true;
        operation.Cancel();
    }
    public void Close() => operation.Close();
}
