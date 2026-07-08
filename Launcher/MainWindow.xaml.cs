using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;

namespace KwaScreenTL_Launcher

{
    public partial class MainWindow : Window
    {
        [DllImport("user32.dll", SetLastError = true)]
        static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, nuint dwExtraInfo);

        private const uint KEYEVENTF_KEYUP = 0x0002;
        private const byte VK_LCONTROL = 0x11;
        private const byte VK_LMENU = 0x12;
        private const byte VK_LSHIFT = 0x10;
        private const byte VK_S = 0x53;

        private TrayManager? _tray;
        private Process? _pythonProcess;
        private readonly string _projectDir;

        public MainWindow()
        {
            InitializeComponent();
            _projectDir = FindProjectRoot(Path.GetDirectoryName(typeof(MainWindow).Assembly.Location)!);
            Loaded += OnLoaded;

            var iconPath = Path.Combine(Path.GetDirectoryName(typeof(MainWindow).Assembly.Location)!, "AppIcon.ico");
            if (File.Exists(iconPath))
            {
                using var ico = LoadIcon(iconPath);
                Icon = System.Windows.Interop.Imaging.CreateBitmapSourceFromHIcon(
                    ico.Handle, System.Windows.Int32Rect.Empty,
                    System.Windows.Media.Imaging.BitmapSizeOptions.FromEmptyOptions());
            }
        }

        private static Icon LoadIcon(string path)
        {
            var bytes = File.ReadAllBytes(path);
            using var ms = new MemoryStream(bytes);
            return new Icon(ms);
        }

        private static string FindProjectRoot(string startDir)
        {
            var dir = startDir;
            while (dir != null)
            {
                if (File.Exists(Path.Combine(dir, "Src", "main.py")))
                    return dir;
                dir = Path.GetDirectoryName(dir);
            }
            throw new InvalidOperationException("Could not find project root (Src/main.py not found)");
        }

        private async void OnLoaded(object sender, RoutedEventArgs e)
        {
            var pythonw = Path.Combine(_projectDir, ".venv", "Scripts", "pythonw.exe");
            if (!File.Exists(pythonw))
            {
                await RunSetup();
                SetupProgress.Visibility = Visibility.Collapsed;
            }

            var flagPath = Path.Combine(_projectDir, "_app_ready.flag");

            // Show throbber during launch
            SetupProgress.IsIndeterminate = true;
            SetupProgress.Visibility = Visibility.Visible;
            StatusText.Text = "Launching KwaScreenTL...";

            StartApp();

            // Poll until ready with minimum 3s and 30s timeout
            var minStart = DateTime.UtcNow;
            var timeout = TimeSpan.FromSeconds(30);
            while (true)
            {
                if (File.Exists(flagPath))
                {
                    break;
                }
                else if (DateTime.UtcNow - minStart > timeout)
                {
                    break;
                }
                await Task.Delay(100);
            }

            try { File.Delete(flagPath); } catch { }

            StatusText.Text = "KwaScreenTL is running in the system tray";
            SetupProgress.Visibility = Visibility.Collapsed;
            MinimizeToTray();
        }

        private async Task RunSetup()
        {
            var psi = new ProcessStartInfo
            {
                FileName = "cmd.exe",
                Arguments = $"/c \"{Path.Combine(_projectDir, "Scripts", "setup.bat")}\"",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = _projectDir,
            };

            using var process = Process.Start(psi);
            if (process is null)
            {
                ShowError("Failed to start setup process.");
                return;
            }

            string? line;
            while ((line = await process.StandardOutput.ReadLineAsync()) is not null)
                Dispatcher.Invoke(() => UpdateProgress(line));

            await process.WaitForExitAsync();

            if (process.ExitCode != 0)
                ShowError("Setup failed. Check the console for details.");
        }

        private int _setupStage;

        private void UpdateProgress(string line)
        {
            var (stage, pct) = line switch
            {
                string s when s.Contains("Creating virtual environment") => (1, 05),
                string s when s.Contains("Installing dependencies")     => (2, 15),
                string s when s.Contains("Installing paddleocr")       => (3, 65),
                string s when s.Contains("GPU acceleration")           => (4, 75),
                string s when s.Contains("Downloading Jamdict")        => (5, 80),
                string s when s.Contains("Setup complete")             => (6, 100),
                _ => (_setupStage, SetupProgress.Value)
            };

            if (stage > _setupStage)
            {
                _setupStage = stage;
                SetupProgress.Value = pct;
            }

            if (pct < 100 && !string.IsNullOrWhiteSpace(line))
                StatusText.Text = line;
            else if (pct >= 100)
                StatusText.Text = "Setup complete!";
        }

        private void StartApp()
        {
            var pythonw = Path.Combine(_projectDir, ".venv", "Scripts", "pythonw.exe");
            if (!File.Exists(pythonw))
            {
                ShowError("pythonw.exe not found. Run setup.bat first.");
                return;
            }

            // Remove stale flag from previous run
            try { File.Delete(Path.Combine(_projectDir, "_app_ready.flag")); } catch { }

            var psi = new ProcessStartInfo
            {
                FileName = pythonw,
                Arguments = "Src/main.py",
                WorkingDirectory = _projectDir,
                UseShellExecute = false,
            };
            psi.EnvironmentVariables["KWASCREENTL_LAUNCHER_PID"] = Environment.ProcessId.ToString();

            _pythonProcess = Process.Start(psi);
        }

        private void MinimizeToTray()
        {
            _tray = new TrayManager();
            _tray.SettingsClicked += () => OnTraySettings(null!, null!);
            _tray.ExitClicked += () => OnTrayExit(null!, null!);
            _tray.Create(Path.Combine(_projectDir, "Launcher", "AppIcon.ico"), "KwaScreenTL — running in system tray");
            _tray.ShowNotification("KwaScreenTL", "Running in the system tray.\nRight-click the icon for Settings or Exit.", 3000);

            Hide();
        }

        private void OnTraySettings(object? sender, EventArgs e)
        {
            if (_pythonProcess is null || _pythonProcess.HasExited)
            {
                StartApp();
                // Wait a moment for the app to start before sending the command
                Thread.Sleep(1000);
            }

            try
            {
                using var client = new System.Net.Sockets.TcpClient("127.0.0.1", 54321);
                using var stream = client.GetStream();
                byte[] data = System.Text.Encoding.UTF8.GetBytes("toggle_settings");
                stream.Write(data, 0, data.Length);
            }
            catch (Exception ex)
            {
                System.Diagnostics.Debug.WriteLine($"Failed to send settings command: {ex.Message}");
            }
        }

        private void OnTrayExit(object? sender, EventArgs e)
        {
            KillPython();
            _tray?.Dispose();
            Environment.Exit(0);
        }

        private void KillPython()
        {
            try { _pythonProcess?.Kill(); } catch { }
            try { _pythonProcess?.WaitForExit(2000); } catch { }
            _pythonProcess?.Dispose();
            _pythonProcess = null;
        }

        private void ShowError(string message)
        {
            StatusText.Text = "Error";
            var result = System.Windows.MessageBox.Show(message + "\n\nRetry?", "Setup Error",
                MessageBoxButton.YesNo, MessageBoxImage.Error);
            if (result == MessageBoxResult.Yes)
                _ = RunSetup();
            else
                System.Windows.Application.Current.Shutdown();
        }

        protected override void OnClosed(EventArgs e)
        {
            KillPython();
            base.OnClosed(e);
        }
    }
}