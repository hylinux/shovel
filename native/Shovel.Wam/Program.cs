using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;

namespace Shovel.Wam;

internal static class Program
{
    internal static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    [STAThread]
    internal static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            Console.WriteLine("Shovel Windows WAM standalone component");
            Console.WriteLine("check");
            Console.WriteLine("silent|interactive --target <approved-local-json> --allow-authentication");
            Console.WriteLine("accept --case Silent|InteractionRequired|Interactive|Cancel|Timeout --target <json> --allow-authentication");
            Console.WriteLine("No token output, business requests, or production Host integration.");
            return 0;
        }
        if (args is ["attest"])
        {
            try
            {
                Installation.Load();
                Console.WriteLine("{\"scope\":\"wam_installation\",\"status\":\"trusted\",\"production_ready\":false}");
                return 0;
            }
            catch (Exception)
            {
                Console.WriteLine("{\"scope\":\"wam_installation\",\"status\":\"unavailable\",\"production_ready\":false}");
                return 3;
            }
        }
        if (args is ["bridge", var input, var secret])
        {
            try { return HostBridge.Run(input, secret); }
            catch (Exception) { Write(new(Status.Unavailable, Stage.Platform)); return 3; }
        }
        AcceptanceCase? acceptance = null;
        if (args.Length == 6 && args[0] == "accept" && args[1] == "--case"
            && Enum.TryParse<AcceptanceCase>(args[2], out var selected) && Enum.IsDefined(selected))
        {
            acceptance = selected;
            args = [selected is AcceptanceCase.Interactive or AcceptanceCase.Cancel ? "interactive" : "silent",
                args[3], args[4], args[5]];
        }
        if (args is ["check"])
        {
            try
            {
                var available = new WindowsWam().Available;
                Write(new(available ? Status.Success : Status.Unavailable, Stage.Platform));
                return available ? 0 : 3;
            }
            catch (Exception)
            {
                Write(new(Status.NativeError, Stage.Platform));
                return 3;
            }
        }
        if (args.Length != 4 || args[0] is not ("silent" or "interactive")
            || args[1] != "--target" || args[3] != "--allow-authentication")
        {
            Write(new(Status.InvalidTarget, Stage.Validation));
            return 2;
        }
        Target target;
        try
        {
            target = Target.Read(args[2]);
        }
        catch (Exception)
        {
            Write(new(Status.InvalidTarget, Stage.Validation));
            return 2;
        }
        try
        {
            return Run(target, args[0] == "silent" ? Mode.Silent : Mode.Interactive, acceptance);
        }
        catch (Exception)
        {
            Write(new(Status.NativeError, Stage.Platform));
            return 3;
        }
    }

    private static int Run(Target target, Mode mode, AcceptanceCase? acceptance)
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        using var cancellation = new CancellationTokenSource();
        var platform = new ObservedWam();
        var client = new WamClient(platform, WriteDiagnostic, TimeSpan.FromSeconds(2));
        var result = new Report(Status.Canceled, Stage.Validation);
        Window? interactionWindow = null;
        ConsoleCancelEventHandler cancel = (_, args) =>
        {
            args.Cancel = true;
            cancellation.Cancel();
            app.Dispatcher.BeginInvoke(new Action(() => interactionWindow?.Close()));
        };
        Console.CancelKeyPress += cancel;
        try
        {
            if (mode == Mode.Silent)
            {
                app.Startup += async (_, _) =>
                {
                    result = await Authenticate(client, target, mode, 0, cancellation.Token);
                    app.Shutdown();
                };
            }
            else
            {
                var panel = new StackPanel { Margin = new Thickness(24) };
                var status = new TextBlock
                {
                    Text = "Use only the approved test target. No business data will be accessed.",
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 0, 0, 16),
                };
                var start = new Button { Content = "Sign in through Windows WAM", Margin = new Thickness(0, 0, 0, 8) };
                var stop = new Button { Content = "Cancel" };
                panel.Children.Add(status);
                panel.Children.Add(start);
                panel.Children.Add(stop);
                var window = new Window
                {
                    Title = "Shovel - approved Windows WAM test",
                    Content = panel,
                    Width = 440,
                    Height = 230,
                    ResizeMode = ResizeMode.NoResize,
                };
                interactionWindow = window;
                bool started = false, finishing = false;
                window.Closing += (_, args) =>
                {
                    cancellation.Cancel();
                    args.Cancel = started && !finishing;
                    if (!started)
                    {
                        app.Shutdown();
                    }
                };
                stop.Click += (_, _) => window.Close();
                start.Click += async (_, _) =>
                {
                    if (started || cancellation.IsCancellationRequested)
                    {
                        return;
                    }
                    started = true;
                    start.IsEnabled = false;
                    status.Text = "Windows WAM may display its authentication UI. Complete it yourself or cancel.";
                    result = await Authenticate(client, target, mode,
                        new WindowInteropHelper(window).EnsureHandle(), cancellation.Token);
                    finishing = true;
                    window.Close();
                    app.Shutdown();
                };
                app.Startup += (_, _) =>
                {
                    if (cancellation.IsCancellationRequested)
                    {
                        app.Shutdown();
                    }
                    else
                    {
                        window.Show();
                    }
                };
            }
            app.Run();
        }
        finally
        {
            Console.CancelKeyPress -= cancel;
        }
        if (acceptance is not null)
        {
            var receipt = new AcceptanceReceipt(acceptance.Value, result, platform.Acquisitions);
            Console.WriteLine(JsonSerializer.Serialize(receipt, Json));
            return receipt.ExpectedOutcomeObserved ? 0 : 3;
        }
        Write(result);
        return result.Status == Status.Success ? 0 : 3;
    }

    private static async Task<Report> Authenticate(
        WamClient client, Target target, Mode mode, nint window, CancellationToken cancellation)
    {
        try
        {
            return await client.Authenticate(target, mode, window, cancellation);
        }
        catch (Exception)
        {
            return new(Status.NativeError, Stage.Acquisition);
        }
    }

    private static void Write(Report report) => Console.WriteLine(JsonSerializer.Serialize(report, Json));
    private static void WriteDiagnostic(Diagnostic diagnostic) =>
        Console.Error.WriteLine(JsonSerializer.Serialize(new { diagnostic }, Json));
}
