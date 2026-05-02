"""Foreground-window detection (Windows) and the multi-trigger hotkey listener."""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QObject, Signal
from pynput import keyboard


# Gamepad button name → (inputs.event_type, inputs.event_code, target_value).
# 'thresh' for analog triggers means "treat ≥ THRESHOLD as pressed".
GAMEPAD_BUTTONS = {
    "a":           ("Key", "BTN_SOUTH",  1),    # Xbox A / PS Cross
    "b":           ("Key", "BTN_EAST",   1),    # Xbox B / PS Circle
    "x":           ("Key", "BTN_WEST",   1),    # Xbox X / PS Square
    "y":           ("Key", "BTN_NORTH",  1),    # Xbox Y / PS Triangle
    "lb":          ("Key", "BTN_TL",     1),    # Xbox LB / PS L1
    "rb":          ("Key", "BTN_TR",     1),    # Xbox RB / PS R1
    "back":        ("Key", "BTN_SELECT", 1),    # Xbox Back/View / PS Share
    "start":       ("Key", "BTN_START",  1),    # Xbox Start/Menu / PS Options
    "lstick":      ("Key", "BTN_THUMBL", 1),    # left stick click
    "rstick":      ("Key", "BTN_THUMBR", 1),    # right stick click
    "dpad_up":     ("Absolute", "ABS_HAT0Y", -1),
    "dpad_down":   ("Absolute", "ABS_HAT0Y",  1),
    "dpad_left":   ("Absolute", "ABS_HAT0X", -1),
    "dpad_right":  ("Absolute", "ABS_HAT0X",  1),
    "lt":          ("Absolute", "ABS_Z",   "thresh"),  # Xbox LT / PS L2
    "rt":          ("Absolute", "ABS_RZ",  "thresh"),  # Xbox RT / PS R2
}
GAMEPAD_TRIGGER_THRESHOLD = 80  # 0..255


# Cached Win32 bindings for is_rl_focused — hoisted to avoid re-loading WinDLL each poll.
if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
else:
    _user32 = _kernel32 = None  # type: ignore[assignment]


def is_rl_focused() -> bool:
    """True when Rocket League is the foreground window. Always True on non-Windows."""
    if _user32 is None or _kernel32 is None:
        return True
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h_proc = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h_proc:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if _kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value.lower().endswith("rocketleague.exe")
        finally:
            _kernel32.CloseHandle(h_proc)
    except Exception:
        return False
    return False


class HotkeyManager(QObject):
    """Multi-trigger keyboard + gamepad listener.

    Emits ``pressed`` when going from no-triggers-held to at-least-one-held, and
    ``released`` when the last held trigger is let go. Holding multiple bindings
    simultaneously is supported (overlay stays up until *all* are released).
    """

    pressed = Signal()
    released = Signal()

    def __init__(self, hotkey_names: list[str]):
        super().__init__()
        self._kb_targets: list[tuple] = []
        self._pad_targets: list[str] = []
        self._down: set[tuple] = set()
        self._kb_listener = keyboard.Listener(
            on_press=self._on_kb_press, on_release=self._on_kb_release,
        )
        self._pad_thread: Optional[threading.Thread] = None
        self._pad_stop = threading.Event()
        self._started = False
        self._apply_bindings(hotkey_names)
        if self._pad_targets:
            self._pad_thread = threading.Thread(
                target=self._pad_loop, daemon=True, name="GamepadListener",
            )

    def _apply_bindings(self, hotkey_names: list[str]) -> None:
        kb_targets: list[tuple] = []
        pad_targets: list[str] = []
        for raw in hotkey_names:
            name = raw.strip().lower()
            if not name:
                continue
            if name.startswith("pad_"):
                pad_name = name[4:]
                if pad_name in GAMEPAD_BUTTONS:
                    pad_targets.append(pad_name)
                else:
                    print(f"[hotkey] unknown gamepad key {raw!r}; "
                          f"valid: {sorted(GAMEPAD_BUTTONS)}", file=sys.stderr)
            else:
                try:
                    kb_targets.append(self._parse_kb(name))
                except ValueError as e:
                    print(f"[hotkey] {e}", file=sys.stderr)
        self._kb_targets = kb_targets
        self._pad_targets = pad_targets

    def set_bindings(self, hotkey_names: list[str]) -> None:
        """Replace the active bindings live without restarting the listener.

        Drops any held-trigger state (we'd otherwise leak a stuck `pressed`
        if the user rebinds the action mid-hold). Spawns a gamepad thread
        on demand if pad bindings appear after start()."""
        was_held = bool(self._down)
        self._apply_bindings(hotkey_names)
        if was_held:
            self._down.clear()
            self.released.emit()
        if self._started and self._pad_targets and (
            self._pad_thread is None or not self._pad_thread.is_alive()
        ):
            self._pad_stop = threading.Event()
            self._pad_thread = threading.Thread(
                target=self._pad_loop, daemon=True, name="GamepadListener",
            )
            self._pad_thread.start()

    @staticmethod
    def _parse_kb(name: str):
        if hasattr(keyboard.Key, name):
            return ("special", getattr(keyboard.Key, name))
        if len(name) == 1:
            return ("char", name)
        raise ValueError(
            f"Unknown keyboard key {name!r}. Use 'tab', 'f1', 'shift' (etc.), "
            "a single char like 'h', or prefix with 'pad_' for a gamepad button."
        )

    def _kb_match(self, key, target) -> bool:
        kind, value = target
        if kind == "special":
            return key == value
        return getattr(key, "char", None) == value

    def _on_kb_press(self, key):
        for t in self._kb_targets:
            if self._kb_match(key, t):
                self._add_down(("kb",) + t)
                return

    def _on_kb_release(self, key):
        for t in self._kb_targets:
            if self._kb_match(key, t):
                self._remove_down(("kb",) + t)
                return

    def _add_down(self, key_id: tuple):
        was_empty = not self._down
        self._down.add(key_id)
        if was_empty:
            self.pressed.emit()

    def _remove_down(self, key_id: tuple):
        self._down.discard(key_id)
        if not self._down:
            self.released.emit()

    def _pad_loop(self):
        try:
            import inputs as _inputs
        except ImportError:
            print("[hotkey] gamepad bindings configured but 'inputs' is not installed. "
                  "Run: pip install inputs", file=sys.stderr)
            return
        active: dict[str, bool] = {}
        warned_no_pad = False
        print(f"[hotkey] gamepad listener watching: {self._pad_targets}", file=sys.stderr)
        while not self._pad_stop.is_set():
            try:
                events = _inputs.get_gamepad()
            except _inputs.UnpluggedError:
                if not warned_no_pad:
                    print("[hotkey] no gamepad detected; will keep watching", file=sys.stderr)
                    warned_no_pad = True
                self._pad_stop.wait(2.0)
                continue
            except Exception as e:
                print(f"[hotkey] gamepad read error: {type(e).__name__}: {e}", file=sys.stderr)
                self._pad_stop.wait(2.0)
                continue
            warned_no_pad = False
            # Rebuild on each batch so set_bindings() applies live without a thread restart.
            wanted: dict[tuple, list[tuple]] = {}
            for pad_name in self._pad_targets:
                etype, ecode, target_val = GAMEPAD_BUTTONS[pad_name]
                wanted.setdefault((etype, ecode), []).append((pad_name, target_val))
            for ev in events:
                key = (ev.ev_type, ev.code)
                if key not in wanted:
                    continue
                for pad_name, target_val in wanted[key]:
                    if target_val == "thresh":
                        is_pressed = ev.state >= GAMEPAD_TRIGGER_THRESHOLD
                    elif ev.ev_type == "Absolute":
                        is_pressed = ev.state == target_val
                    else:
                        is_pressed = ev.state == 1
                    was = active.get(pad_name, False)
                    if is_pressed and not was:
                        active[pad_name] = True
                        self._add_down(("pad", pad_name))
                    elif not is_pressed and was:
                        active[pad_name] = False
                        self._remove_down(("pad", pad_name))

    def start(self):
        self._kb_listener.start()
        if self._pad_thread is not None:
            self._pad_thread.start()
        self._started = True

    def stop(self):
        self._kb_listener.stop()
        self._pad_stop.set()


# Windows virtual-key codes for menu navigation. Used by MenuHotkeyListener
# to identify keys inside the low-level keyboard hook (where we get a vk_code,
# not a pynput Key object).
_VK_BY_NAME: dict[str, int] = {
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "caps_lock": 0x14,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},  # F1=0x70..F12=0x7B
}


def _name_to_vk(name: Optional[str]) -> Optional[int]:
    """Config-style binding name → Windows virtual-key code, or None for
    pad bindings / unknowns. Single-char keys map by ord(uppercase)."""
    if not name:
        return None
    n = name.strip().lower()
    if n.startswith("pad_"):
        return None
    if n in _VK_BY_NAME:
        return _VK_BY_NAME[n]
    if len(n) == 1:
        return ord(n.upper())
    return None


class MenuHotkeyListener(QObject):
    """Keyboard listener for the in-game settings menu.

    On Windows, uses pynput's ``win32_event_filter`` to selectively suppress
    the menu key (always, when menu isn't capturing) and the nav keys
    (↑/↓/Enter, only while the menu is open and not in rebind capture). The
    suppression prevents Rocket League from also reacting to those presses
    — without it, Enter would also trigger RL's UI confirm, etc.

    On non-Windows platforms there's no suppression hook in pynput's stable
    API; we fall back to a regular listener that dispatches signals but lets
    keys pass through to the foreground app. Suppression matters for
    in-game use (Windows-only), so the macOS dev path is fine.
    """

    toggle = Signal()
    up = Signal()
    down = Signal()
    enter = Signal()

    # Windows hook message types. We only emit on key-down (and only on the
    # down-edge — the LL hook receives auto-repeat WM_KEYDOWN events too,
    # which would otherwise flicker the menu open/closed on a held key).
    _WM_KEYDOWN = 0x0100
    _WM_KEYUP = 0x0101
    _WM_SYSKEYDOWN = 0x0104
    _WM_SYSKEYUP = 0x0105

    def __init__(self, menu_key_cb, is_visible_cb, is_capturing_cb):
        super().__init__()
        self._menu_key_cb = menu_key_cb
        self._is_visible_cb = is_visible_cb
        self._is_capturing_cb = is_capturing_cb
        self._held: set[int] = set()
        if sys.platform == "win32":
            self._listener = keyboard.Listener(
                on_press=lambda k: None,
                on_release=lambda k: None,
                win32_event_filter=self._win32_filter,
            )
        else:
            self._listener = keyboard.Listener(on_press=self._on_press_generic)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    @staticmethod
    def _vk_of(data) -> int:
        """Read vkCode out of pynput's KBDLLHOOKSTRUCT (field name varies
        across pynput versions: vk_code in modern, vkCode in older)."""
        return getattr(data, "vk_code", None) or getattr(data, "vkCode", 0)

    def _win32_filter(self, msg, data) -> bool:
        vk = self._vk_of(data)
        if msg in (self._WM_KEYUP, self._WM_SYSKEYUP):
            self._held.discard(vk)
            return True
        if msg not in (self._WM_KEYDOWN, self._WM_SYSKEYDOWN):
            return True
        # Down-edge only: the LL hook fires WM_KEYDOWN for every auto-repeat.
        if vk in self._held:
            # Still suppress repeats for keys we'd suppress on the down-edge,
            # otherwise RL would see only the repeats.
            menu_vk_now = _name_to_vk(self._menu_key_cb())
            if vk == menu_vk_now and not self._is_capturing_cb():
                return False
            if self._is_visible_cb() and not self._is_capturing_cb() and vk in (
                _VK_BY_NAME["up"], _VK_BY_NAME["down"], _VK_BY_NAME["enter"],
            ):
                return False
            return True
        self._held.add(vk)
        capturing = self._is_capturing_cb()
        visible = self._is_visible_cb()
        menu_vk = _name_to_vk(self._menu_key_cb())

        if menu_vk is not None and vk == menu_vk:
            # During capture, the menu key is being captured as a new
            # binding — let it propagate so capture_next_input's listener
            # sees it and so the user gets a chance to cancel via Esc.
            if capturing:
                return True
            self.toggle.emit()
            return False  # suppress so RL doesn't also see the press

        if not visible or capturing:
            return True

        if vk == _VK_BY_NAME["up"]:
            self.up.emit()
            return False
        if vk == _VK_BY_NAME["down"]:
            self.down.emit()
            return False
        if vk == _VK_BY_NAME["enter"]:
            self.enter.emit()
            return False
        return True

    def _on_press_generic(self, key) -> None:
        # Non-Windows fallback: dispatch but don't suppress.
        name = _kb_event_name(key)
        if name is None:
            return
        capturing = self._is_capturing_cb()
        if name == (self._menu_key_cb() or "f5"):
            if capturing:
                return
            self.toggle.emit()
            return
        if not self._is_visible_cb() or capturing:
            return
        if name == "up":
            self.up.emit()
        elif name == "down":
            self.down.emit()
        elif name == "enter":
            self.enter.emit()


def _kb_event_name(key) -> Optional[str]:
    """pynput key → config-style binding name. None for unbindable events
    (modifier-only releases, dead keys)."""
    if hasattr(key, "name"):  # special keys: Tab, F1, Esc, …
        return key.name.lower()
    char = getattr(key, "char", None)
    if char and len(char) == 1:
        return char.lower()
    return None


def capture_next_input(on_done) -> None:
    """Listen for one keyboard or gamepad press, then call `on_done(name)`.

    `name` is a config-style binding string ('y', 'f1', 'tab', 'pad_lb', …)
    or None if the user pressed Esc to cancel. Listeners stop after firing
    once. Runs concurrently with any active HotkeyManager — pynput supports
    multiple listeners observing the same key stream."""
    done = threading.Event()
    pad_stop = threading.Event()

    def _emit(name: Optional[str]) -> None:
        if done.is_set():
            return
        done.set()
        pad_stop.set()
        on_done(name)

    def _on_press(key) -> bool:
        if key == keyboard.Key.esc:
            _emit(None)
            return False
        name = _kb_event_name(key)
        if name is None:
            return None
        _emit(name)
        return False  # stop the listener after the first valid press

    listener = keyboard.Listener(on_press=_on_press)
    listener.start()

    def _pad_loop():
        try:
            import inputs as _inputs
        except ImportError:
            return
        wanted: dict[tuple, list[tuple]] = {}
        for pad_name, (etype, ecode, target_val) in GAMEPAD_BUTTONS.items():
            wanted.setdefault((etype, ecode), []).append((pad_name, target_val))
        while not pad_stop.is_set():
            try:
                events = _inputs.get_gamepad()
            except _inputs.UnpluggedError:
                pad_stop.wait(1.0)
                continue
            except Exception:
                pad_stop.wait(1.0)
                continue
            for ev in events:
                key = (ev.ev_type, ev.code)
                if key not in wanted:
                    continue
                for pad_name, target_val in wanted[key]:
                    if target_val == "thresh":
                        if ev.state >= GAMEPAD_TRIGGER_THRESHOLD:
                            _emit(f"pad_{pad_name}")
                            return
                    elif ev.ev_type == "Absolute":
                        if ev.state == target_val:
                            _emit(f"pad_{pad_name}")
                            return
                    else:
                        if ev.state == 1:
                            _emit(f"pad_{pad_name}")
                            return
                if done.is_set():
                    return

    threading.Thread(target=_pad_loop, daemon=True, name="GamepadCapture").start()
