using System;
using System.IO;
using System.Drawing;
using System.Windows.Forms;

namespace KwaScreenTL_Launcher;

internal sealed class TrayManager : IDisposable
{
    private readonly NotifyIcon _notifyIcon;
    private readonly ContextMenuStrip _menu;
    private bool _disposed;

    public event Action? SettingsClicked;
    public event Action? ExitClicked;

    public TrayManager()
    {
        _menu = new ContextMenuStrip();
        _menu.Items.Add("Settings", null, (_, _) => SettingsClicked?.Invoke());
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add("Exit", null, (_, _) => ExitClicked?.Invoke());

        _notifyIcon = new NotifyIcon
        {
            ContextMenuStrip = _menu,
            Visible = false
        };

        // Handle left-click to open settings
        _notifyIcon.MouseClick += (s, e) =>
        {
            if (e.Button == MouseButtons.Left)
            {
                SettingsClicked?.Invoke();
            }
        };
    }

    public void Create(string iconPath, string tooltip)
    {
        if (File.Exists(iconPath))
        {
            using var fs = new FileStream(iconPath, FileMode.Open, FileAccess.Read);
            _notifyIcon.Icon = new Icon(fs);
        }
        else
        {
            _notifyIcon.Icon = SystemIcons.Application;
        }

        _notifyIcon.Text = tooltip;
        _notifyIcon.Visible = true;
    }

    public void ShowNotification(string title, string text, int timeoutMs = 3000)
    {
        if (_disposed) return;
        _notifyIcon.ShowBalloonTip(timeoutMs, title, text, ToolTipIcon.Info);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _menu.Dispose();
    }
}
