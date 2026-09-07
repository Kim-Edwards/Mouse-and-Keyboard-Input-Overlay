"""
Input Overlay - shows your keyboard and mouse input on screen.
Useful for screen recordings, tutorials, or streaming.

Requirements:
    pip install pynput

Run:
    python input_overlay.py

Controls:
    - Left-click and drag the overlay to reposition it.
    - Right-click the overlay to open the Settings panel
      (font, size, position, opacity, background color).
    - Settings are saved to input_overlay_config.json next to this
      script, so they persist between runs.
    - Close the overlay window (or the Settings window's "Quit" button)
      to exit.

Notes:
    - macOS: System Settings > Privacy & Security > Accessibility must
      allow your terminal/Python to monitor input.
    - Linux (X11): usually works out of the box. Wayland may restrict
      global input capture depending on your compositor.
    - Windows: works out of the box.
    - This only *displays* input on your own screen; it does not log
      or send anything anywhere.
"""

import ctypes
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from pynput import keyboard, mouse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_overlay_config.json")

DEFAULT_CONFIG = {
    "font_family": "Impact",
    "font_size": 22,
    "pos_x": 1100,
    "pos_y": 1280,
    "bg_color": "#101010",
    "fg_color": "#00FF90",
    "opacity": 0.85,
    "max_lines": 1,
    "fade_seconds": 2.5,
}


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


MODIFIER_NAMES = {
    keyboard.Key.ctrl_l: "Ctrl", keyboard.Key.ctrl_r: "Ctrl",
    keyboard.Key.alt_l: "Alt", keyboard.Key.alt_r: "Alt",
    keyboard.Key.shift_l: "Shift", keyboard.Key.shift_r: "Shift",
    keyboard.Key.cmd: "Cmd", keyboard.Key.cmd_l: "Cmd", keyboard.Key.cmd_r: "Cmd",
}

SPECIAL_NAMES = {
    keyboard.Key.space: "Space", keyboard.Key.enter: "Enter",
    keyboard.Key.backspace: "Backspace", keyboard.Key.tab: "Tab",
    keyboard.Key.esc: "Esc", keyboard.Key.up: "Up", keyboard.Key.down: "Down",
    keyboard.Key.left: "Left", keyboard.Key.right: "Right",
    keyboard.Key.delete: "Delete", keyboard.Key.home: "Home", keyboard.Key.end: "End",
}

# Windows virtual-key codes for the numeric keypad. pynput's `.char` for
# these can be wrong or missing depending on NumLock state and keyboard
# layout, so they're labeled explicitly by vk code instead.
NUMPAD_VK = {
    96: "Num 0", 97: "Num 1", 98: "Num 2", 99: "Num 3", 100: "Num 4",
    101: "Num 5", 102: "Num 6", 103: "Num 7", 104: "Num 8", 105: "Num 9",
    106: "Num *", 107: "Num +", 109: "Num -", 110: "Num .", 111: "Num /",
}


def caps_lock_on():
    """Return True if Caps Lock is currently toggled on (Windows)."""
    try:
        # VK_CAPITAL = 0x14. Low-order bit of GetKeyState is the toggle state.
        return bool(ctypes.WinDLL("user32").GetKeyState(0x14) & 1)
    except Exception:
        return False


def key_to_label(key, shift_held=False):
    if key in MODIFIER_NAMES:
        return MODIFIER_NAMES[key]
    if key in SPECIAL_NAMES:
        return SPECIAL_NAMES[key]
    vk = getattr(key, "vk", None)
    if vk in NUMPAD_VK:
        return NUMPAD_VK[vk]
    char = getattr(key, "char", None)
    if char and len(char) == 1 and ord(char) >= 32:
        if char.isalpha():
            # pynput's .char reflects the real Shift key but not the
            # Caps Lock toggle, so letter case is computed explicitly.
            upper = shift_held != caps_lock_on()  # Shift XOR Caps Lock
            return char.upper() if upper else char.lower()
        return char
    # Holding Ctrl (and some other modifiers) makes Windows send a raw
    # control character instead of the actual letter (e.g. Ctrl+S -> a
    # control code, not "s"). Recover the real key from its virtual-key
    # code instead, which stays stable regardless of modifiers, and use
    # the current Shift state to restore the correct case.
    if vk is not None:
        if 65 <= vk <= 90:  # A-Z
            letter = chr(vk)
            upper = shift_held != caps_lock_on()  # Shift XOR Caps Lock
            return letter if upper else letter.lower()
        if 48 <= vk <= 57:  # 0-9
            return chr(vk)
    name = str(key).replace("Key.", "")
    return name.capitalize()


class InputOverlay:
    def __init__(self):
        self.cfg = load_config()
        self.event_queue = queue.Queue()
        self.active_modifiers = set()
        self.lines = []  # list of (text, timestamp)

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", self.cfg["opacity"])
        except tk.TclError:
            pass
        self.root.configure(bg=self.cfg["bg_color"])
        self.root.geometry(f"+{self.cfg['pos_x']}+{self.cfg['pos_y']}")

        self.label = tk.Label(
            self.root,
            text="Press a key or click...",
            justify="left",
            anchor="w",
            fg=self.cfg["fg_color"],
            bg=self.cfg["bg_color"],
            padx=14,
            pady=10,
        )
        self.label.pack(fill="both", expand=True)
        self.apply_font()

        # Dragging
        self.label.bind("<Button-1>", self.start_drag)
        self.label.bind("<B1-Motion>", self.do_drag)
        self.label.bind("<Button-3>", self.open_settings)  # right-click

        self.settings_win = None

        self.root.after(100, self.process_queue)
        self.root.after(200, self.fade_old_lines)

        self.start_listeners()

    # ---------- font / style ----------
    def apply_font(self):
        f = tkfont.Font(family=self.cfg["font_family"], size=self.cfg["font_size"])
        self.label.configure(font=f)

    # ---------- dragging ----------
    def start_drag(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def do_drag(self, event):
        x = self.root.winfo_pointerx() - self._drag_x
        y = self.root.winfo_pointery() - self._drag_y
        self.root.geometry(f"+{x}+{y}")
        self.cfg["pos_x"], self.cfg["pos_y"] = x, y

    # ---------- listeners (background threads) ----------
    def start_listeners(self):
        self.kb_listener = keyboard.Listener(
            on_press=self.on_key_press, on_release=self.on_key_release
        )
        self.mouse_listener = mouse.Listener(
            on_click=self.on_click, on_scroll=self.on_scroll
        )
        self.kb_listener.start()
        self.mouse_listener.start()

    def on_key_press(self, key):
        if key in MODIFIER_NAMES:
            self.active_modifiers.add(MODIFIER_NAMES[key])
            self.event_queue.put(MODIFIER_NAMES[key])
            return
        label = key_to_label(key, "Shift" in self.active_modifiers)
        if self.active_modifiers:
            label = "+".join(sorted(self.active_modifiers)) + "+" + label
        self.event_queue.put(label)

    def on_key_release(self, key):
        if key in MODIFIER_NAMES:
            self.active_modifiers.discard(MODIFIER_NAMES[key])

    def on_click(self, x, y, button, pressed):
        if pressed:
            name = {"left": "Left Click", "right": "Right Click", "middle": "Middle Click"}.get(
                button.name, str(button)
            )
            if self.active_modifiers:
                name = "+".join(sorted(self.active_modifiers)) + "+" + name
            self.event_queue.put(name)

    def on_scroll(self, x, y, dx, dy):
        self.event_queue.put("Scroll Up" if dy > 0 else "Scroll Down")

    # ---------- UI update loop ----------
    def process_queue(self):
        updated = False
        while not self.event_queue.empty():
            text = self.event_queue.get()
            self.lines.append([text, time.time()])
            updated = True
        if len(self.lines) > self.cfg["max_lines"]:
            self.lines = self.lines[-self.cfg["max_lines"]:]
        if updated:
            self.redraw()
        self.root.after(50, self.process_queue)

    def fade_old_lines(self):
        cutoff = time.time() - self.cfg["fade_seconds"] * self.cfg["max_lines"]
        before = len(self.lines)
        self.lines = [l for l in self.lines if l[1] > cutoff]
        if len(self.lines) != before:
            self.redraw()
        self.root.after(300, self.fade_old_lines)

    def redraw(self):
        text = "\n".join(l[0] for l in self.lines) if self.lines else "..."
        self.label.configure(text=text)

    # ---------- settings panel ----------
    def open_settings(self, event=None):
        if self.settings_win is not None and tk.Toplevel.winfo_exists(self.settings_win):
            self.settings_win.lift()
            return

        win = tk.Toplevel(self.root)
        win.title("Input Overlay Settings")
        win.attributes("-topmost", True)
        win.geometry("300x360")
        self.settings_win = win

        pad = {"padx": 10, "pady": 6}

        # Font family
        ttk.Label(win, text="Font family").pack(anchor="w", **pad)
        families = sorted(set(tkfont.families()))
        font_var = tk.StringVar(value=self.cfg["font_family"])
        font_combo = ttk.Combobox(win, textvariable=font_var, values=families, state="readonly")
        font_combo.pack(fill="x", padx=10)

        # Font size
        ttk.Label(win, text="Font size").pack(anchor="w", **pad)
        size_var = tk.IntVar(value=self.cfg["font_size"])
        size_spin = ttk.Spinbox(win, from_=8, to=96, textvariable=size_var)
        size_spin.pack(fill="x", padx=10)

        # Position
        pos_frame = ttk.Frame(win)
        pos_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(pos_frame, text="X").grid(row=0, column=0)
        x_var = tk.IntVar(value=self.cfg["pos_x"])
        ttk.Entry(pos_frame, textvariable=x_var, width=8).grid(row=0, column=1, padx=5)
        ttk.Label(pos_frame, text="Y").grid(row=0, column=2)
        y_var = tk.IntVar(value=self.cfg["pos_y"])
        ttk.Entry(pos_frame, textvariable=y_var, width=8).grid(row=0, column=3, padx=5)

        # Opacity
        ttk.Label(win, text="Opacity").pack(anchor="w", **pad)
        opacity_var = tk.DoubleVar(value=self.cfg["opacity"])
        ttk.Scale(win, from_=0.2, to=1.0, variable=opacity_var, orient="horizontal").pack(
            fill="x", padx=10
        )

        # Max lines
        ttk.Label(win, text="Lines shown").pack(anchor="w", **pad)
        lines_var = tk.IntVar(value=self.cfg["max_lines"])
        ttk.Spinbox(win, from_=1, to=10, textvariable=lines_var).pack(fill="x", padx=10)

        def apply_settings():
            self.cfg["font_family"] = font_var.get()
            self.cfg["font_size"] = size_var.get()
            self.cfg["pos_x"] = x_var.get()
            self.cfg["pos_y"] = y_var.get()
            self.cfg["opacity"] = opacity_var.get()
            self.cfg["max_lines"] = lines_var.get()

            self.apply_font()
            self.root.geometry(f"+{self.cfg['pos_x']}+{self.cfg['pos_y']}")
            try:
                self.root.attributes("-alpha", self.cfg["opacity"])
            except tk.TclError:
                pass
            save_config(self.cfg)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=10, pady=14)
        ttk.Button(btn_frame, text="Apply", command=apply_settings).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Button(btn_frame, text="Quit App", command=self.quit_app).pack(side="left", expand=True, fill="x", padx=4)

    def quit_app(self):
        try:
            self.kb_listener.stop()
            self.mouse_listener.stop()
        except Exception:
            pass
        save_config(self.cfg)
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.root.mainloop()


if __name__ == "__main__":
    app = InputOverlay()
    app.run()
