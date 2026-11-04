namespace Shovel.Wam;

internal enum AcceptanceCase { Silent, InteractionRequired, Interactive, Cancel, Timeout }

internal sealed class ObservedWam : IWamPlatform
{
    private readonly WindowsWam inner = new();
    internal int Acquisitions { get; private set; }
    public bool Available => inner.Available;
    public bool OwnsWindow(nint window) => inner.OwnsWindow(window);
    public INativeOperation<Provider?> FindProvider() => inner.FindProvider();
    public INativeOperation<Account?> FindAccount(Provider provider, Target target) => inner.FindAccount(provider, target);
    public INativeOperation<Reply> Acquire(Provider provider, Account account, Target target, Mode mode, nint window)
    {
        Acquisitions++;
        return inner.Acquire(provider, account, target, mode, window);
    }
}

internal sealed record AcceptanceReceipt(
    AcceptanceCase Case, Report NativeOutcome, int AcquisitionDispatches)
{
    public int FormatVersion => 1;
    public string Scope => "wam_live_acceptance";
    public bool AuthenticationAttempted => AcquisitionDispatches > 0;
    public bool ExpectedOutcomeObserved => AuthenticationAttempted && Case switch
    {
        AcceptanceCase.Silent or AcceptanceCase.Interactive => NativeOutcome.Status == Status.Success,
        AcceptanceCase.InteractionRequired => NativeOutcome.Status == Status.InteractionRequired,
        AcceptanceCase.Cancel => NativeOutcome.Status == Status.Canceled,
        AcceptanceCase.Timeout => NativeOutcome.Status == Status.TimedOut,
        _ => false,
    };
    public bool RequiresIndependentUiAndOrganizationEvidence => true;
    public bool LiveAcceptanceComplete => false;
    public bool ProductionReady => false;
}
