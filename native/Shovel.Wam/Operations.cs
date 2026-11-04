namespace Shovel.Wam;

internal enum NativeState { Started, Completed, Canceled, Error }
internal enum OperationStatus { Success, Canceled, TimedOut, NativeError, CancellationUnconfirmed }
internal enum Diagnostic { NativeCancelFailed, NativeCloseFailed, NativeStatusFailed, LateNativeFailure }

internal interface INativeOperation<T>
{
    Task<T> Completion { get; }
    NativeState State { get; }
    void Cancel();
    void Close();
}

internal sealed record OperationResult<T>(OperationStatus Status, T? Value, bool NativeOperationCanceled);

internal sealed class Operations(Action<Diagnostic> diagnostic, TimeSpan cancellationGrace)
{
    internal async Task<OperationResult<T>> Run<T>(
        Func<INativeOperation<T>> start, TimeSpan remaining, CancellationToken cancellation)
    {
        if (cancellation.IsCancellationRequested)
        {
            return new(OperationStatus.Canceled, default, false);
        }
        if (remaining <= TimeSpan.Zero)
        {
            return new(OperationStatus.TimedOut, default, false);
        }
        INativeOperation<T> operation;
        try
        {
            operation = start();
        }
        catch (Exception)
        {
            return new(OperationStatus.NativeError, default, false);
        }
        OperationResult<T> result;
        var closed = true;
        try
        {
            try
            {
                var value = await operation.Completion.WaitAsync(remaining, cancellation);
                result = cancellation.IsCancellationRequested
                    ? await Cancel(operation, OperationStatus.Canceled)
                    : new(OperationStatus.Success, value, false);
            }
            catch (TimeoutException)
            {
                result = await Cancel(operation, OperationStatus.TimedOut);
            }
            catch (OperationCanceledException)
            {
                result = await Cancel(operation, OperationStatus.Canceled);
            }
            catch (Exception)
            {
                result = await Cancel(operation, OperationStatus.NativeError);
            }
        }
        finally
        {
            if (!operation.Completion.IsCompleted)
            {
                _ = Drain(operation);
            }
            else
            {
                closed = Close(operation);
            }
        }
        return closed ? result : new(OperationStatus.NativeError, default, false);
    }

    private async Task<OperationResult<T>> Cancel<T>(INativeOperation<T> operation, OperationStatus reason)
    {
        try
        {
            operation.Cancel();
        }
        catch (Exception)
        {
            diagnostic(Diagnostic.NativeCancelFailed);
        }
        // Observe native completion; canceling only the managed await is not proof of native cancellation.
        try
        {
            var value = await operation.Completion.WaitAsync(cancellationGrace);
            if (value is IDisposable disposable) { disposable.Dispose(); }
        }
        catch (TimeoutException)
        {
            return new(OperationStatus.CancellationUnconfirmed, default, false);
        }
        catch (OperationCanceledException) { }
        catch (Exception)
        {
            diagnostic(Diagnostic.LateNativeFailure);
        }
        try
        {
            return new(reason, default, operation.State == NativeState.Canceled);
        }
        catch (Exception)
        {
            diagnostic(Diagnostic.NativeStatusFailed);
            return new(OperationStatus.NativeError, default, false);
        }
    }

    private async Task Drain<T>(INativeOperation<T> operation)
    {
        try
        {
            var value = await operation.Completion.ConfigureAwait(false);
            if (value is IDisposable disposable) { disposable.Dispose(); }
        }
        catch (OperationCanceledException) { }
        catch (Exception)
        {
            diagnostic(Diagnostic.LateNativeFailure);
        }
        finally
        {
            Close(operation);
        }
    }

    private bool Close<T>(INativeOperation<T> operation)
    {
        try
        {
            operation.Close();
            return true;
        }
        catch (Exception)
        {
            diagnostic(Diagnostic.NativeCloseFailed);
            return false;
        }
    }
}
