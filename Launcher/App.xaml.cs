using System.IO;
using System.Windows;

namespace KwaScreenTL_Launcher
{
    public partial class App : System.Windows.Application
    {
        private static readonly string LogPath = Path.Combine(
            Path.GetDirectoryName(typeof(App).Assembly.Location)!,
            "launcher_error.log");

        protected override void OnStartup(StartupEventArgs e)
        {
            DispatcherUnhandledException += (_, args) =>
            {
                LogException(args.Exception);
                System.Windows.MessageBox.Show(args.Exception.ToString(), "KwaScreenTL Error",
                    MessageBoxButton.OK, MessageBoxImage.Error);
                args.Handled = true;
            };

            System.AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            {
                LogException(args.ExceptionObject as System.Exception);
                System.Windows.MessageBox.Show(args.ExceptionObject?.ToString() ?? "Unknown error",
                    "KwaScreenTL Fatal Error", MessageBoxButton.OK, MessageBoxImage.Error);
            };

            TaskScheduler.UnobservedTaskException += (_, args) =>
            {
                LogException(args.Exception);
                args.SetObserved();
            };

            base.OnStartup(e);
            new MainWindow().Show();
        }

        private static void LogException(System.Exception? ex)
        {
            try
            {
                File.AppendAllText(LogPath, $"{System.DateTime.Now:yyyy-MM-dd HH:mm:ss} {ex}{System.Environment.NewLine}");
            }
            catch { }
        }
    }
}
