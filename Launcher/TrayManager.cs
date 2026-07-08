using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Drawing;

namespace KwaScreenTL_Launcher;

internal sealed class TrayManager : IDisposable
{
    private const uint NIM_ADD = 0;
    private const uint NIM_MODIFY = 1;
    private const uint NIM_DELETE = 2;
    private const uint NIF_MESSAGE = 1;
    private const uint NIF_ICON = 2;
    private const uint NIF_TIP = 4;
    private const uint NIF_INFO = 0x10;
    private const uint NIIF_USER = 4;
    private const uint NIIF_LARGE_ICON = 0x20;
    private const uint WM_USER = 0x0400;
    private const uint WM_TRAY_CALLBACK = WM_USER + 100;
    private const uint WM_LBUTTONUP = 0x0202;
    private const uint WM_RBUTTONUP = 0x0205;
    private const int GWLP_USERDATA = -21;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NOTIFYICONDATAW
    {
        public int cbSize;
        public IntPtr hWnd;
        public uint uID;
        public uint uFlags;
        public uint uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string szTip;
        public uint dwState;
        public uint dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string szInfo;
        public uint uTimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string szInfoTitle;
        public uint dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSW
    {
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        public string lpszMenuName;
        public string lpszClassName;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern bool Shell_NotifyIconW(uint message, ref NOTIFYICONDATAW data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern ushort RegisterClassW(ref WNDCLASSW wc);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateWindowExW(uint exStyle, string className, string windowName,
        uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu,
        IntPtr instance, IntPtr param);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool UnregisterClassW(string className, IntPtr hInstance);

    [DllImport("user32.dll")]
    private static extern IntPtr SetWindowLongPtrW(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll")]
    private static extern IntPtr GetWindowLongPtrW(IntPtr hWnd, int nIndex);

    private const uint WS_POPUP = 0x80000000;
    private static readonly Guid GUID_TRAY = new("D3E34B21-9D75-4F1A-8F2C-8A5E5C5E5C5E");
    private const string WINDOW_CLASS = "KwaScreenTL_TrayWindow";

    private static uint _nextID;
    private readonly uint _uID;
    private IntPtr _hWnd;
    private Icon? _icon;
    private readonly ContextMenuStrip _menu;
    private bool _disposed;

    public event Action? SettingsClicked;
    public event Action? ExitClicked;

    public TrayManager()
    {
        _uID = ++_nextID;
        _menu = new ContextMenuStrip();
        _menu.Items.Add("Settings", null, (_, _) => SettingsClicked?.Invoke());
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add("Exit", null, (_, _) => ExitClicked?.Invoke());
    }

    public void Create(string iconPath, string tooltip)
    {
        _icon = File.Exists(iconPath) ? LoadIcon(iconPath) : SystemIcons.Application;

        var hInstance = GetModuleHandleW(null);

        var wc = new WNDCLASSW
        {
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(WndProc),
            hInstance = hInstance,
            lpszClassName = WINDOW_CLASS,
            style = 0,
        };
        if (RegisterClassW(ref wc) == 0) { }

        _hWnd = CreateWindowExW(0, WINDOW_CLASS, "TrayWindow", WS_POPUP,
            0, 0, 0, 0, IntPtr.Zero, IntPtr.Zero, hInstance, IntPtr.Zero);
        if (_hWnd == IntPtr.Zero)
            throw new InvalidOperationException("Failed to create tray window");

        var handle = GCHandle.Alloc(this);
        SetWindowLongPtrW(_hWnd, GWLP_USERDATA, GCHandle.ToIntPtr(handle));

        var data = new NOTIFYICONDATAW
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATAW>(),
            hWnd = _hWnd,
            uID = _uID,
            uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage = WM_TRAY_CALLBACK,
            hIcon = _icon.Handle,
            szTip = tooltip,
            guidItem = GUID_TRAY,
        };
        if (!Shell_NotifyIconW(NIM_ADD, ref data))
        {
            DestroyWindow(_hWnd);
            throw new InvalidOperationException("Shell_NotifyIconW NIM_ADD failed");
        }
    }

    public void ShowNotification(string title, string text, int timeoutMs = 3000)
    {
        if (_disposed || _hWnd == IntPtr.Zero || _icon == null)
            return;

        var data = new NOTIFYICONDATAW
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATAW>(),
            hWnd = _hWnd,
            uID = _uID,
            uFlags = NIF_INFO,
            dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON,
            hBalloonIcon = _icon.Handle,
            szInfoTitle = title,
            szInfo = text,
            uTimeoutOrVersion = (uint)timeoutMs,
            guidItem = GUID_TRAY,
        };
        Shell_NotifyIconW(NIM_MODIFY, ref data);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        var data = new NOTIFYICONDATAW
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATAW>(),
            hWnd = _hWnd,
            uID = _uID,
            guidItem = GUID_TRAY,
        };
        Shell_NotifyIconW(NIM_DELETE, ref data);

        if (_hWnd != IntPtr.Zero)
            DestroyWindow(_hWnd);

        UnregisterClassW(WINDOW_CLASS, GetModuleHandleW(null));

        _icon?.Dispose();
        _menu.Dispose();
    }

    private static readonly WndProcDelegate WndProc = OnWndProc;
    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    private static IntPtr OnWndProc(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == WM_TRAY_CALLBACK)
        {
            var action = (uint)lParam.ToInt64();
            if (action == WM_RBUTTONUP || action == WM_LBUTTONUP)
            {
                var handlePtr = GetWindowLongPtrW(hWnd, GWLP_USERDATA);
                if (handlePtr != IntPtr.Zero)
                {
                    var gch = GCHandle.FromIntPtr(handlePtr);
                    if (gch.IsAllocated && gch.Target is TrayManager self && !self._disposed)
                    {
                        self._menu.Show(Cursor.Position);
                    }
                }
            }
            return IntPtr.Zero;
        }
        return DefWindowProcW(hWnd, msg, wParam, lParam);
    }

    private static Icon LoadIcon(string path)
    {
        var bytes = File.ReadAllBytes(path);
        using var ms = new MemoryStream(bytes);
        return new Icon(ms);
    }
}
