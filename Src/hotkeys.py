import ctypes
import time

user32 = ctypes.windll.user32
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_LCTRL = 0xA2
VK_RCTRL = 0xA3
VK_LALT = 0xA4
VK_RALT = 0xA5
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1

HK_CAPTURE  = 1
HK_SNIP     = 2
HK_SETTINGS = 3

def _mods_from_hk(hk):
    return hk["mod"] & ~MOD_NOREPEAT | MOD_NOREPEAT

_VK_MODS = {
    VK_LCTRL: "Ctrl", VK_RCTRL: "Ctrl",
    VK_LALT: "Alt", VK_RALT: "Alt",
    VK_LSHIFT: "Shift", VK_RSHIFT: "Shift",
}

def _vk_to_display(vk):
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x7B:
        return f"F{vk - 0x6F}"
    return f"VK_{vk}"

def _hk_display(hk):
    parts = []
    m = hk["mod"]
    if m & MOD_CONTROL: parts.append("Ctrl")
    if m & MOD_ALT: parts.append("Alt")
    if m & MOD_SHIFT: parts.append("Shift")
    parts.append(_vk_to_display(hk["vk"]))
    return "+".join(parts)

def register_hotkey_win32(app):
    msg = ctypes.wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

    def do_register():
        user32.UnregisterHotKey(None, HK_CAPTURE)
        user32.UnregisterHotKey(None, HK_SNIP)
        user32.UnregisterHotKey(None, HK_SETTINGS)
        hk = app.hk_capture
        user32.RegisterHotKey(None, HK_CAPTURE, _mods_from_hk(hk), hk["vk"])
        hk = app.hk_snip
        user32.RegisterHotKey(None, HK_SNIP, _mods_from_hk(hk), hk["vk"])
        hk = app.hk_settings
        user32.RegisterHotKey(None, HK_SETTINGS, _mods_from_hk(hk), hk["vk"])

    do_register()

    def is_key_down(vk):
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    pressed_flag = 0
    while True:
        suppress = (app.settings.recording_action is not None
                    or time.time() < app.settings.hk_suppress_until)
        while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, 1):
            if suppress:
                continue
            if msg.wParam == HK_CAPTURE:
                pressed_flag |= 1
                app.trigger()
            elif msg.wParam == HK_SNIP:
                pressed_flag |= 2
                app.trigger_snip()
            elif msg.wParam == HK_SETTINGS:
                pressed_flag |= 4
                app.trigger_settings()

        if app._hk_dirty.is_set():
            do_register()
            app._hk_dirty.clear()

        if suppress:
            pressed_flag = 0
            time.sleep(0.05)
            continue

        ctrl_down = is_key_down(VK_LCTRL) or is_key_down(VK_RCTRL)
        alt_down = is_key_down(VK_LALT) or is_key_down(VK_RALT)
        shift_down = is_key_down(VK_LSHIFT) or is_key_down(VK_RSHIFT)

        def mods_match(hk):
            m = hk["mod"]
            if m & MOD_CONTROL:
                if not ctrl_down: return False
            elif ctrl_down:
                return False
            if m & MOD_ALT:
                if not alt_down: return False
            elif alt_down:
                return False
            if m & MOD_SHIFT:
                if not shift_down: return False
            elif shift_down:
                return False
            return True

        hk = app.hk_capture
        if mods_match(hk) and is_key_down(hk["vk"]):
            if not (pressed_flag & 1):
                pressed_flag |= 1
                app.trigger()
        else:
            pressed_flag &= ~1

        hk = app.hk_snip
        if mods_match(hk) and is_key_down(hk["vk"]):
            if not (pressed_flag & 2):
                pressed_flag |= 2
                app.trigger_snip()
        else:
            pressed_flag &= ~2

        hk = app.hk_settings
        if mods_match(hk) and is_key_down(hk["vk"]):
            if not (pressed_flag & 4):
                pressed_flag |= 4
                app.trigger_settings()
        else:
            pressed_flag &= ~4

        time.sleep(0.05)
