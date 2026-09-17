"""Apply the packaged Battlezone icon to Tk, Windows taskbar, and tray code."""
import os
import sys

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "GrizzlyOne95.Battlezone.LobbyMonitor"
        )
    except Exception:
        pass

base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
icon_path = os.path.join(base_path, "branding", "app_icon.png")

# The existing tray implementation looks for bzrmon.ico. Materialize the new
# canonical artwork there in frozen builds so the tray icon also stays branded.
if os.path.exists(icon_path):
    try:
        from PIL import Image
        Image.open(icon_path).save(
            os.path.join(base_path, "bzrmon.ico"),
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    except Exception:
        pass

try:
    import tkinter as tk

    if os.path.exists(icon_path):
        def _wrap_init(cls):
            original = cls.__init__

            def wrapped(self, *args, **kwargs):
                original(self, *args, **kwargs)
                try:
                    image = tk.PhotoImage(file=icon_path)
                    self.iconphoto(True, image)
                    self._battlezone_app_icon = image
                except Exception:
                    pass

            cls.__init__ = wrapped

        _wrap_init(tk.Tk)
        _wrap_init(tk.Toplevel)
except Exception:
    pass
