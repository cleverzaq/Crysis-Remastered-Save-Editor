"""Explorer file drops through TkDND; no Python Win32 window-procedure hooks.

A native ctypes WndProc callback can abort pythonw without a Python exception.
TkDND owns the platform integration; Python only receives a normal Tk event.
"""
from __future__ import annotations

from pathlib import Path
import site
import subprocess
import sys
import traceback

MAX_DROP_FILES = 256
TKDND_REQUIREMENT = "tkinterdnd2>=0.6.2,<0.8"


def runtime_console_python() -> Path:
    """Use the console twin of the *running* pythonw, never an unrelated `py`."""
    executable = Path(sys.executable)
    name = executable.name
    if name.casefold().startswith("pythonw"):
        console = executable.with_name("python" + name[len("pythonw"):])
        if console.is_file():
            return console
    return executable


def install_tkdnd_for_runtime(timeout=180):
    """Install into the interpreter running the GUI; return (ok, diagnostic)."""
    exe = runtime_console_python()
    command = [
        str(exe), "-m", "pip", "install", "--disable-pip-version-check",
        "--no-input", "--upgrade", "--force-reinstall", TKDND_REQUIREMENT,
    ]
    # --user cannot be used in many virtual environments.  In system Python,
    # keep the installation independent of administrator privileges.
    if sys.prefix == sys.base_prefix and site.ENABLE_USER_SITE:
        command.insert(-1, "--user")
    try:
        cp = subprocess.run(command, text=True, capture_output=True,
                            timeout=timeout, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = f"Python: {exe}\nCommand: {' '.join(command)}\nExit: {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
        return cp.returncode == 0, info
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Python: {exe}\nFailed to invoke pip: {exc}"



class DragDropUnavailable(RuntimeError):
    """TkDND is not installed or the current Tk interpreter cannot load it."""


def create_drop_zone(master, **kwargs):
    """Return a visible, reliable drop target or a passive label without TkDND."""
    from tkinter import ttk

    try:
        from tkinterdnd2 import TkinterDnD
    except ImportError:
        return ttk.Label(master, **kwargs)

    class DropZone(ttk.Label, TkinterDnD.DnDWrapper):
        pass

    return DropZone(master, **kwargs)


class WindowsDropTarget:
    """Backwards-compatible facade around the maintained TkDND event API."""

    def __init__(self, root, on_files, widgets=(), on_activity=None, register_content=False):
        self.root = root
        self.on_files = on_files
        self.widgets = tuple(widgets)
        self.on_activity = on_activity
        self.register_content = register_content
        self._registered = []
        self._pending = False
        self._closed = False

    def _drop_surfaces(self):
        """Include central editor panels, not only a strip hidden below the tabs."""
        yield self.root
        yield from self.widgets
        if not self.register_content or not hasattr(self.root, "winfo_children"):
            return
        # Register through TkDnD itself (never subclass HWNDs using ctypes).
        # Skip menus/buttons/inputs: the large editor areas are the useful targets.
        supported = {"TFrame", "TLabelframe", "TNotebook", "Treeview",
                     "Text", "Listbox", "TPanedwindow", "TLabel"}
        pending = list(self.root.winfo_children())
        while pending:
            widget = pending.pop()
            try:
                pending.extend(widget.winfo_children())
                if widget.winfo_class() in supported:
                    yield widget
            except Exception:
                continue

    def install(self) -> int:
        try:
            from tkinterdnd2 import DND_FILES
        except ImportError as exc:
            raise DragDropUnavailable(
                "TkDND is not installed in this Python runtime. Click Enable Drag & Drop and restart the editor."
            ) from exc

        if not hasattr(self.root, "drop_target_register"):
            detail = getattr(self.root, "_crysis_dnd_startup_error", None)
            raise DragDropUnavailable(
                "The window was opened without TkDND. "
                + (f"Startup detail: {detail}" if detail else
                   "Use Enable Drag & Drop and restart the editor.")
            )

        required = (self.root, *self.widgets)
        seen = set()
        for widget in self._drop_surfaces():
            if id(widget) in seen:
                continue
            seen.add(id(widget))
            if not (hasattr(widget, "drop_target_register") and hasattr(widget, "dnd_bind")):
                continue
            try:
                widget.drop_target_register(DND_FILES)
                self._registered.append(widget)
                widget.dnd_bind("<<DropEnter>>", self._on_enter)
                widget.dnd_bind("<<DropPosition>>", self._on_position)
                widget.dnd_bind("<<DropLeave>>", self._on_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception as exc:
                if any(widget is needed for needed in required):
                    self.close()
                    raise DragDropUnavailable(f"Could not activate TkDND: {exc}") from exc
                self._write_error()

        if not self._registered:
            raise DragDropUnavailable("No TkDND-compatible drop surface is available.")
        return len(self._registered)

    def _notify(self, state):
        if self.on_activity is not None and not self._closed:
            try:
                self.on_activity(state)
            except Exception:
                self._write_error()

    def _on_enter(self, event):
        self._notify("enter")
        return "copy"

    def _on_position(self, event):
        return "copy"

    def _on_leave(self, event):
        self._notify("leave")
        return "copy"

    def _on_drop(self, event):
        """Tcl dispatches this callback on the Tk event loop, not a Win32 hook."""
        if self._closed or self._pending:
            return "copy"
        try:
            paths = self.root.tk.splitlist(event.data)
            if len(paths) > MAX_DROP_FILES:
                raise ValueError(f"Too many dropped files ({len(paths)}); limit {MAX_DROP_FILES}.")
            if paths:
                self._notify("received")
                self._pending = True
                self.root.after(25, self._deliver, tuple(paths))
        except Exception:
            self._write_error()
        return "copy"

    def _deliver(self, paths):
        try:
            if not self._closed:
                self.on_files(paths)
        except Exception:
            self._write_error()
            self._notify("error")
        finally:
            self._pending = False
            self._notify("leave")

    def close(self):
        self._closed = True
        self._pending = False
        for widget in self._registered:
            try:
                for event in ("<<DropEnter>>", "<<DropPosition>>", "<<DropLeave>>", "<<Drop>>"):
                    widget.dnd_bind(event, "")
                widget.drop_target_unregister()
            except Exception:
                # Tk may have destroyed the widget while shutting down.
                pass
        self._registered.clear()

    @staticmethod
    def _write_error():
        try:
            log = Path(__file__).resolve().parent / "logs" / "editor.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write("\n=== TkDND drag and drop error ===\n")
                traceback.print_exc(file=stream)
        except OSError:
            pass
