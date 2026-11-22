#!/usr/bin/env python3
"""Crysis Save Editor - desktop GUI for Crysis Remastered save files."""
from __future__ import annotations

import csv
import copy
import configparser
import ctypes
import os
import subprocess
import sys
import queue
import shutil
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path
import xml.etree.ElementTree as ET

from crysis_remastered_decrypt import (
    extract_cipher, save_key_backup, KEY_BACKUP_EXTENSION,
    CRYSIS1_MAGIC, CRYSIS2_MAGIC, CRYSIS3_MAGIC,
)
from game_profiles import GAME_PROFILES, identify_game, profile_fields, validate_value, locate_profile
from xml_tree_search import find_xml_nodes
from windows_drop import (
    WindowsDropTarget, create_drop_zone, install_tkdnd_for_runtime,
    runtime_console_python,
)
from savegame_paths import (
    resolve_savegame_dir, find_save_file, is_profile_directory, game_from_path,
    profile_name_from_path, save_file_candidates, classify_save_filename,
)
from crysis3_gui import Crysis3ProgressMixin
from editor_safety import document_is_dirty, export_slot_backup
from campaign_progress import (
    LEVEL_IDS, read_campaign_progress, apply_campaign_values, restore_campaign_values,
    next_save_id_for,
    split_save_filename, format_save_filename, load_exported_profile,
)
from save_editor_core import (
    SaveDocument,
    SaveEditorError,
    batch_decrypt_folder,
    create_backup_file,
    list_backup_files,
    export_csv,
    export_json,
    find_inventory_context, parse_ltl_inventory_snapshot,
    find_player_stats_context,
    import_xml_into_document,
    iter_attribute_rows,
    open_encrypted_save,
    save_over_encrypted_file,
    restore_backup_file,
    serialize_xml,
)

APP_VERSION = "1.0.0"
MAX_RECENT_FILES = 12
MAX_RECENT_EXES = 8


class CrysisSaveEditor(Crysis3ProgressMixin):
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Crysis Save Editor v{APP_VERSION}")
        self.root.geometry("1480x940")
        self.root.minsize(1120, 740)
        self.root.option_add("*Font", ("Segoe UI", 10))

        self.settings_path = Path(__file__).resolve().with_name("settings.ini")
        self.settings = configparser.ConfigParser(interpolation=None)
        self._load_settings()
        saved_geometry = self.settings.get("General", "window_geometry", fallback="").strip()
        if saved_geometry:
            try:
                self.root.geometry(saved_geometry)
            except tk.TclError:
                pass
        self.theme_var = tk.BooleanVar(value=self.settings.getboolean("General", "dark_mode", fallback=True))
        self.style = ttk.Style(self.root)
        self.apply_theme(persist=False)

        self.document: SaveDocument | None = None
        self.profile_document: SaveDocument | None = None
        self.profile_game: str | None = None
        self.profile_rows: dict[str, tuple[ET.Element, str]] = {}
        self._profile_edit_target: tuple[ET.Element, str] | None = None
        self._profile_edit_iid: str | None = None
        self._campaign_updating = False
        self.exe_path: Path | None = None
        self.last_save_path: Path | None = None
        self.recent_saves = self._read_history("recent_saves")
        self.recent_exes = self._read_history("recent_exes")
        self.tree_node_map: dict[str, ET.Element] = {}
        self.tree_iid_by_node: dict[int, str] = {}
        self.attr_editor: tk.Entry | None = None
        self.editable_tables: dict[str, dict[str, tuple[ET.Element, str]]] = {}
        self.batch_queue: queue.Queue = queue.Queue()
        self.memory_documents: dict[Path, SaveDocument] = {}
        self.memory_visible_paths: list[Path] = []
        self.memory_preview: dict[Path, list[tuple[str, str, str]]] = {}
        self.memory_dirty_paths: set[Path] = set()
        self.memory_task_active = False
        self.batch_export_active = False
        self.memory_loading_active = False
        self.memory_cancel = threading.Event()
        self._memory_tab_states = []
        self.memory_limit_bytes = 512 * 1024 * 1024

        self.status_var = tk.StringVar(value="Select the game EXE, then open an encrypted save.")
        self.file_var = tk.StringVar(value="No file open")
        self.exe_var = tk.StringVar(value="EXE: not selected")
        self.batch_input_var = tk.StringVar()
        self.batch_output_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.slot_folder_var = tk.StringVar(value=self.settings.get("General", "slot_folder", fallback=""))
        self.slot_recursive_var = tk.BooleanVar(value=True)
        self.slot_tree_paths: dict[str, Path] = {}
        self.slot_meta_cache: dict[tuple[str, int, str], dict[str, str]] = {}
        self.backup_tree_paths: dict[str, Path] = {}
        self.player_editor_rows: dict[str, tuple[ET.Element, str]] = {}
        self._quick_search_window: tk.Toplevel | None = None
        self._quick_search_results: dict[str, dict] = {}
        self._drop_target = None
        self._drop_install_in_progress = False
        self._dnd_install_results = queue.Queue()
        self._campaign_next_id_manual = False

        self._restore_last_paths()
        self._build_menu()
        self._build_toolbar()
        self._build_main()
        self._refresh_recent_menus()
        self.refresh_slot_list()
        self._save_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._batch_poll_after_id = self.root.after(100, self._poll_batch_queue)
        self.root.after(300, self._apply_windows_titlebar_theme)
        self.root.after(350, self._restore_layout_positions)
        self.root.after(450, self._install_windows_drop_target)

    def _load_settings(self):
        if self.settings_path.is_file():
            try:
                self.settings.read(self.settings_path, encoding="utf-8")
                return
            except (OSError, configparser.Error):
                pass
        fallback = Path(os.environ.get("APPDATA", Path.home())) / "CrysisSaveEditor" / "settings.ini"
        if fallback.is_file():
            try:
                self.settings.read(fallback, encoding="utf-8")
                self.settings_path = fallback
            except (OSError, configparser.Error):
                pass

    def _read_history(self, key: str) -> list[str]:
        raw = self.settings.get("History", key, fallback="")
        result = []
        for value in raw.splitlines():
            value = value.strip()
            if value and value not in result:
                result.append(value)
        return result

    def _save_settings(self):
        if not self.settings.has_section("General"):
            self.settings.add_section("General")
        if not self.settings.has_section("History"):
            self.settings.add_section("History")
        self.settings.set("General", "dark_mode", "true" if self.theme_var.get() else "false")
        self.settings.set("General", "game_exe", str(self.exe_path) if self.exe_path else "")
        self.settings.set("General", "last_save", str(self.last_save_path) if self.last_save_path else "")
        self.settings.set("General", "slot_folder", self.slot_folder_var.get().strip() if hasattr(self, "slot_folder_var") else "")
        if hasattr(self, "root"):
            try:
                if self.root.state() != "zoomed":
                    self.settings.set("General", "window_geometry", self.root.geometry())
                if hasattr(self, "workspace_pane"):
                    self.settings.set("General", "main_sash_position", str(self.workspace_pane.sashpos(0)))
                if hasattr(self, "xml_pane"):
                    self.settings.set("General", "xml_sash_position", str(self.xml_pane.sashpos(0)))
            except (tk.TclError, IndexError):
                pass
        self.settings.set("History", "recent_saves", "\n".join(self.recent_saves[:MAX_RECENT_FILES]))
        self.settings.set("History", "recent_exes", "\n".join(self.recent_exes[:MAX_RECENT_EXES]))
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.settings_path.with_suffix(".ini.tmp")
            with temp_path.open("w", encoding="utf-8") as stream:
                self.settings.write(stream)
            os.replace(temp_path, self.settings_path)
        except OSError:
            fallback = Path(os.environ.get("APPDATA", Path.home())) / "CrysisSaveEditor" / "settings.ini"
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with fallback.open("w", encoding="utf-8") as stream:
                    self.settings.write(stream)
                self.settings_path = fallback
            except OSError:
                pass

    def _restore_last_paths(self):
        saved_exe = self.settings.get("General", "game_exe", fallback="").strip()
        saved_file = self.settings.get("General", "last_save", fallback="").strip()
        if saved_exe:
            candidate = Path(saved_exe).expanduser()
            if candidate.is_file():
                try:
                    extract_cipher(candidate)
                    self.exe_path = candidate.resolve()
                    self.exe_var.set(f"Game Key: {self.exe_path}")
                    self.status_var.set("Restored the previously selected game EXE. Open an encrypted save.")
                except Exception:
                    self.status_var.set("The previously selected EXE is no longer valid. Select the matching game EXE.")
            else:
                self.status_var.set("The previously selected game EXE was not found. Select it again.")
        if saved_file:
            self.last_save_path = Path(saved_file).expanduser()

    def _remember_exe(self, path: Path):
        value = str(path.resolve())
        self.recent_exes = [value] + [item for item in self.recent_exes if item != value]
        self.recent_exes = self.recent_exes[:MAX_RECENT_EXES]
        self._save_settings()
        self._refresh_recent_menus()

    def _remember_save(self, path: Path):
        value = str(path.resolve())
        self.last_save_path = Path(value)
        self.recent_saves = [value] + [item for item in self.recent_saves if item != value]
        self.recent_saves = self.recent_saves[:MAX_RECENT_FILES]
        self._save_settings()
        self._refresh_recent_menus()

    def _refresh_recent_menus(self):
        if not hasattr(self, "recent_save_menu"):
            return
        self.recent_save_menu.delete(0, "end")
        existing = self.recent_saves
        if not existing:
            self.recent_save_menu.add_command(label="(No recent files)", state="disabled")
        else:
            for index, item in enumerate(existing):
                path = Path(item)
                label = f"{index + 1}. {path.name} — {path.parent}"
                self.recent_save_menu.add_command(label=label, command=lambda p=item: self.open_file(p))
        self.recent_exe_menu.delete(0, "end")
        if not self.recent_exes:
            self.recent_exe_menu.add_command(label="(No recent EXEs)", state="disabled")
        else:
            for index, item in enumerate(self.recent_exes):
                path = Path(item)
                self.recent_exe_menu.add_command(
                    label=f"{index + 1}. {path.name}", command=lambda p=item: self._select_exe_path(p)
                )

    def open_last_save(self):
        if not self.last_save_path:
            messagebox.showinfo("Open Last Save", "There is no previously opened save file.")
            return
        self.open_file(str(self.last_save_path))

    def clear_recent_files(self):
        self.recent_saves.clear()
        self._save_settings()
        self._refresh_recent_menus()

    def _apply_windows_titlebar_theme(self):
        """Request a dark native title bar on supported Windows versions."""
        if sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            hwnd = self.root.winfo_id()
            dwmapi = ctypes.windll.dwmapi

            def set_attribute(attribute: int, value, value_type):
                data = value_type(value)
                dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
                )

            set_attribute(20, 1, ctypes.c_int)
            set_attribute(19, 1, ctypes.c_int)

            def colorref(hex_color: str) -> int:
                value = hex_color.lstrip("#")
                red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
                return red | (green << 8) | (blue << 16)

            set_attribute(35, colorref(self.colors["bg"]), ctypes.c_uint)
            set_attribute(36, colorref(self.colors["fg"]), ctypes.c_uint)
            set_attribute(34, colorref(self.colors["border"]), ctypes.c_uint)
        except Exception:
            pass

    def apply_theme(self, persist=True):
        dark = bool(self.theme_var.get())
        if dark:
            c = {
                "bg": "#0B1120", "surface": "#111B2E", "panel": "#162238",
                "panel_alt": "#1D2B43", "fg": "#E6EDF7", "muted": "#9BAAC0",
                "border": "#2A3A55", "accent": "#35B6A5", "accent_hover": "#279889",
                "selection": "#234D63", "entry": "#0F192A", "warning": "#F5B971",
            }
        else:
            c = {
                "bg": "#EEF2F7", "surface": "#FFFFFF", "panel": "#FFFFFF",
                "panel_alt": "#E5EBF3", "fg": "#1B2638", "muted": "#5B687A",
                "border": "#CBD5E1", "accent": "#087F8C", "accent_hover": "#066B76",
                "selection": "#D2E8EE", "entry": "#FFFFFF", "warning": "#A65A08",
            }
        self.colors = c
        self.root.configure(background=c["bg"])

        # Classic Tk controls (including popup menus and any auxiliary text widgets).
        for option, value in {
            "*Background": c["bg"],
            "*Foreground": c["fg"],
            "*insertBackground": c["fg"],
            "*selectBackground": c["selection"],
            "*selectForeground": c["fg"],
            "*highlightBackground": c["border"],
            "*highlightColor": c["accent"],
            "*troughColor": c["bg"],
            "*Entry.background": c["entry"],
            "*Entry.foreground": c["fg"],
            "*Text.background": c["panel"],
            "*Text.foreground": c["fg"],
            "*Listbox.background": c["panel"],
            "*Listbox.foreground": c["fg"],
            "*Menu.background": c["surface"],
            "*Menu.foreground": c["fg"],
            "*Menu.activeBackground": c["selection"],
            "*Menu.activeForeground": c["fg"],
        }.items():
            self.root.option_add(option, value)

        st = self.style
        available_themes = st.theme_names()
        preferred_theme = "clam" if "clam" in available_themes else "alt"
        try:
            if st.theme_use() != preferred_theme:
                st.theme_use(preferred_theme)
        except tk.TclError:
            pass

        # Base controls.
        st.configure(".", background=c["bg"], foreground=c["fg"],
                     font=("Segoe UI", 10), borderwidth=0, focuscolor=c["accent"])
        st.configure("TFrame", background=c["bg"], relief="flat", borderwidth=0)
        st.configure("Card.TFrame", background=c["surface"], relief="flat", borderwidth=0)
        st.configure("Menubar.TFrame", background=c["surface"], relief="flat", borderwidth=0)
        st.configure("TLabel", background=c["bg"], foreground=c["fg"])
        st.configure("Card.TLabel", background=c["surface"], foreground=c["fg"])
        st.configure("Title.TLabel", background=c["bg"], foreground=c["fg"],
                     font=("Segoe UI Semibold", 17))
        st.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])

        # Buttons and menu buttons share the same shapes, padding and state colors.
        st.configure("TButton", background=c["panel_alt"], foreground=c["fg"],
                     bordercolor=c["border"], lightcolor=c["panel_alt"],
                     darkcolor=c["panel_alt"], padding=(12, 7), relief="flat",
                     borderwidth=0, focusthickness=0, focuscolor=c["accent"])
        st.map("TButton",
               background=[("disabled", c["surface"]), ("pressed", c["accent_hover"]),
                           ("active", c["surface"])],
               foreground=[("disabled", c["muted"]), ("pressed", "#FFFFFF"),
                           ("active", c["fg"])])
        st.configure("Accent.TButton", background=c["accent"], foreground="#FFFFFF",
                     bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                     padding=(14, 8), relief="flat", borderwidth=0,
                     font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton",
               background=[("disabled", c["muted"]), ("pressed", c["accent_hover"]),
                           ("active", c["accent_hover"])],
               foreground=[("disabled", "#FFFFFF"), ("active", "#FFFFFF")])
        st.configure("Menu.TMenubutton", background=c["surface"], foreground=c["fg"],
                     padding=(12, 6), relief="flat", borderwidth=0,
                     lightcolor=c["surface"], darkcolor=c["surface"],
                     focuscolor=c["accent"])
        st.map("Menu.TMenubutton",
               background=[("pressed", c["selection"]), ("active", c["panel_alt"])],
               foreground=[("active", c["fg"])])

        # Inputs, notebook tabs, group boxes and selection controls.
        st.configure("TEntry", fieldbackground=c["entry"], foreground=c["fg"],
                     insertcolor=c["fg"], bordercolor=c["border"], lightcolor=c["border"],
                     darkcolor=c["border"], padding=7, relief="flat", borderwidth=1)
        st.map("TEntry", bordercolor=[("focus", c["accent"])])
        st.configure("TCombobox", fieldbackground=c["entry"], foreground=c["fg"],
                     arrowcolor=c["fg"], bordercolor=c["border"], lightcolor=c["border"],
                     darkcolor=c["border"], padding=6, relief="flat")
        st.map("TCombobox",
               fieldbackground=[("readonly", c["entry"]), ("disabled", c["surface"])],
               foreground=[("readonly", c["fg"]), ("disabled", c["muted"])],
               selectbackground=[("readonly", c["selection"])],
               selectforeground=[("readonly", c["fg"])])
        st.configure("TNotebook", background=c["bg"], borderwidth=0, relief="flat",
                     bordercolor=c["bg"], lightcolor=c["bg"], darkcolor=c["bg"],
                     tabmargins=(2, 6, 2, 0))
        st.configure("TNotebook.Tab", background=c["surface"], foreground=c["muted"],
                     padding=(15, 9), borderwidth=0, relief="flat", bordercolor=c["border"],
                     lightcolor=c["surface"], darkcolor=c["surface"],
                     focuscolor=c["surface"], focusthickness=0)
        st.map("TNotebook.Tab",
               background=[("selected", c["panel_alt"]), ("active", c["surface"])],
               foreground=[("selected", c["fg"]), ("active", c["fg"])],
               expand=[("selected", (1, 1, 1, 0))])
        st.configure("TLabelframe", background=c["bg"], foreground=c["fg"],
                     bordercolor=c["border"], lightcolor=c["border"],
                     darkcolor=c["border"], borderwidth=1, relief="flat")
        st.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent"],
                     font=("Segoe UI Semibold", 10))
        for control_style in ("TCheckbutton", "TRadiobutton"):
            st.configure(control_style, background=c["bg"], foreground=c["fg"],
                         indicatorcolor=c["panel_alt"], padding=(3, 3))
            st.map(control_style, background=[("active", c["bg"])],
                   foreground=[("disabled", c["muted"]), ("active", c["fg"])],
                   indicatorcolor=[("selected", c["accent"]), ("active", c["selection"])])

        # Data grids and their headers use the same surface with subtle separators.
        st.configure("Treeview", background=c["panel"], fieldbackground=c["panel"],
                     foreground=c["fg"], bordercolor=c["panel"], lightcolor=c["panel"],
                     darkcolor=c["panel"], borderwidth=0, relief="flat", padding=(4, 3),
                     rowheight=29, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", background=c["panel_alt"], foreground=c["fg"],
                     bordercolor=c["panel_alt"], lightcolor=c["panel_alt"],
                     darkcolor=c["panel_alt"], borderwidth=0, relief="flat",
                     padding=(9, 8), font=("Segoe UI Semibold", 9))
        # Remove the stock one-pixel bevels that otherwise look bright on dark themes.
        try:
            st.layout("Treeview", [("Treeview.field", {
                "sticky": "nswe", "border": 0,
                "children": [("Treeview.padding", {
                    "sticky": "nswe",
                    "children": [("Treeview.treearea", {"sticky": "nswe"})],
                })],
            })])
        except tk.TclError:
            pass
        st.map("Treeview",
               background=[("selected", c["selection"])],
               foreground=[("selected", c["fg"])])
        st.map("Treeview.Heading",
               background=[("pressed", c["selection"]), ("active", c["surface"])],
               foreground=[("active", c["fg"])])

        # Clam scrollbars are explicitly themed; this avoids the pale Windows defaults.
        for scrollbar_style in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(scrollbar_style, background=c["panel_alt"],
                         troughcolor=c["bg"], bordercolor=c["bg"],
                         lightcolor=c["panel_alt"], darkcolor=c["panel_alt"],
                         arrowcolor=c["muted"], relief="flat", borderwidth=0,
                         arrowsize=13, gripcount=0)
            st.map(scrollbar_style,
                   background=[("pressed", c["accent"]), ("active", c["surface"])],
                   arrowcolor=[("disabled", c["border"])])
        st.configure("TPanedwindow", background=c["bg"], sashthickness=5,
                     sashrelief="flat", sashpad=2)
        st.configure("TSeparator", background=c["border"])

        # Repaint already-created classic widgets and popup menus after theme changes.
        if hasattr(self, "batch_log"):
            self.batch_log.configure(background=c["panel"], foreground=c["fg"],
                                     insertbackground=c["fg"], selectbackground=c["selection"],
                                     selectforeground=c["fg"], relief="flat", bd=0,
                                     highlightthickness=1, highlightbackground=c["border"],
                                     highlightcolor=c["accent"])
        if hasattr(self, "dirty_label"):
            self.dirty_label.configure(foreground=c["warning"])
        if hasattr(self, "menubar"):
            self.menubar.configure(style="Menubar.TFrame")
        if hasattr(self, "menu_objects"):
            for menu in self.menu_objects:
                try:
                    menu.configure(background=c["surface"], foreground=c["fg"],
                                   activebackground=c["selection"], activeforeground=c["fg"],
                                   disabledforeground=c["muted"], borderwidth=0,
                                   activeborderwidth=0, relief="flat", selectcolor=c["accent"],
                                   tearoff=0, font=("Segoe UI", 9))
                except tk.TclError:
                    pass

        self._apply_windows_titlebar_theme()
        if persist and hasattr(self, "settings"):
            self._save_settings()

    def toggle_theme(self):
        self.theme_var.set(not self.theme_var.get())
        self.apply_theme(persist=True)

    def _build_menu(self):
        # Use an in-window menu strip instead of the OS-native menu bar so its
        # colors, font, spacing and hover states match the rest of the interface.
        self.root.config(menu="")
        self.menubar = ttk.Frame(self.root, style="Menubar.TFrame", padding=(8, 2))
        self.menubar.pack(fill="x", side="top")

        file_menu = tk.Menu(self.root, tearoff=False)
        file_menu.add_command(label="Open Encrypted Save…", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Open Last Save", command=self.open_last_save)
        file_menu.add_command(label="Select Game EXE…", command=self.choose_exe)
        file_menu.add_command(label="Use Saved Key…", command=self.choose_key_backup)
        file_menu.add_command(label="Export Key Backup…", command=self.export_key_backup)
        file_menu.add_command(label="Export Keys from EXEs…", command=self.export_multiple_keys)
        self.recent_exe_menu = tk.Menu(file_menu, tearoff=False)
        file_menu.add_cascade(label="Recent Game EXEs", menu=self.recent_exe_menu)
        self.recent_save_menu = tk.Menu(file_menu, tearoff=False)
        file_menu.add_cascade(label="Recent Save Files", menu=self.recent_save_menu)
        file_menu.add_command(label="Clear Recent Save History", command=self.clear_recent_files)
        file_menu.add_separator()
        file_menu.add_command(label="Import XML…", command=self.import_xml)
        file_menu.add_command(label="Export XML…", command=lambda: self.export_data("xml"))
        file_menu.add_command(label="Export JSON…", command=lambda: self.export_data("json"))
        file_menu.add_command(label="Export CSV…", command=lambda: self.export_data("csv"))
        file_menu.add_separator()
        file_menu.add_command(label="Save Encrypted File (Ctrl+S)", command=self.save_file)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)

        view_menu = tk.Menu(self.root, tearoff=False)
        view_menu.add_checkbutton(label="Dark Mode", variable=self.theme_var, command=self.apply_theme)
        view_menu.add_command(label="Find Field in XML Tree (Ctrl+F)", command=self._focus_tree_find)
        view_menu.add_command(label="Reset Layout", command=self.reset_layout)

        help_menu = tk.Menu(self.root, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)

        self.file_menu_button = ttk.Menubutton(
            self.menubar, text="File", style="Menu.TMenubutton", direction="below", width=6
        )
        self.file_menu_button.configure(menu=file_menu)
        self.file_menu_button.pack(side="left")
        self.view_menu_button = ttk.Menubutton(
            self.menubar, text="View", style="Menu.TMenubutton", direction="below", width=6
        )
        self.view_menu_button.configure(menu=view_menu)
        self.view_menu_button.pack(side="left")
        self.help_menu_button = ttk.Menubutton(
            self.menubar, text="Help", style="Menu.TMenubutton", direction="below", width=6
        )
        self.help_menu_button.configure(menu=help_menu)
        self.help_menu_button.pack(side="left")

        self.menu_objects = [file_menu, view_menu, help_menu,
                             self.recent_exe_menu, self.recent_save_menu]
        self.apply_theme(persist=False)
        self.root.bind_all("<Control-o>", lambda _e: self.open_file())
        self.root.bind_all("<Control-s>", lambda _e: self.save_file())
        self.root.bind_all("<Control-k>", lambda _e: self.open_global_search())
        self.root.bind_all("<Control-f>", self._focus_tree_find)
        self.root.bind_all("<F3>", self._tree_find_hotkey_next)
        self.root.bind_all("<Shift-F3>", self._tree_find_hotkey_previous)

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(14, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Open Save…", style="Accent.TButton", command=self.open_file).pack(side="left", padx=(0, 7))
        ttk.Button(bar, text="Open Last", command=self.open_last_save).pack(side="left", padx=5)
        ttk.Button(bar, text="Select Game EXE…", command=self.choose_exe).pack(side="left", padx=5)
        ttk.Button(bar, text="Use Key…", command=self.choose_key_backup).pack(side="left", padx=5)
        ttk.Button(bar, text="Backup Key…", command=self.export_key_backup).pack(side="left", padx=5)
        ttk.Button(bar, text="Save Encrypted File", command=self.save_file).pack(side="left", padx=5)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="Import XML", command=self.import_xml).pack(side="left", padx=6)
        ttk.Button(bar, text="Export XML", command=lambda: self.export_data("xml")).pack(side="left", padx=6)
        ttk.Button(bar, text="JSON", command=lambda: self.export_data("json")).pack(side="left", padx=6)
        ttk.Button(bar, text="CSV", command=lambda: self.export_data("csv")).pack(side="left", padx=6)
        ttk.Button(bar, text="Quick Search  Ctrl+K", command=self.open_global_search).pack(side="left", padx=6)
        self.dirty_label = ttk.Label(bar, text="", foreground="#b45309")
        self.dirty_label.pack(side="right", padx=8)

    def _build_main(self):
        # The workspace uses a draggable split: a persistent navigation/session
        # sidebar and a resizable document area containing the feature tabs.
        self.workspace_pane = ttk.Panedwindow(self.root, orient="horizontal")
        self.workspace_pane.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.sidebar = ttk.Frame(self.workspace_pane, padding=12, width=238)
        self.content_area = ttk.Frame(self.workspace_pane, padding=(2, 0, 0, 0))
        self.workspace_pane.add(self.sidebar, weight=1)
        self.workspace_pane.add(self.content_area, weight=5)
        self.sidebar.pack_propagate(False)

        self.tabs = ttk.Notebook(self.content_area)
        self.tabs.pack(fill="both", expand=True)
        self.tree_tab = ttk.Frame(self.tabs, padding=8)
        self.stats_tab = ttk.Frame(self.tabs, padding=8)
        self.player_advanced_tab = ttk.Frame(self.tabs, padding=8)
        self.profile_tab = ttk.Frame(self.tabs, padding=8)
        self.crysis3_tab = ttk.Frame(self.tabs, padding=8)
        self.inventory_tab = ttk.Frame(self.tabs, padding=8)
        self.diff_tab = ttk.Frame(self.tabs, padding=8)
        self.search_tab = ttk.Frame(self.tabs, padding=8)
        self.slot_tab = ttk.Frame(self.tabs, padding=8)
        self.backup_tab = ttk.Frame(self.tabs, padding=8)
        self.batch_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.tree_tab, text="XML Tree Viewer")
        self.tabs.add(self.stats_tab, text="Player Statistics")
        self.tabs.add(self.player_advanced_tab, text="Advanced Player Editor")
        self.tabs.add(self.profile_tab, text="Game Profile Editor")
        self.tabs.add(self.crysis3_tab, text="Campaign & Nanosuit")
        self.tabs.add(self.inventory_tab, text="Inventory Editor")
        self.tabs.add(self.diff_tab, text="Save Diff Viewer")
        self.tabs.add(self.search_tab, text="Value Search")
        self.tabs.add(self.slot_tab, text="Save Slot Manager")
        self.tabs.add(self.backup_tab, text="Backup Manager")
        self.tabs.add(self.batch_tab, text="Batch / Export")
        self._build_tree_tab()
        self._build_stats_tab()
        self._build_advanced_player_tab()
        self._build_profile_tab()
        self._build_crysis3_features()
        self._build_inventory_tab()
        self._build_diff_tab()
        self._build_search_tab()
        self._build_slot_tab()
        self._build_backup_tab()
        self._build_batch_tab()
        self._build_sidebar()

        drop_strip = ttk.Frame(self.root, padding=(14, 0, 14, 4))
        drop_strip.pack(fill="x")
        self.drop_zone = create_drop_zone(
            drop_strip, text="Drag encrypted CSF / attributes.xml / .meta here",
            anchor="center", style="Muted.TLabel", padding=(10, 9),
        )
        self.drop_zone.pack(side="left", fill="x", expand=True)
        self.dnd_install_button = ttk.Button(
            drop_strip, text="Enable Drag & Drop", command=self._enable_drag_drop
        )
        self.dnd_install_button.pack(side="right", padx=(8, 0))

        status = ttk.Frame(self.root, padding=(16, 8))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, anchor="w", style="Muted.TLabel").pack(fill="x")
        self.workspace_pane.bind("<ButtonRelease-1>", lambda _e: self._save_settings())

    def _build_sidebar(self):
        ttk.Label(self.sidebar, text="CRYSIS SAVE EDITOR", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 12))
        session = ttk.LabelFrame(self.sidebar, text="Current Session", padding=8)
        session.pack(fill="x", pady=(0, 12))
        ttk.Label(session, textvariable=self.file_var, wraplength=198, justify="left", style="Card.TLabel").pack(anchor="w", fill="x")
        ttk.Label(session, textvariable=self.exe_var, wraplength=198, justify="left", style="Muted.TLabel").pack(anchor="w", fill="x", pady=(7, 0))

        ttk.Label(self.sidebar, text="QUICK ACTIONS", font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(2, 6))
        ttk.Button(self.sidebar, text="Global Quick Search  Ctrl+K", command=self.open_global_search).pack(fill="x", pady=3)
        ttk.Button(self.sidebar, text="Open Encrypted Save…", style="Accent.TButton", command=self.open_file).pack(fill="x", pady=3)
        ttk.Button(self.sidebar, text="Save Encrypted File", command=self.save_file).pack(fill="x", pady=3)
        ttk.Button(self.sidebar, text="Open Last Save", command=self.open_last_save).pack(fill="x", pady=3)

        ttk.Separator(self.sidebar).pack(fill="x", pady=12)
        ttk.Label(self.sidebar, text="WORKSPACE", font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(0, 6))
        nav = (
            ("XML Tree Viewer", self.tree_tab),
            ("Player Statistics", self.stats_tab),
            ("Advanced Player Editor", self.player_advanced_tab),
            ("Game Profile Editor", self.profile_tab),
            ("Campaign & Nanosuit", self.crysis3_tab),
            ("Inventory Editor", self.inventory_tab),
            ("Save Diff Viewer", self.diff_tab),
            ("Value Search", self.search_tab),
            ("Save Slot Manager", self.slot_tab),
            ("Backup Manager", self.backup_tab),
            ("Batch / Export", self.batch_tab),
        )
        for label, tab in nav:
            ttk.Button(self.sidebar, text=label, command=lambda target=tab: self.tabs.select(target)).pack(fill="x", pady=2)

        ttk.Separator(self.sidebar).pack(fill="x", pady=12)
        ttk.Label(
            self.sidebar,
            text="Drop a game EXE, .crysiskey, encrypted profile, .CSF, .meta, or save folder here.",
            style="Muted.TLabel", wraplength=198, justify="left",
        ).pack(anchor="w")

    def _restore_layout_positions(self):
        try:
            main_pos = self.settings.getint("General", "main_sash_position", fallback=250)
            self.workspace_pane.sashpos(0, max(190, min(main_pos, 420)))
        except (tk.TclError, ValueError):
            pass
        try:
            xml_pos = self.settings.getint("General", "xml_sash_position", fallback=620)
            self.xml_pane.sashpos(0, max(260, xml_pos))
        except (tk.TclError, ValueError):
            pass

    def reset_layout(self):
        try:
            self.root.geometry("1480x940")
            self.workspace_pane.sashpos(0, 250)
            self.xml_pane.sashpos(0, 620)
        except tk.TclError:
            pass
        self._save_settings()

    def _build_tree_tab(self):
        # Ctrl+F searches the underlying ElementTree, including collapsed nodes.
        find_bar = ttk.Frame(self.tree_tab)
        find_bar.pack(fill="x", pady=(0, 9))
        ttk.Label(find_bar, text="Find field  Ctrl+F:").pack(side="left", padx=(0, 8))
        self.tree_find_var = tk.StringVar()
        self.tree_find_results: list[tuple[ET.Element, str | None]] = []
        self.tree_find_index = -1
        self._tree_find_after_id = None
        self.tree_find_entry = ttk.Entry(find_bar, textvariable=self.tree_find_var, width=44)
        self.tree_find_entry.pack(side="left", fill="x", expand=True, padx=(0, 7))
        self.tree_find_entry.bind("<Return>", lambda _e: self._tree_find_step(1))
        self.tree_find_entry.bind("<Shift-Return>", lambda _e: self._tree_find_step(-1))
        self.tree_find_entry.bind("<Escape>", self._tree_find_clear)
        ttk.Button(find_bar, text="Previous", command=lambda: self._tree_find_step(-1)).pack(side="left", padx=2)
        ttk.Button(find_bar, text="Next", command=lambda: self._tree_find_step(1)).pack(side="left", padx=2)
        ttk.Button(find_bar, text="Clear", command=self._tree_find_clear).pack(side="left", padx=(2, 8))
        self.tree_find_status = tk.StringVar(value="Enter a field name or value")
        ttk.Label(find_bar, textvariable=self.tree_find_status, style="Muted.TLabel", width=20).pack(side="right")
        self.tree_find_var.trace_add("write", self._tree_find_scheduled)
        pane = ttk.Panedwindow(self.tree_tab, orient="horizontal")
        self.xml_pane = pane
        pane.pack(fill="both", expand=True)
        pane.bind("<ButtonRelease-1>", lambda _e: self._save_settings())
        left = ttk.Frame(pane)
        right = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(left, weight=3)
        pane.add(right, weight=2)
        tree_header = ttk.Frame(left)
        tree_header.pack(fill="x", pady=(0, 6))
        ttk.Label(tree_header, text="Save Structure", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(tree_header, text="Expand Selected", command=self._expand_selected_tree_node).pack(side="right", padx=(4, 0))
        ttk.Button(tree_header, text="Collapse Selected", command=self._collapse_selected_tree_node).pack(side="right", padx=(4, 0))
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.xml_tree = ttk.Treeview(tree_frame, columns=("summary",), show="tree headings")
        self.xml_tree.heading("#0", text="Element")
        self.xml_tree.heading("summary", text="Preview")
        self.xml_tree.column("#0", width=270, stretch=True)
        self.xml_tree.column("summary", width=300, stretch=True)
        ybar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.xml_tree.yview)
        xbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.xml_tree.xview)
        self.xml_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.xml_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.xml_tree.bind("<<TreeviewOpen>>", self._tree_opened)
        self.xml_tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.xml_tree.bind("<Double-1>", self._tree_double_click)

        ttk.Label(right, text="Selected Element Attributes", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        self.selected_node_label = ttk.Label(right, text="No element selected", style="Muted.TLabel", wraplength=420)
        self.selected_node_label.pack(anchor="w", fill="x", pady=(0, 6))
        attr_frame = ttk.Frame(right)
        attr_frame.pack(fill="both", expand=True)
        self.attr_tree = ttk.Treeview(attr_frame, columns=("name", "value"), show="headings", height=16)
        self.attr_tree.heading("name", text="Name")
        self.attr_tree.heading("value", text="Value (double-click to edit)")
        self.attr_tree.column("name", width=150, stretch=False)
        self.attr_tree.column("value", width=280, stretch=True)
        self.attr_tree.pack(side="left", fill="both", expand=True)
        attr_scroll = ttk.Scrollbar(attr_frame, orient="vertical", command=self.attr_tree.yview)
        attr_scroll.pack(side="right", fill="y")
        self.attr_tree.configure(yscrollcommand=attr_scroll.set)
        self.attr_tree.bind("<Double-1>", self._edit_attr_tree_cell)
        attr_buttons = ttk.Frame(right)
        attr_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(attr_buttons, text="Add Attribute", command=self.add_attribute).pack(side="left", padx=(0, 4))
        ttk.Button(attr_buttons, text="Remove Attribute", command=self.remove_attribute).pack(side="left", padx=4)
        ttk.Button(attr_buttons, text="Add Child Node", command=self.add_child_node).pack(side="left", padx=4)
        ttk.Button(attr_buttons, text="Remove Node", command=self.remove_selected_node).pack(side="left", padx=4)

    def _focus_tree_find(self, _event=None):
        self.tabs.select(self.tree_tab)
        self.tree_find_entry.focus_set()
        self.tree_find_entry.selection_range(0, "end")
        return "break"

    def _tree_find_hotkey_next(self, _event=None):
        if self.tabs.select() == str(self.tree_tab):
            return self._tree_find_step(1)

    def _tree_find_hotkey_previous(self, _event=None):
        if self.tabs.select() == str(self.tree_tab):
            return self._tree_find_step(-1)

    def _tree_find_scheduled(self, *_):
        if self._tree_find_after_id is not None:
            self.root.after_cancel(self._tree_find_after_id)
        self._tree_find_after_id = self.root.after(120, self._tree_find_update)

    def _tree_find_update(self):
        if self._tree_find_after_id is not None:
            self.root.after_cancel(self._tree_find_after_id)
            self._tree_find_after_id = None
        query = self.tree_find_var.get().strip()
        self.tree_find_results = find_xml_nodes(self.document.root, query) if self.document and query else []
        self.tree_find_index = -1
        if not self.document:
            self.tree_find_status.set("Open a save first")
        elif not query:
            self.tree_find_status.set("Enter a field name or value")
        elif not self.tree_find_results:
            self.tree_find_status.set("No matches")
        else:
            self._tree_find_show_result(0)

    def _tree_find_show_result(self, index: int):
        if not self.document or not self.tree_find_results:
            return
        self.tree_find_index = index % len(self.tree_find_results)
        element, attribute = self.tree_find_results[self.tree_find_index]
        self._reveal_element(element)
        if attribute is not None:
            row = f"attr:{attribute}"
            if self.attr_tree.exists(row):
                self.attr_tree.selection_set(row)
                self.attr_tree.focus(row)
                self.attr_tree.see(row)
        self.tree_find_status.set(f"{self.tree_find_index + 1:,} / {len(self.tree_find_results):,} matches")

    def _tree_find_step(self, direction: int):
        if self._tree_find_after_id is not None:
            self._tree_find_update()
            return "break"
        if self.tree_find_results:
            self._tree_find_show_result(self.tree_find_index + direction)
        elif self.document:
            self._tree_find_update()
        return "break"

    def _tree_find_clear(self, _event=None):
        self.tree_find_var.set("")
        self._tree_find_update()
        self.tree_find_entry.focus_set()
        return "break"

    def _build_stats_tab(self):
        ttk.Label(self.stats_tab, text="Detected player character fields — double-click a value to edit it.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        self.stats_tree = self._make_edit_table(self.stats_tab, ("field", "value"), ("Field", "Value"))

    def _build_advanced_player_tab(self):
        header = ttk.Frame(self.player_advanced_tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(
            header,
            text="Advanced view of detected player-related XML fields. Double-click a value to edit it.",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        controls = ttk.Frame(self.player_advanced_tab)
        controls.pack(fill="x", pady=(8, 8))
        ttk.Label(controls, text="Filter fields:").pack(side="left")
        self.player_advanced_filter = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.player_advanced_filter, width=32)
        entry.pack(side="left", padx=7)
        self.player_advanced_filter.trace_add("write", lambda *_: self._refresh_advanced_player_editor())
        ttk.Button(controls, text="Set Value…", command=self.set_selected_player_value).pack(side="left", padx=4)
        ttk.Button(controls, text="Increase", command=lambda: self.adjust_selected_player_value(1)).pack(side="left", padx=4)
        ttk.Button(controls, text="Decrease", command=lambda: self.adjust_selected_player_value(-1)).pack(side="left", padx=4)
        ttk.Button(controls, text="Health = Max Health", command=self.match_health_to_max).pack(side="left", padx=4)
        self.player_advanced_count = ttk.Label(controls, text="0 fields", style="Muted.TLabel")
        self.player_advanced_count.pack(side="right")

        frame = ttk.Frame(self.player_advanced_tab)
        frame.pack(fill="both", expand=True)
        self.player_advanced_tree = ttk.Treeview(
            frame, columns=("group", "attribute", "value", "type"), show="headings"
        )
        for col, label, width in (
            ("group", "Player Component", 190),
            ("attribute", "Attribute", 220),
            ("value", "Value (double-click to edit)", 360),
            ("type", "Data Type", 100),
        ):
            self.player_advanced_tree.heading(col, text=label)
            self.player_advanced_tree.column(col, width=width, stretch=(col == "value"))
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.player_advanced_tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=self.player_advanced_tree.xview)
        self.player_advanced_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.player_advanced_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.player_advanced_tree.bind("<Double-1>", self._edit_advanced_player_cell)

    def _player_value_type(self, value: str) -> str:
        if "," in value:
            parts = [part.strip() for part in value.split(",")]
            try:
                for part in parts:
                    float(part)
                return f"Vector ({len(parts)}D)"
            except ValueError:
                return "Text"
        try:
            float(value)
            return "Numeric"
        except ValueError:
            return "Text"

    def _refresh_advanced_player_editor(self):
        if not hasattr(self, "player_advanced_tree"):
            return
        tree = self.player_advanced_tree
        tree.delete(*tree.get_children())
        self.player_editor_rows.clear()
        if not self.document:
            self.player_advanced_count.configure(text="0 fields")
            return
        query = self.player_advanced_filter.get().strip().casefold() if hasattr(self, "player_advanced_filter") else ""
        _actor, related = find_player_stats_context(self.document.root)
        count = 0
        for element, name, value in iter_attribute_rows(related):
            group = element.tag
            if query and query not in group.casefold() and query not in name.casefold() and query not in value.casefold():
                continue
            iid = tree.insert("", "end", values=(group, name, value, self._player_value_type(value)))
            self.player_editor_rows[iid] = (element, name)
            count += 1
        if not count and not query and not related:
            tree.insert("", "end", values=("No player actor detected", "Open a .CSF save", "", ""))
        self.player_advanced_count.configure(text=f"{count:,} fields")

    def _edit_advanced_player_cell(self, event):
        if not self.document:
            return
        row = self.player_advanced_tree.identify_row(event.y)
        col = self.player_advanced_tree.identify_column(event.x)
        if not row or col != "#3":
            return
        box = self.player_advanced_tree.bbox(row, col)
        if not box:
            return
        x, y, width, height = box
        value = self.player_advanced_tree.set(row, "value")
        if hasattr(self, "player_advanced_editor") and self.player_advanced_editor:
            self.player_advanced_editor.destroy()
        self.player_advanced_editor = ttk.Entry(self.player_advanced_tree)
        self.player_advanced_editor.insert(0, value)
        self.player_advanced_editor.place(x=x, y=y, width=width, height=height)
        self.player_advanced_editor.focus_set()
        self.player_advanced_editor.select_range(0, "end")
        self.player_advanced_editor.bind("<Return>", lambda _e: self._commit_advanced_player_edit(row))
        self.player_advanced_editor.bind("<FocusOut>", lambda _e: self._commit_advanced_player_edit(row))
        self.player_advanced_editor.bind("<Escape>", self._cancel_advanced_player_edit)

    def _cancel_advanced_player_edit(self, _event=None):
        editor = getattr(self, "player_advanced_editor", None)
        self.player_advanced_editor = None
        if editor is not None:
            try:
                editor.destroy()
            except tk.TclError:
                pass

    def _commit_advanced_player_edit(self, iid: str):
        editor = getattr(self, "player_advanced_editor", None)
        if editor is None:
            return
        self.player_advanced_editor = None
        try:
            value = editor.get()
            editor.destroy()
        except tk.TclError:
            return
        target = self.player_editor_rows.get(iid)
        if target is None:
            return
        element, name = target
        element.set(name, value)
        self._refresh_advanced_player_editor()
        self._refresh_stats()
        self._mark_dirty()

    def _selected_player_editor_target(self):
        selection = self.player_advanced_tree.selection() if hasattr(self, "player_advanced_tree") else ()
        return self.player_editor_rows.get(selection[0]) if selection else None

    def set_selected_player_value(self):
        target = self._selected_player_editor_target()
        if target is None:
            messagebox.showinfo("Player Editor", "Select a player attribute first.")
            return
        element, name = target
        current = element.attrib.get(name, "")
        value = simpledialog.askstring(
            "Set Player Value", f"New value for {element.tag}.{name}:", initialvalue=current, parent=self.root
        )
        if value is None:
            return
        element.set(name, value)
        self._refresh_advanced_player_editor()
        self._refresh_stats()
        self._mark_dirty()

    def adjust_selected_player_value(self, direction: int):
        target = self._selected_player_editor_target()
        if target is None:
            messagebox.showinfo("Player Editor", "Select a numeric player attribute first.")
            return
        element, name = target
        current = element.attrib.get(name, "")
        try:
            number = float(current)
        except ValueError:
            messagebox.showwarning("Player Editor", "The selected value is not a single numeric value.")
            return
        amount = simpledialog.askfloat(
            "Adjust Player Value", "Adjustment amount:", initialvalue=1.0, minvalue=0.000001, parent=self.root
        )
        if amount is None:
            return
        updated = number + (amount * direction)
        element.set(name, str(int(updated)) if current.strip().lstrip("+-").isdigit() and updated.is_integer() else f"{updated:.8g}")
        self._refresh_advanced_player_editor()
        self._refresh_stats()
        self._mark_dirty()

    def match_health_to_max(self):
        if not self._require_document():
            return
        actor, _related = find_player_stats_context(self.document.root)
        if actor is None or "health" not in actor.attrib or "maxHealth" not in actor.attrib:
            messagebox.showinfo("Player Editor", "Health and maxHealth fields were not both found in the player actor.")
            return
        actor.set("health", actor.attrib["maxHealth"])
        self._refresh_advanced_player_editor()
        self._refresh_stats()
        self._mark_dirty()
        self.status_var.set("Player health was set to maxHealth. Save to apply the change to the encrypted file.")

    def _build_slot_tab(self):
        header = ttk.LabelFrame(self.slot_tab, text="Save Slot Folder", padding=10)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(header, textvariable=self.slot_folder_var).grid(row=0, column=1, sticky="ew", padx=7)
        ttk.Button(header, text="Browse…", command=self.choose_slot_folder).grid(row=0, column=2)
        ttk.Checkbutton(header, text="Include subfolders", variable=self.slot_recursive_var, command=self.refresh_slot_list).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Button(header, text="Refresh", command=self.refresh_slot_list).grid(row=1, column=2, sticky="e", pady=(6, 0))
        header.columnconfigure(1, weight=1)

        frame = ttk.Frame(self.slot_tab)
        frame.pack(fill="both", expand=True)
        columns = ("game", "kind", "level", "checkpoint", "difficulty", "playtime", "size", "modified")
        self.slot_tree = ttk.Treeview(frame, columns=columns, show="tree headings")
        headings = {
            "#0": ("Save Slot", 260, True),
            "game": ("Game", 155, False),
            "kind": ("Type", 95, False),
            "level": ("Level", 145, True),
            "checkpoint": ("Checkpoint", 170, True),
            "difficulty": ("Difficulty", 90, False),
            "playtime": ("Play Time", 100, False),
            "size": ("Size", 95, False),
            "modified": ("Saved / Modified", 175, False),
        }
        for col, (title, width, stretch) in headings.items():
            self.slot_tree.heading(col, text=title)
            self.slot_tree.column(col, width=width, stretch=stretch, anchor="w")
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.slot_tree.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=self.slot_tree.xview)
        self.slot_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.slot_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.slot_tree.bind("<Double-1>", lambda _e: self.open_selected_slot())
        actions = ttk.Frame(self.slot_tab)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Open Selected", style="Accent.TButton", command=self.open_selected_slot).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Export Slot Backup Copy…", command=self.duplicate_selected_slot).pack(side="left", padx=6)
        ttk.Button(actions, text="Backup .CSF Only", command=self.backup_selected_slot).pack(side="left", padx=6)
        ttk.Button(actions, text="Open Folder", command=self.open_slot_folder).pack(side="left", padx=6)
        self.slot_count_label = ttk.Label(actions, text="0 save slots", style="Muted.TLabel")
        self.slot_count_label.pack(side="right")

    def _selected_savegame(self) -> str | None:
        """Use the loaded profile, otherwise the game EXE magic or save path."""
        game = getattr(self, 'profile_game', None)
        if game in GAME_PROFILES:
            return game
        if self.exe_path and self.exe_path.is_file():
            try:
                magic, _ = extract_cipher(self.exe_path)
                game = identify_game(magic)
                if game:
                    return game
            except Exception:
                pass
        if self.document:
            return game_from_path(self.document.path)
        return None

    def choose_slot_folder(self):
        initial = self.slot_folder_var.get().strip()
        profile_path = self.profile_document.path if self.profile_document else None
        default = resolve_savegame_dir(profile_path, initial, self._selected_savegame())
        if default is not None:
            initial = str(default)
        elif not initial and self.document:
            initial = str(self.document.path.parent)
        folder = filedialog.askdirectory(title="Select Save Slot Folder", initialdir=initial or None)
        if folder:
            self.slot_folder_var.set(folder)
            self._save_settings()
            self.refresh_slot_list()

    def _game_name_for_save(self, save_path: Path) -> str:
        """Identify the save family from its encrypted file signature."""
        try:
            with save_path.open("rb") as stream:
                marker = stream.read(7)
        except OSError:
            return "Unknown"
        return {
            b"CRYePC1": "Crysis Remastered",
            b"CRYePC2": "Crysis 2 Remastered",
            b"C3KePC2": "Crysis 3 Remastered",
        }.get(marker, "Unknown")

    def _read_slot_metadata(self, save_path: Path) -> dict[str, str]:
        """Decode matching encrypted .meta across Crysis 1, 2 and 3.

        Prefer a user-supplied matching EXE/key. A bundled verified key
        backup may be used for read-only metadata if the chosen EXE belongs to
        a different game. Never try a key whose magic disagrees with the file.
        """
        meta_path = save_path.with_suffix(".meta")
        if not meta_path.is_file():
            return {}
        try:
            with meta_path.open("rb") as stream:
                marker = stream.read(7)
            game = identify_game(marker)
            if game is None:
                return {}
            sources = []
            if self.exe_path is not None and self.exe_path.is_file():
                try:
                    if extract_cipher(self.exe_path)[0] == marker:
                        sources.append(self.exe_path)
                except Exception:
                    pass
            bundled_key = Path(__file__).resolve().parent / "keys" / f"{game}.crysiskey"
            if bundled_key.is_file() and bundled_key not in sources:
                try:
                    if extract_cipher(bundled_key)[0] == marker:
                        sources.append(bundled_key)
                except Exception:
                    pass
            for source in sources:
                cache_key = (str(meta_path.resolve()), meta_path.stat().st_mtime_ns, str(source.resolve()))
                cached = self.slot_meta_cache.get(cache_key)
                if cached is not None:
                    return cached
                try:
                    meta_doc = open_encrypted_save(meta_path, source)
                    if meta_doc.root.tag != 'Metadata':
                        continue
                    values = {str(key): str(value) for key, value in meta_doc.root.attrib.items()}
                    for node in meta_doc.root.iter():
                        for key, value in node.attrib.items():
                            values.setdefault(str(key), str(value))
                    self.slot_meta_cache[cache_key] = values
                    return values
                except Exception:
                    # A different game build may have a different cipher key.
                    # Failing to decode metadata must never damage the slot.
                    continue
        except OSError:
            pass
        return {}

    def refresh_slot_list(self):
        if not hasattr(self, "slot_tree"):
            return
        self.slot_tree.delete(*self.slot_tree.get_children())
        self.slot_tree_paths.clear()
        folder_text = self.slot_folder_var.get().strip()
        if not folder_text and self.document:
            folder_text = str(self.document.path.parent)
        folder = Path(folder_text).expanduser() if folder_text else None
        if folder is None or not folder.is_dir():
            self.slot_count_label.configure(text="Select a folder")
            return
        iterator = folder.rglob("*") if self.slot_recursive_var.get() else folder.glob("*")
        files = []
        for path in iterator:
            if not path.is_file() or path.suffix.casefold() != ".csf":
                continue
            lowered = path.name.casefold()
            if ".bak" in lowered or lowered.endswith((".decrypted.csf", ".export.csf")):
                continue
            try:
                stat = path.stat()
                metadata = self._read_slot_metadata(path)
                saved = metadata.get('saveTime', '')
                if saved:
                    try:
                        # XML metadata is UTC in the supplied games. Prefer its
                        # actual save timestamp over copied file modification times.
                        timestamp = datetime.fromisoformat(saved.replace('Z', '+00:00')).timestamp()
                    except (ValueError, OverflowError, OSError):
                        timestamp = stat.st_mtime
                else:
                    timestamp = stat.st_mtime
                files.append((timestamp, path, stat, metadata, saved))
            except OSError:
                continue
        files.sort(key=lambda row: row[0], reverse=True)
        for index, (_when, path, stat, metadata, saved) in enumerate(files):
            try:
                relative = str(path.relative_to(folder))
                game_name = self._game_name_for_save(path)
                level = metadata.get("level", "")
                checkpoint = metadata.get("checkPointName", metadata.get("checkpoint", ""))
                difficulty = (metadata.get("sp_difficulty") or metadata.get("sp_minimumLevelDifficulty")
                              or metadata.get("sp_difficultylowestplayed") or metadata.get("difficulty", ""))
                playtime = (metadata.get("timePlayed") or metadata.get("sp_gamePlayTime")
                            or metadata.get("sp_levelPlayTime") or metadata.get("playTime", ""))
                # Keep source saveTime unchanged for traceability; if it is
                # missing, show file modification as a clearly marked fallback.
                display_time = saved or (datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S") + " (file)")
                iid = f"slot-{index}"
                self.slot_tree.insert(
                    "", "end", iid=iid, text=relative,
                    values=(game_name, classify_save_filename(path.name), level,
                            checkpoint, difficulty, playtime, f"{stat.st_size:,} B", display_time),
                )
                self.slot_tree_paths[iid] = path
            except OSError:
                continue
        self.slot_count_label.configure(text=f"{len(self.slot_tree_paths):,} save slots (.CSF)")

    def _selected_slot_path(self) -> Path | None:
        selection = self.slot_tree.selection() if hasattr(self, "slot_tree") else ()
        return self.slot_tree_paths.get(selection[0]) if selection else None

    def open_selected_slot(self):
        path = self._selected_slot_path()
        if path is None:
            messagebox.showinfo("Save Slot Manager", "Select a save slot first.")
            return
        self.open_file(path)

    def duplicate_selected_slot(self):
        """Export a restorable copy; never invent a playable '_copy.CSF' slot."""
        source = self._selected_slot_path()
        if source is None:
            messagebox.showinfo("Save Slot Manager", "Select a save slot first.")
            return
        directory = filedialog.askdirectory(
            title="Choose a backup folder OUTSIDE SaveGames (not a playable slot)",
            initialdir=str(source.parent.parent),
        )
        if not directory:
            return
        try:
            copied = export_slot_backup(source, directory)
        except SaveEditorError as exc:
            messagebox.showerror("Export Slot Backup Failed", str(exc))
            return
        self.status_var.set(
            f"Backup copy exported ({len(copied)} files) to {directory}. "
            "This is not a new in-game save slot."
        )
        missing_thumbnail = "\nOptional .bmp thumbnail not present in this slot." if not source.with_suffix(".bmp").is_file() else ""
        open_destination = messagebox.askyesno(
            "Backup Exported",
            f"Copied {len(copied)} files (including .CSF and .meta) with their original names.\n"
            f"Destination:\n{Path(directory).resolve()}\n\n"
            "This is a backup, NOT a new playable save slot."
            + missing_thumbnail + "\n\nOpen destination folder now?",
        )
        if open_destination:
            try:
                self._open_destination_folder(Path(directory))
            except (OSError, RuntimeError) as exc:
                messagebox.showwarning("Unable to Open Folder", str(exc))

    @staticmethod
    def _open_destination_folder(folder: Path):
        """Open the verified backup destination with the OS file manager."""
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise OSError(f"Backup folder no longer exists: {folder}")
        if os.name == "nt":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def backup_selected_slot(self):
        source = self._selected_slot_path()
        if source is None:
            messagebox.showinfo("Save Slot Manager", "Select a save slot first.")
            return
        try:
            backup = create_backup_file(source, label="slot")
        except Exception as exc:
            messagebox.showerror("Create Backup", str(exc))
            return
        self.status_var.set(f"Backup of .CSF only (not sidecar .meta/.bmp): {backup}")
        if self.document and self.document.path == source.resolve():
            self.refresh_backup_list()

    def open_slot_folder(self):
        folder_text = self.slot_folder_var.get().strip()
        if not folder_text and self.document:
            folder_text = str(self.document.path.parent)
        folder = Path(folder_text).expanduser() if folder_text else None
        if folder is None or not folder.is_dir():
            messagebox.showinfo("Save Slot Manager", "Select an existing folder first.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))
            else:
                import webbrowser
                webbrowser.open(folder.as_uri())
        except Exception as exc:
            messagebox.showerror("Open Folder", str(exc))

    def _build_backup_tab(self):
        header = ttk.Frame(self.backup_tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(
            header,
            text="Manage timestamped backups for the currently opened save file.",
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        ttk.Button(header, text="Refresh", command=self.refresh_backup_list).pack(side="right", padx=4)
        ttk.Button(header, text="Backup Current File Only", command=self.create_current_backup).pack(side="right", padx=4)

        self.backup_target_label = ttk.Label(self.backup_tab, text="No save file open", style="Muted.TLabel")
        self.backup_target_label.pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(self.backup_tab)
        frame.pack(fill="both", expand=True)
        self.backup_tree = ttk.Treeview(frame, columns=("created", "size"), show="tree headings")
        self.backup_tree.heading("#0", text="Backup File")
        self.backup_tree.heading("created", text="Created")
        self.backup_tree.heading("size", text="Size")
        self.backup_tree.column("#0", width=570, stretch=True)
        self.backup_tree.column("created", width=180, stretch=False)
        self.backup_tree.column("size", width=110, stretch=False)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self.backup_tree.yview)
        self.backup_tree.configure(yscrollcommand=ybar.set)
        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        bottom = ttk.Frame(self.backup_tab)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Restore Selected Backup…", style="Accent.TButton", command=self.restore_selected_backup).pack(side="left")
        self.backup_count_label = ttk.Label(bottom, text="0 backups", style="Muted.TLabel")
        self.backup_count_label.pack(side="right")

    def refresh_backup_list(self):
        if not hasattr(self, "backup_tree"):
            return
        self.backup_tree.delete(*self.backup_tree.get_children())
        self.backup_tree_paths.clear()
        if not self.document:
            self.backup_target_label.configure(text="No save file open")
            self.backup_count_label.configure(text="0 backups")
            return
        self.backup_target_label.configure(text=f"Backups for: {self.document.path}")
        backups = list_backup_files(self.document.path)
        for index, path in enumerate(backups):
            try:
                stat = path.stat()
                iid = f"backup-{index}"
                self.backup_tree.insert(
                    "", "end", iid=iid, text=path.name,
                    values=(datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), f"{stat.st_size:,} B"),
                )
                self.backup_tree_paths[iid] = path
            except OSError:
                continue
        self.backup_count_label.configure(text=f"{len(self.backup_tree_paths):,} backups")

    def create_current_backup(self):
        if not self._require_document():
            return
        try:
            backup = create_backup_file(self.document.path, label="manual")
        except Exception as exc:
            messagebox.showerror("Create Backup", str(exc))
            return
        self.refresh_backup_list()
        self.status_var.set(f"Manual backup created: {backup}")
        messagebox.showinfo("Backup Created", f"Backup created successfully:\n{backup}")

    def restore_selected_backup(self):
        if not self._require_document():
            return
        selection = self.backup_tree.selection()
        backup = self.backup_tree_paths.get(selection[0]) if selection else None
        if backup is None:
            messagebox.showinfo("Restore Backup", "Select a backup file first.")
            return
        if not self._confirm_discard_changes(save=True, action="restore a backup"):
            return
        if not messagebox.askyesno(
            "Restore Backup",
            f"Restore this backup over the currently opened encrypted save?\n\n{backup}\n\nA pre-restore backup of the current file will be created first.",
        ):
            return
        try:
            restored, rollback_backup = restore_backup_file(backup, self.document.path, self.document.exe_path)
        except Exception as exc:
            messagebox.showerror("Restore Failed", str(exc))
            return
        self.document = restored
        self._refresh_all()
        self.file_var.set(f"File: {restored.path}   |   {restored.kind.upper()}   |   {restored.file_size:,} B | {restored.node_count:,} nodes")
        self.dirty_label.configure(text="Restored")
        self.status_var.set(f"Backup restored. Previous file preserved as: {rollback_backup}")
        messagebox.showinfo("Restore Complete", f"Backup restored successfully.\n\nRollback backup:\n{rollback_backup}")

    def _build_profile_tab(self):
        top = ttk.Frame(self.profile_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Open Profile", command=self.open_profile).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Find Profile", command=self.find_profile).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Save Encrypted Profile", command=self.save_profile).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Refresh", command=self._refresh_profile_editor).pack(side="left")
        self.profile_info = tk.StringVar(value="Select the matching game EXE, then open its encrypted attributes profile.")
        ttk.Label(self.profile_tab, textvariable=self.profile_info, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(0, 8))
        self._build_campaign_panel()
        filters = ttk.Frame(self.profile_tab)
        self.profile_filters = filters
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="Section:").pack(side="left")
        self.profile_category = tk.StringVar(value="All fields")
        self.profile_section_combo = ttk.Combobox(filters, textvariable=self.profile_category, state="readonly", width=25, values=("All fields",))
        self.profile_section_combo.pack(side="left", padx=8)
        self.profile_section_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_profile_editor())
        ttk.Label(filters, text="Filter:").pack(side="left", padx=(10, 0))
        self.profile_filter = tk.StringVar()
        entry = ttk.Entry(filters, textvariable=self.profile_filter)
        entry.pack(side="left", fill="x", expand=True, padx=8)
        entry.bind("<KeyRelease>", lambda _e: self._refresh_profile_editor())
        pane = ttk.Panedwindow(self.profile_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane, padding=12)
        pane.add(left, weight=4)
        pane.add(right, weight=2)
        self.profile_tree = ttk.Treeview(left, columns=("section", "name", "value"), show="headings", selectmode="browse")
        for key, title, width in (("section", "Section", 160), ("name", "Field", 360), ("value", "Value", 170)):
            self.profile_tree.heading(key, text=title)
            self.profile_tree.column(key, width=width, stretch=True)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.profile_tree.yview)
        self.profile_tree.configure(yscrollcommand=scroll.set)
        self.profile_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.profile_tree.bind("<<TreeviewSelect>>", self._profile_select)
        self.profile_tree.bind("<Double-1>", lambda _e: self._profile_apply())
        ttk.Label(right, text="Selected profile field", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 12))
        self.profile_selected_name = tk.StringVar(value="No field selected")
        ttk.Label(right, textvariable=self.profile_selected_name, wraplength=300).pack(anchor="w", pady=(0, 12))
        ttk.Label(right, text="New value:").pack(anchor="w")
        self.profile_new_value = tk.StringVar()
        ttk.Entry(right, textvariable=self.profile_new_value).pack(fill="x", pady=(4, 10))
        ttk.Button(right, text="Apply Value", command=self._profile_apply).pack(fill="x")
        ttk.Label(right, text="Only existing fields are editable. Changes remain in memory until Save Encrypted Profile. Backups are created automatically.", wraplength=300, style="Muted.TLabel").pack(anchor="w", pady=(20, 0))

    def _build_campaign_panel(self):
        frame = ttk.LabelFrame(self.profile_tab, text="Campaign Progress — Crysis 3 Remastered", padding=10)
        self.campaign_frame = frame
        self.campaign_vars = {
            key: tk.StringVar(value="") for key in (
                "last_completed", "current_mission", "level", "last_id", "next_id", "filename", "difficulty",
            )
        }
        fields = (
            ("Last Completed Mission (inferred)", "last_completed", 0, 0, True),
            ("Current Mission (from last save)", "current_mission", 0, 1, True),
            ("Current Level ID", "level", 1, 0, False),
            ("Last Save ID", "last_id", 1, 1, False),
            ("Difficulty", "difficulty", 2, 0, False),
            ("Next Save ID (SaveRunningID)", "next_id", 2, 1, False),
        )
        for col in range(2):
            frame.columnconfigure(col, weight=1)
        for label, key, row, col, readonly in fields:
            container = ttk.Frame(frame)
            container.grid(row=row, column=col, sticky="ew", padx=(0, 14) if col == 0 else (0, 0), pady=(0, 6))
            ttk.Label(container, text=label, style="Muted.TLabel").pack(anchor="w")
            if readonly:
                ttk.Label(container, textvariable=self.campaign_vars[key], font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(4, 1))
            elif key == "level":
                ttk.Combobox(container, textvariable=self.campaign_vars[key], values=(*LEVEL_IDS, "River"), width=25).pack(fill="x", pady=(4, 0))
            elif key == "next_id":
                next_controls = ttk.Frame(container)
                next_controls.pack(fill="x", pady=(4, 0))
                ttk.Entry(next_controls, textvariable=self.campaign_vars[key]).pack(side="left", fill="x", expand=True)
                ttk.Button(next_controls, text="Last + 1", command=self._auto_campaign_next_id).pack(side="left", padx=(8, 0))
            else:
                ttk.Entry(container, textvariable=self.campaign_vars[key]).pack(fill="x", pady=(4, 0))
        ttk.Label(frame, text="Last Save File", style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Entry(frame, textvariable=self.campaign_vars["filename"]).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(4, 8)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(buttons, text="Apply Campaign Values", command=self._apply_campaign_panel,
                   style="Accent.TButton").pack(side="left", padx=(0, 7))
        ttk.Button(buttons, text="Restore from Exported XML…", command=self._restore_campaign_panel).pack(side="left", padx=(0, 7))
        ttk.Button(buttons, text="Check .CSF", command=self._check_campaign_csf).pack(side="left", padx=(0, 7))
        ttk.Button(buttons, text="SaveGames Folder…", command=self.choose_slot_folder).pack(side="left")
        self.campaign_hint = tk.StringVar(value="Open an encrypted Crysis 3 Remastered profile to display campaign progress.")
        ttk.Label(frame, textvariable=self.campaign_hint, style="Muted.TLabel", wraplength=930).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        self.campaign_vars["filename"].trace_add("write", self._campaign_filename_changed)
        self.campaign_vars["last_id"].trace_add("write", self._campaign_parts_changed)
        self.campaign_vars["level"].trace_add("write", self._campaign_parts_changed)
        self.campaign_vars["next_id"].trace_add("write", self._campaign_next_id_changed)
        frame.pack(fill="x", pady=(0, 8))

    def _campaign_next_id_changed(self, *_):
        if not self._campaign_updating:
            self._campaign_next_id_manual = True

    def _auto_campaign_next_id(self):
        """Opt back into ID + 1 after a manual SaveRunningID override."""
        self._campaign_next_id_manual = False
        self._sync_campaign_next_id()

    def _sync_campaign_next_id(self):
        if self._campaign_next_id_manual:
            return
        self._campaign_updating = True
        try:
            try:
                value = str(next_save_id_for(self.campaign_vars["last_id"].get()))
            except ValueError:
                value = ""
            self.campaign_vars["next_id"].set(value)
        finally:
            self._campaign_updating = False

    def _campaign_filename_changed(self, *_):
        if self._campaign_updating:
            return
        parsed = split_save_filename(self.campaign_vars["filename"].get().strip())
        if parsed is None:
            self._sync_campaign_next_id()
            return
        self._campaign_updating = True
        try:
            self.campaign_vars["last_id"].set(str(parsed[0]))
            self.campaign_vars["level"].set(parsed[1])
        finally:
            self._campaign_updating = False
        self._sync_campaign_next_id()

    def _campaign_parts_changed(self, *_):
        if self._campaign_updating:
            return
        self._sync_campaign_next_id()
        parsed = split_save_filename(self.campaign_vars["filename"].get().strip())
        if parsed is None:
            return
        try:
            number = int(self.campaign_vars["last_id"].get())
            filename = format_save_filename(number, self.campaign_vars["level"].get().strip(), parsed[2])
        except (ValueError, OverflowError):
            return
        self._campaign_updating = True
        try:
            self.campaign_vars["filename"].set(filename)
        finally:
            self._campaign_updating = False

    def _refresh_campaign_panel(self):
        if not hasattr(self, "campaign_frame"):
            return
        doc = self.profile_document
        if doc is None or getattr(self, "profile_game", "") != "Crysis3Remastered":
            # Show the panel so users can find the feature before opening a profile.
            # It will be populated only for a decrypted Crysis 3 Remastered profile.
            self._campaign_updating = True
            try:
                for var in self.campaign_vars.values():
                    var.set("")
            finally:
                self._campaign_updating = False
            self.campaign_hint.set(
                "Open an encrypted Crysis 3 Remastered attributes.xml using Open Profile "
                "to populate Campaign Progress. Choose the game EXE or a saved key first."
            )
            return
        # Keep the panel above the filters and the existing Attr field editor.
        if not self.campaign_frame.winfo_manager():
            self.campaign_frame.pack(fill="x", pady=(0, 8), before=self.profile_filters)
        try:
            snapshot = read_campaign_progress(doc.root)
        except ValueError as exc:
            self.campaign_hint.set(f"Could not read campaign data: {exc}")
            return
        values = {
            "last_completed": snapshot.last_completed_mission,
            "current_mission": snapshot.current_mission,
            "level": snapshot.current_level_id,
            "last_id": "" if snapshot.last_save_id is None else str(snapshot.last_save_id),
            "next_id": "" if snapshot.next_save_id is None else str(snapshot.next_save_id),
            "filename": snapshot.last_save_file,
            "difficulty": snapshot.difficulty,
        }
        self._campaign_updating = True
        try:
            for key, value in values.items():
                self.campaign_vars[key].set(value)
            self._campaign_next_id_manual = False
        finally:
            self._campaign_updating = False
        save_folder = resolve_savegame_dir(doc.path, self.slot_folder_var.get().strip(), self.profile_game)
        path_note = f".CSF folder: {save_folder}" if save_folder else ".CSF folder not detected; use SaveGames Folder…"
        self.campaign_hint.set(
            "Last Completed Mission is inferred from campaign progress (not a timestamp). "
            "Next Save ID is editable. Last + 1 restores automatic numbering. "
            f"Changes stay in memory until Save Encrypted Profile. {path_note}"
        )

    def _campaign_inputs(self):
        return dict(
            last_save_file=self.campaign_vars["filename"].get(),
            last_save_id=self.campaign_vars["last_id"].get(),
            current_level_id=self.campaign_vars["level"].get(),
            difficulty=self.campaign_vars["difficulty"].get(),
            next_save_id=self.campaign_vars["next_id"].get(),
        )

    def _campaign_has_pending_inputs(self):
        current = read_campaign_progress(self.profile_document.root)
        fields = self._campaign_inputs()
        return any((
            fields["last_save_file"] != current.last_save_file,
            fields["last_save_id"] != (str(current.last_save_id) if current.last_save_id is not None else ""),
            fields["current_level_id"] != current.current_level_id,
            fields["difficulty"] != current.difficulty,
            fields["next_save_id"] != (str(current.next_save_id) if current.next_save_id is not None else ""),
        ))

    def _confirm_campaign_counter_rewind(self, staged_root):
        current = read_campaign_progress(self.profile_document.root)
        proposed = read_campaign_progress(staged_root)
        old_id, new_id = current.next_save_id, proposed.next_save_id
        last_id = proposed.last_save_id
        if new_id is None:
            return False
        warnings = []
        if old_id is not None and new_id < old_id:
            warnings.append(f"SaveRunningID will decrease from {old_id} to {new_id}.")
        if last_id is not None and new_id <= last_id:
            warnings.append(f"Next Save ID {new_id} is not greater than Last Save ID {last_id}.")
        if warnings:
            return messagebox.askyesno(
                "Potential Save Slot ID Collision",
                "\n".join(warnings)
                + "\n\nThis may reuse an existing .CSF ID and overwrite a newer save. "
                  "Back up SaveGames and verify the intended next ID first.\n\nContinue?",
                icon="warning",
            )
        return True

    def _apply_campaign_panel(self):
        doc = self.profile_document
        if doc is None or getattr(self, "profile_game", "") != "Crysis3Remastered":
            messagebox.showinfo("Campaign Progress", "Open a Crysis 3 Remastered profile first.")
            return
        try:
            staged = copy.deepcopy(doc.root)
            apply_campaign_values(staged, **self._campaign_inputs())
            if not self._confirm_campaign_counter_rewind(staged):
                return
            changed = apply_campaign_values(doc.root, **self._campaign_inputs())
        except ValueError as exc:
            messagebox.showerror("Invalid Campaign Values", str(exc))
            return
        self._refresh_profile_editor()
        self.status_var.set(
            f"Updated {len(changed)} campaign profile fields in memory. "
            "Click Save Encrypted Profile to write the file and create a backup."
            if changed else "Campaign values already match the loaded profile."
        )

    def _restore_campaign_panel(self):
        doc = self.profile_document
        if doc is None or getattr(self, "profile_game", "") != "Crysis3Remastered":
            messagebox.showinfo("Campaign Progress", "Open a Crysis 3 Remastered profile first.")
            return
        path = filedialog.askopenfilename(
            title="Choose a DECRYPTED Attributes XML Export to Restore From",
            initialdir=str(doc.path.parent),
            filetypes=(("Decrypted XML export", "*.xml"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            source = load_exported_profile(path)
            preview = read_campaign_progress(source)
            current = read_campaign_progress(doc.root)
            # Validate on a clone before asking for confirmation: no partial edits.
            test_root = copy.deepcopy(doc.root)
            changed = restore_campaign_values(test_root, source)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Restore Campaign Failed", str(exc))
            return
        if not changed:
            messagebox.showinfo("Campaign Progress", "The selected XML already matches these campaign values.")
            return
        summary = (
            f"SOURCE: {Path(path).name}\n\n"
            f"Last Save File: {current.last_save_file}  →  {preview.last_save_file}\n"
            "Internal SaveRunningID counter will also be restored from the source XML.\n"
            f"Difficulty: {current.difficulty}  →  {preview.difficulty}\n"
            f"Last Completed Mission (inferred): {current.last_completed_mission}  →  {preview.last_completed_mission}\n\n"
            f"Update {len(changed)} existing attributes in MEMORY?\n"
            "Completed mission records will also be restored if present in both XML files.\n"
            "The encrypted file will NOT be changed until you click Save Encrypted Profile (creates a backup)."
        )
        if not messagebox.askyesno("Restore Campaign From XML", summary):
            return
        try:
            restore_campaign_values(doc.root, source)
        except ValueError as exc:
            messagebox.showerror("Restore Campaign Failed", str(exc))
            return
        self._refresh_profile_editor(preserve_campaign_form=False)
        self.status_var.set(f"Restored campaign values from {Path(path).name} in memory ({len(changed)} fields). Save the encrypted profile to persist.")

    def _check_campaign_csf(self):
        doc = self.profile_document
        if doc is None or getattr(self, "profile_game", "") != "Crysis3Remastered":
            return
        filename = self.campaign_vars["filename"].get().strip()
        if not split_save_filename(filename):
            messagebox.showerror("Invalid Save Filename", "The last save filename does not have a valid .CSF format.")
            return
        configured = self.slot_folder_var.get().strip()
        root = resolve_savegame_dir(doc.path, configured, self.profile_game)
        if root is None:
            # The game may be installed elsewhere or Saved Games may be redirected.
            chosen = filedialog.askdirectory(
                title="Select Crysis 3 Remastered SaveGames folder (.CSF files)",
                initialdir=str(doc.path.parent),
            )
            if not chosen:
                return
            root = Path(chosen)
        if str(root) != configured:
            self.slot_folder_var.set(str(root))
            self._save_settings()
            self.refresh_slot_list()
        match = find_save_file(
            root, filename, recursive=self.slot_recursive_var.get(),
            profile_name=profile_name_from_path(doc.path),
        )
        if match:
            self.campaign_hint.set(f"Found last save: {match}")
            messagebox.showinfo("Save File Found", str(match))
        else:
            self.campaign_hint.set(f"Save file not found in SaveGames folder: {root}")
            messagebox.showwarning(
                "Save File Not Found",
                f"None of these filenames was found in:\n{root}\n\n"
                + "\n".join(save_file_candidates(filename, profile_name_from_path(doc.path))) + "\n\n"
                "Open Save Slot Manager > Browse to choose a different SaveGames folder. "
                "The referenced .CSF file may have been deleted or renamed.",
            )

    def find_profile(self):
        if not self.exe_path:
            self.choose_exe()
        if not self.exe_path:
            return
        try:
            magic, _ = extract_cipher(self.exe_path)
            game = identify_game(magic)
            if game is None:
                raise SaveEditorError("Unrecognized game EXE signature.")
            origin = self.document.path if self.document else Path(self.slot_folder_var.get().strip() or self.exe_path.parent)
            profile = locate_profile(origin, game)
            if profile is None:
                messagebox.showinfo("Profile Not Found", f"Could not find {GAME_PROFILES[game]['filename']} near the current save folder. Select it manually.")
                return self.open_profile()
            return self.open_profile(profile)
        except Exception as exc:
            messagebox.showerror("Profile Search Failed", str(exc))

    def _profile_has_pending_raw_value(self) -> bool:
        """Detect a value typed into the side editor but not applied yet."""
        selected = getattr(self, "_profile_edit_target", None)
        if (self.profile_document is None or selected is None
                or not hasattr(self, "profile_new_value")):
            return False
        node, _ = selected
        return self.profile_new_value.get() != node.get("value", "")

    def _profile_has_unsaved_changes(self) -> bool:
        if document_is_dirty(self.profile_document) or self._profile_has_pending_raw_value():
            return True
        if (self.profile_document is not None
                and self.profile_game == "Crysis3Remastered"
                and hasattr(self, "campaign_vars")):
            try:
                return self._campaign_has_pending_inputs()
            except (ValueError, AttributeError, KeyError):
                # Invalid/partially typed form inputs still need protection.
                return True
        return False

    def _document_has_unsaved_changes(self) -> bool:
        return document_is_dirty(self.document) or bool(
            self.document is not None and self.document.path in self.memory_dirty_paths
        )

    def _confirm_discard_changes(self, *, profile=False, save=False, ram=False, action="continue") -> bool:
        pending = []
        if profile and self._profile_has_unsaved_changes():
            pending.append("encrypted game profile")
        if save and self._document_has_unsaved_changes():
            pending.append("opened save file")
        if ram:
            ram_paths = (self._dirty_memory_paths() if hasattr(self, "memory_documents")
                         else getattr(self, "memory_dirty_paths", set()))
            if ram_paths:
                pending.append(f"{len(ram_paths)} RAM document(s)")
        if not pending:
            return True
        return bool(messagebox.askyesno(
            "Unsaved Changes",
            "Unsaved edits exist in: " + ", ".join(pending)
            + f".\n\nDiscard these edits and {action}?",
        ))

    def open_profile(self, path=None):
        if not self.exe_path:
            self.choose_exe()
        if not self.exe_path:
            return
        if path is None:
            path = filedialog.askopenfilename(title="Open Encrypted Game Profile", filetypes=(("Encrypted XML profiles", "*.xml"), ("All files", "*.*")))
        if not path:
            return
        try:
            magic, _ = extract_cipher(self.exe_path)
            game = identify_game(magic)
            if not game:
                raise SaveEditorError("The selected EXE is not a supported Remastered game.")
            path = Path(path).resolve()
            if path.name.lower() != GAME_PROFILES[game]["filename"].lower():
                raise SaveEditorError(f"Expected {GAME_PROFILES[game]['filename']} for {game}; choose the matching profile.")
            if not self._confirm_discard_changes(profile=True, action="open this profile"):
                return
            doc = open_encrypted_save(path, self.exe_path)
            if doc.kind != "xml" or not any(True for _ in profile_fields(doc.root, game)):
                raise SaveEditorError("This file does not contain the expected Attr profile fields.")
            self.profile_document = doc
            self.profile_game = game
            self._profile_edit_target = None
            self._profile_edit_iid = None
            # The encrypted profile lives in Profiles/default, but .CSF saves
            # are normally in the sibling SaveGames directory. Detect it using
            # the actual opened profile; never hard-code C:\Users\<username>.
            old_folder = self.slot_folder_var.get().strip()
            auto_folder = resolve_savegame_dir(doc.path, old_folder, game)
            if auto_folder is not None and str(auto_folder) != old_folder:
                self.slot_folder_var.set(str(auto_folder))
                self._save_settings()
                self.refresh_slot_list()
            self.profile_category.set("All fields")
            self.profile_filter.set("")
            self.profile_section_combo.configure(values=("All fields", *GAME_PROFILES[game]["sections"]))
            self.tabs.select(self.profile_tab)
            self._refresh_profile_editor(preserve_campaign_form=False)
            self.status_var.set(f"Opened {game} profile in memory: {path.name}")
        except Exception as exc:
            messagebox.showerror("Open Profile Failed", str(exc))

    def _refresh_profile_editor(self, *, preserve_campaign_form=True):
        if not hasattr(self, "profile_tree"):
            return
        # Refuse to silently lose a typed-but-unapplied value when the filter
        # rebuilds the Treeview. Applying the value permits refresh immediately.
        if preserve_campaign_form and self._profile_has_pending_raw_value():
            if hasattr(self, "status_var"):
                self.status_var.set("Apply or discard the selected profile value before filtering the list.")
            return
        tree = self.profile_tree
        tree.delete(*tree.get_children())
        self._profile_edit_target = None
        self._profile_edit_iid = None
        self.profile_rows.clear()
        doc = self.profile_document
        if doc is None:
            self._refresh_campaign_panel()
            self._refresh_crysis3_features()
            return
        # Search/filter refreshes and other profile edits must not silently
        # discard values typed into Campaign Progress but not yet applied.
        # New profiles and deliberate restore actions pass preserve=False.
        if (not preserve_campaign_form or self.profile_game != 'Crysis3Remastered'
                or not self._campaign_has_pending_inputs()):
            self._refresh_campaign_panel()
        self._refresh_crysis3_features()
        section_filter = self.profile_category.get()
        query = self.profile_filter.get().strip().casefold()
        count = 0
        for section, name, value, node in profile_fields(doc.root, self.profile_game):
            if section_filter != "All fields" and section != section_filter:
                continue
            if query and query not in name.casefold() and query not in value.casefold():
                continue
            iid = f"profile-{count}"
            tree.insert("", "end", iid=iid, values=(section, name, value))
            self.profile_rows[iid] = (node, name)
            count += 1
        total = sum(1 for _ in profile_fields(doc.root, self.profile_game))
        self.profile_info.set(f"{self.profile_game} | {doc.path} | {count:,} displayed / {total:,} fields | decrypted in memory")

    def _profile_select(self, _event=None):
        selected = self.profile_tree.selection()
        if not selected or selected[0] not in self.profile_rows:
            return
        node, name = self.profile_rows[selected[0]]
        if (getattr(self, "_profile_edit_target", None) is not None
                and node is not self._profile_edit_target[0]
                and self._profile_has_pending_raw_value()):
            if not messagebox.askyesno(
                "Unapplied Profile Value",
                "A changed value has not been applied. Discard it and select another field?",
            ):
                previous = getattr(self, "_profile_edit_iid", None)
                if previous and previous in self.profile_rows:
                    self.profile_tree.selection_set(previous)
                return
        self._profile_edit_target = (node, name)
        self._profile_edit_iid = selected[0]
        self.profile_selected_name.set(name)
        self.profile_new_value.set(node.get("value", ""))

    def _profile_apply(self):
        selected = self.profile_tree.selection()
        if not selected:
            return
        node, name = self.profile_rows[selected[0]]
        old = node.get("value", "")
        try:
            new = validate_value(old, self.profile_new_value.get())
        except ValueError as exc:
            messagebox.showerror("Invalid Profile Value", str(exc))
            return
        if new != old:
            node.set("value", new)
            self._refresh_profile_editor()
            self.status_var.set(f"Updated profile field {name} in memory. Save the profile to persist changes.")

    def save_profile(self):
        doc = self.profile_document
        if doc is None:
            messagebox.showinfo("No Profile", "Open an encrypted game profile first.")
            return
        if self._profile_has_pending_raw_value():
            if not messagebox.askyesno("Unapplied Profile Value", "Apply the typed field value before saving the encrypted profile?"):
                return
            self._profile_apply()
            if self._profile_has_pending_raw_value():
                return  # invalid value; _profile_apply already displayed the error
        if not messagebox.askyesno("Save Encrypted Profile", f"Overwrite {doc.path.name} with the edited encrypted profile? A backup will be created first."):
            return
        if getattr(self, "profile_game", "") == "Crysis3Remastered":
            try:
                # Do not silently discard edits still typed into the Campaign panel.
                if self._campaign_has_pending_inputs():
                    staged = copy.deepcopy(doc.root)
                    apply_campaign_values(staged, **self._campaign_inputs())
                    if not self._confirm_campaign_counter_rewind(staged):
                        return
                    apply_campaign_values(doc.root, **self._campaign_inputs())
            except ValueError as exc:
                messagebox.showerror("Invalid Campaign Values", str(exc))
                return
        try:
            result = save_over_encrypted_file(doc)
            verified = open_encrypted_save(doc.path, doc.exe_path)
            if serialize_xml(verified.root, pretty=False) != serialize_xml(doc.root, pretty=False):
                raise SaveEditorError("Profile verification failed after saving. Restore the backup before playing.")
        except Exception as exc:
            messagebox.showerror("Profile Save Failed", str(exc))
            return
        self._refresh_profile_editor()
        self.status_var.set(f"Profile saved and verified. Backup: {result['backup']}")
        messagebox.showinfo("Profile Saved", f"Encrypted profile saved and verified.\nBackup: {result['backup']}")

    def _build_inventory_tab(self):
        ttk.Label(self.inventory_tab, text="Ammunition, slots, and saved weapon entities associated with the player's inventory.",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        self.inventory_tree = self._make_edit_table(self.inventory_tab, ("field", "value"), ("Field", "Value"))

    def _build_diff_tab(self):
        header = ttk.Frame(self.diff_tab)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="Compare the current XML with the version from when the file was opened or last saved.",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Refresh Diff", command=self.refresh_diff).pack(side="right")
        self.diff_tree = ttk.Treeview(self.diff_tab, columns=("kind", "old", "new"), show="tree headings")
        for col, label in (("#0", "Path"), ("kind", "Change"), ("old", "Before"), ("new", "After")):
            self.diff_tree.heading(col, text=label)
        self.diff_tree.column("#0", width=430)
        self.diff_tree.column("kind", width=120, stretch=False)
        self.diff_tree.column("old", width=250)
        self.diff_tree.column("new", width=250)
        self.diff_tree.pack(fill="both", expand=True)

    def _build_search_tab(self):
        search_bar = ttk.Frame(self.search_tab)
        search_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(search_bar, text="Search element names, attribute names, and values:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_bar, textvariable=self.search_var, width=38)
        search_entry.pack(side="left", padx=8)
        search_entry.bind("<Return>", lambda _e: self.run_value_search())
        ttk.Button(search_bar, text="Search", command=self.run_value_search).pack(side="left")
        self.search_count = ttk.Label(search_bar, text="")
        self.search_count.pack(side="right")
        self.search_tree = ttk.Treeview(self.search_tab, columns=("type", "field", "value"), show="tree headings")
        for col, label in (("#0", "Path"), ("type", "Type"), ("field", "Element / Attribute"), ("value", "Value")):
            self.search_tree.heading(col, text=label)
        self.search_tree.column("#0", width=470)
        self.search_tree.column("type", width=90, stretch=False)
        self.search_tree.column("field", width=220)
        self.search_tree.column("value", width=450)
        self.search_tree.pack(fill="both", expand=True)
        self.search_tree.bind("<Double-1>", self._search_result_open)

    def _build_batch_tab(self):
        top = ttk.LabelFrame(self.batch_tab, text="Load Saves into RAM", padding=10)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Folder containing encrypted saves:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(top, textvariable=self.batch_input_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse…", command=self.choose_batch_input).grid(row=0, column=2)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var).grid(row=1, column=1, sticky="w", pady=4)
        self.memory_button = ttk.Button(top, text="Load Folder into RAM", command=self.load_folder_into_memory)
        self.memory_button.grid(row=2, column=2, sticky="e", pady=4)
        ttk.Label(top, text="Decrypt and parse in memory; no XML files are created (512 MiB limit).",
                  justify="left").grid(row=2, column=0, columnspan=2, sticky="w")
        top.columnconfigure(1, weight=1)

        memory_box = ttk.LabelFrame(self.batch_tab, text="Save Slots in RAM (Ctrl/Shift to select multiple)", padding=8)
        memory_box.pack(fill="both", expand=True, pady=(0, 10))
        self.memory_list = tk.Listbox(memory_box, height=8, exportselection=False, selectmode="extended")
        self.memory_list.pack(side="left", fill="both", expand=True)
        self.memory_list.bind("<Double-1>", lambda _event: self.open_selected_memory_document())
        memory_buttons = ttk.Frame(memory_box)
        memory_buttons.pack(side="left", fill="y", padx=6)
        ttk.Button(memory_buttons, text="Open Selected", command=self.open_selected_memory_document).pack(fill="x", pady=2)
        ttk.Button(memory_buttons, text="Select All", command=lambda: self.memory_list.selection_set(0, "end")).pack(fill="x", pady=2)
        ttk.Button(memory_buttons, text="Clear RAM", command=self.clear_memory_documents).pack(fill="x", pady=2)

        edit_box = ttk.LabelFrame(self.batch_tab, text="Batch Edit Selected Save Slots", padding=8)
        edit_box.pack(fill="x", pady=(0, 10))
        ttk.Label(edit_box, text="Attribute name (exact):").grid(row=0, column=0, sticky="w")
        self.batch_field_var = tk.StringVar(value="health")
        ttk.Entry(edit_box, textvariable=self.batch_field_var, width=28).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(edit_box, text="New value:").grid(row=0, column=2, sticky="w")
        self.batch_value_var = tk.StringVar()
        ttk.Entry(edit_box, textvariable=self.batch_value_var, width=20).grid(row=0, column=3, sticky="ew", padx=5)
        self.preview_button = ttk.Button(edit_box, text="Preview Changes", command=self.preview_memory_batch)
        self.preview_button.grid(row=0, column=4, padx=3)
        self.apply_button = ttk.Button(edit_box, text="Apply to RAM", command=self.apply_memory_batch)
        self.apply_button.grid(row=0, column=5, padx=3)
        self.save_memory_button = ttk.Button(edit_box, text="Save Selected + Backups", command=self.save_selected_memory)
        self.save_memory_button.grid(row=0, column=6, padx=3)
        self.cancel_memory_button = ttk.Button(edit_box, text="Cancel Task", command=self.cancel_memory_task, state="disabled")
        self.cancel_memory_button.grid(row=0, column=7, padx=3)
        edit_box.columnconfigure(1, weight=1)
        edit_box.columnconfigure(3, weight=1)
        self.memory_preview_text = tk.Text(edit_box, height=5, wrap="none", state="disabled")
        self.memory_preview_text.grid(row=1, column=0, columnspan=8, sticky="nsew", pady=(6, 0))
        ttk.Label(edit_box, text="Exact XML attribute names only. Preview before applying; only matching fields change. Save creates .bak backups and verifies encryption.").grid(row=2, column=0, columnspan=8, sticky="w", pady=(5, 0))

        # Disk export is optional and separate from the RAM workflow.
        disk_box = ttk.LabelFrame(self.batch_tab, text="Optional: Export Folder to XML", padding=10)
        disk_box.pack(fill="x", pady=(0, 10))
        ttk.Label(disk_box, text="XML output folder:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(disk_box, textvariable=self.batch_output_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(disk_box, text="Browse…", command=self.choose_batch_output).grid(row=0, column=2)
        self.batch_button = ttk.Button(disk_box, text="Export Folder to XML…", command=self.start_batch)
        self.batch_button.grid(row=1, column=2, sticky="e", pady=4)
        ttk.Label(disk_box, text="Only use this section when you want decrypted XML files on disk.").grid(
            row=1, column=0, columnspan=2, sticky="w")
        disk_box.columnconfigure(1, weight=1)

        export_box = ttk.LabelFrame(self.batch_tab, text="Export / Import", padding=10)
        export_box.pack(fill="x", pady=(0, 10))
        ttk.Label(export_box, text="Export the currently open document to readable XML, JSON (tree), or CSV (attribute rows).\nImporting XML replaces the current tree; encryption is applied when you click Save Encrypted File.",
                  justify="left").pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(export_box)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Export XML…", command=lambda: self.export_data("xml")).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Export JSON…", command=lambda: self.export_data("json")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Export CSV…", command=lambda: self.export_data("csv")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Import XML…", command=self.import_xml).pack(side="left", padx=6)

        log_box = ttk.LabelFrame(self.batch_tab, text="Log", padding=8)
        log_box.pack(fill="both", expand=True)
        self.batch_log = tk.Text(log_box, height=12, wrap="word", state="disabled", bd=0, relief="flat")
        self.batch_log.pack(fill="both", expand=True)
        self.batch_log.configure(background=self.colors["panel"], foreground=self.colors["fg"],
                                 insertbackground=self.colors["fg"], selectbackground=self.colors["selection"])

    def _make_edit_table(self, parent, data_columns, headings):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=data_columns, show="headings")
        for col, heading in zip(data_columns, headings):
            tree.heading(col, text=heading)
            tree.column(col, width=280 if col == "field" else 220, stretch=True)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=ybar.set)
        tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda event, w=tree: self._edit_data_table_cell(event, w))
        return tree

    def choose_key_backup(self):
        path = filedialog.askopenfilename(
            title="Use Saved Game Key", filetypes=(("Crysis Key Backup", "*.crysiskey"), ("All Files", "*.*")),
        )
        if path:
            self._select_exe_path(path)

    def export_key_backup(self):
        if not self.exe_path:
            messagebox.showwarning("No Key Selected", "Select a game EXE or a key backup first.")
            return
        try:
            magic, _ = extract_cipher(self.exe_path)
            suggested = {b"CRYePC1": "CrysisRemastered", b"CRYePC2": "Crysis2Remastered", b"C3KePC2": "Crysis3Remastered"}[magic]
            target = filedialog.asksaveasfilename(
                title="Export Game Key Backup", defaultextension=".crysiskey",
                initialfile=suggested + ".crysiskey", filetypes=(("Crysis Key Backup", "*.crysiskey"),),
            )
            if not target:
                return
            if Path(target).exists():
                messagebox.showwarning("Backup Exists", "This backup already exists. Choose another filename to avoid overwriting it.")
                return
            save_key_backup(self.exe_path, target)
            messagebox.showinfo("Key Backup Created", f"Key saved to:\n{target}\n\nStore this unencrypted key backup securely.")
        except Exception as exc:
            messagebox.showerror("Key Export Failed", str(exc))

    def export_multiple_keys(self):
        paths = filedialog.askopenfilenames(
            title="Select Game EXEs to Back Up", filetypes=(("Executable File", "*.exe"), ("All Files", "*.*")),
        )
        if not paths:
            return
        directory = filedialog.askdirectory(title="Choose Backup Directory")
        if not directory:
            return
        game_names = {b"CRYePC1": "CrysisRemastered", b"CRYePC2": "Crysis2Remastered", b"C3KePC2": "Crysis3Remastered"}
        exported, errors = [], []
        for path in paths:
            try:
                magic, _ = extract_cipher(Path(path))
                base = Path(directory) / (game_names[magic] + ".crysiskey")
                if base.exists():
                    raise FileExistsError(f"Backup already exists: {base.name}")
                save_key_backup(path, base)
                exported.append(base.name)
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        messagebox.showinfo("Key Backup Results", f"Exported {len(exported)} key(s).\n" + "\n".join(exported + errors) + "\n\nKeep backups secure.")

    def choose_exe(self):
        initial_dir = self.exe_path.parent if self.exe_path else None
        path = filedialog.askopenfilename(
            title="Select the game EXE that matches the save files",
            initialdir=str(initial_dir) if initial_dir else None,
            filetypes=(("Game executable / key", "*.exe *.crysiskey"), ("All Files", "*.*")),
        )
        if path:
            self._select_exe_path(path)

    def _select_exe_path(self, path: str | Path):
        if (getattr(self, "batch_export_active", False) or getattr(self, "memory_loading_active", False)
                or getattr(self, "memory_task_active", False)):
            messagebox.showwarning("Task Running", "Finish the current batch or RAM task before changing game keys.")
            return
        candidate = Path(path).expanduser().resolve()
        try:
            marker, key = extract_cipher(candidate)
        except Exception as exc:
            messagebox.showerror("Invalid EXE", str(exc))
            return
        if self.document is not None and (marker != self.document.magic or key != self.document.key):
            messagebox.showwarning(
                "Different Game Key",
                "The open save uses a different key. Save or close the current session before switching games.",
            )
            return
        if (self.profile_document is not None
                and (marker != self.profile_document.magic or key != self.profile_document.key)):
            if not self._confirm_discard_changes(profile=True, action="switch games"):
                return
            # Unload the previous game's profile; never save it using a new key.
            self.profile_document = None
            self.profile_game = None
            self._refresh_profile_editor()
        self.exe_path = candidate
        self.slot_meta_cache.clear()
        if self.document is not None:
            self.document.exe_path = candidate
        self.exe_var.set(f"Game Key: {self.exe_path}")
        self.status_var.set("Game key source selected and saved in settings.ini.")
        game = identify_game(marker)
        if game:
            profile_path = self.profile_document.path if self.profile_document and getattr(self, 'profile_game', None) == game else None
            previous = self.slot_folder_var.get().strip()
            found = resolve_savegame_dir(profile_path, previous, game)
            if found is not None and str(found) != previous:
                self.slot_folder_var.set(str(found))
                self.refresh_slot_list()
        self._remember_exe(candidate)

    def open_file(self, path: str | Path | None = None):
        if self.exe_path is None:
            self.choose_exe()
            if self.exe_path is None:
                return
        if path is None:
            initial_dir = self.last_save_path.parent if self.last_save_path else None
            initial_file = self.last_save_path.name if self.last_save_path else None
            path = filedialog.askopenfilename(
                title="Open Encrypted Save",
                initialdir=str(initial_dir) if initial_dir and initial_dir.exists() else None,
                initialfile=initial_file,
                filetypes=(("Crysis Save Files", "*.CSF *.csf *.meta *.xml"), ("All Files", "*.*")),
            )
        if not path:
            return
        if not self._confirm_discard_changes(save=True, action="open another save"):
            return
        try:
            doc = open_encrypted_save(path, self.exe_path)
        except Exception as exc:
            messagebox.showerror("Failed to Open Save", str(exc))
            return
        self.document = doc
        self._remember_save(doc.path)
        if not self.slot_folder_var.get().strip():
            self.slot_folder_var.set(str(doc.path.parent))
            self._save_settings()
        self._refresh_all()
        details = f"{doc.file_size:,} B | {doc.node_count:,} nodes"
        if doc.decompressed_size is not None:
            details += f" | {doc.decompressed_size:,} B expanded in memory"
        self.file_var.set(f"File: {doc.path}   |   {doc.kind.upper()}   |   {details}")
        self.status_var.set(
            f"Opened {doc.path.name} into memory. "
            + ("Decryption, zlib decompression, and XMLCPB parsing completed in memory. " if doc.kind == "csf" else "Decryption and XML parsing completed in memory. ")
            + "The encrypted source is unchanged until you save."
        )
        self.dirty_label.configure(text="")

    def save_file(self):
        if not self._require_document():
            return
        if not messagebox.askyesno(
            "Save Encrypted File",
            "Saving will overwrite the currently open encrypted file. A .bak backup will be created first.\n\nContinue?",
        ):
            return
        try:
            result = save_over_encrypted_file(self.document)
        except Exception as exc:
            messagebox.showerror("Save Error", f"Failed to save the file. The original should remain unchanged.\n\n{exc}")
            return
        self._refresh_all()
        details = f"{self.document.file_size:,} B | {self.document.node_count:,} nodes"
        if self.document.decompressed_size is not None:
            details += f" | {self.document.decompressed_size:,} B expanded in memory"
        self.file_var.set(f"File: {self.document.path}   |   {self.document.kind.upper()}   |   {details}")
        self.status_var.set(f"Encrypted file saved. Backup: {result['backup']}")
        self.dirty_label.configure(text="Saved")
        # The active document may also live in the RAM collection. The new
        # encrypted file now matches the in-memory state, so clear its flag.
        self.memory_dirty_paths.discard(self.document.path)
        messagebox.showinfo("Save Complete", f"The file was encrypted and saved under the same name.\n\nBackup:\n{result['backup']}")

    def import_xml(self):
        if not self._require_document():
            return
        path = filedialog.askopenfilename(
            title="Import Edited XML",
            filetypes=(("XML Files", "*.xml"), ("All Files", "*.*")),
        )
        if not path:
            return
        try:
            new_root = import_xml_into_document(self.document, path)
        except Exception as exc:
            messagebox.showerror("XML Import Failed", str(exc))
            return
        if not self._confirm_discard_changes(save=True, action="replace the current XML with the import"):
            return
        # RAM-loaded documents omit baseline snapshots to reduce memory use;
        # preserve the original before the replacement so edits remain visible.
        if self.document.baseline_root is None:
            self.document.baseline_root = copy.deepcopy(self.document.root)
        self.document.root = new_root
        self._refresh_all()
        self._mark_dirty()
        self.status_var.set(f"Imported XML: {path}. Save to create the encrypted file.")

    def export_data(self, fmt: str):
        if not self._require_document():
            return
        extension = {"xml": ".xml", "json": ".json", "csv": ".csv"}[fmt]
        initial = self.document.path.stem + ".export" + extension
        path = filedialog.asksaveasfilename(
            title=f"Export {fmt.upper()}",
            initialfile=initial,
            defaultextension=extension,
            filetypes=((f"{fmt.upper()}", f"*{extension}"), ("All Files", "*.*")),
        )
        if not path:
            return
        try:
            if fmt == "xml":
                Path(path).write_bytes(serialize_xml(self.document.root))
            elif fmt == "json":
                export_json(self.document.root, path)
            else:
                export_csv(self.document.root, path)
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc))
            return
        self.status_var.set(f"Exported {fmt.upper()}: {path}")

    def _refresh_all(self):
        self._refresh_xml_tree()
        self._refresh_stats()
        self._refresh_inventory()
        self._refresh_profile_editor()
        self.refresh_diff()
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_count.configure(text="")
        self.refresh_slot_list()
        self.refresh_backup_list()

    def _refresh_xml_tree(self):
        self.xml_tree.delete(*self.xml_tree.get_children())
        self.tree_node_map.clear()
        self.tree_iid_by_node.clear()
        if not self.document:
            return
        root = self.document.root
        iid = self.xml_tree.insert("", "end", text=root.tag, values=(self._node_summary(root),), open=True)
        self.tree_node_map[iid] = root
        self.tree_iid_by_node[id(root)] = iid
        self._populate_tree_children(iid, root)
        self.selected_node_label.configure(text=f"<{root.tag}>")
        self._refresh_attribute_tree(root)
        if hasattr(self, "tree_find_var"):
            self._tree_find_update()

    def _node_summary(self, element: ET.Element) -> str:
        if element.attrib:
            first = next(iter(element.attrib.items()))
            text = f"{first[0]}={first[1]}"
            return text[:100]
        return f"{len(list(element))} child elements" if len(list(element)) else ""

    def _populate_tree_children(self, parent_iid: str, element: ET.Element):
        """Materialize one XML level exactly once, replacing the lazy sentinel.

        The sentinel must be checked *before* it is deleted: querying a deleted
        Treeview item raised TclError and left XML branches permanently empty.
        """
        existing = self.xml_tree.get_children(parent_iid)
        if existing:
            is_placeholder = (
                len(existing) == 1
                and self.xml_tree.item(existing[0], "text") == "__placeholder__"
            )
            if not is_placeholder:
                return
            self.xml_tree.delete(existing[0])
        for child in element:
            child_iid = self.xml_tree.insert(
                parent_iid, "end", text=child.tag, values=(self._node_summary(child),)
            )
            self.tree_node_map[child_iid] = child
            self.tree_iid_by_node[id(child)] = child_iid
            if len(child):
                self.xml_tree.insert(child_iid, "end", text="__placeholder__", values=("",))

    def _expand_tree_node(self, iid: str):
        element = self.tree_node_map.get(iid)
        if element is None or not len(element):
            return
        self._populate_tree_children(iid, element)
        self.xml_tree.item(iid, open=True)

    def _expand_selected_tree_node(self):
        iid = self.xml_tree.focus()
        if iid:
            self._expand_tree_node(iid)

    def _collapse_selected_tree_node(self):
        iid = self.xml_tree.focus()
        if iid:
            self.xml_tree.item(iid, open=False)

    def _tree_double_click(self, event):
        iid = self.xml_tree.identify_row(event.y)
        if not iid or iid not in self.tree_node_map:
            return "break"
        self.xml_tree.focus(iid)
        self.xml_tree.selection_set(iid)
        if self.xml_tree.item(iid, "open"):
            self.xml_tree.item(iid, open=False)
        else:
            self._expand_tree_node(iid)
        # Avoid the default Treeview double-click toggling the same node twice.
        return "break"

    def _tree_opened(self, _event=None):
        iid = self.xml_tree.focus()
        if iid:
            self._expand_tree_node(iid)

    def _tree_selected(self, _event=None):
        if not self.document:
            return
        selected = self.xml_tree.selection()
        if not selected:
            return
        element = self.tree_node_map.get(selected[0])
        if element is None:
            return
        self.selected_node_label.configure(text=f"<{element.tag}>  —  {self._path_for(element)}")
        self._refresh_attribute_tree(element)

    def _refresh_attribute_tree(self, element: ET.Element):
        self.attr_tree.delete(*self.attr_tree.get_children())
        for name, value in element.attrib.items():
            self.attr_tree.insert("", "end", iid=f"attr:{name}", values=(name, value))

    def _edit_attr_tree_cell(self, event):
        if not self.document:
            return
        row = self.attr_tree.identify_row(event.y)
        col = self.attr_tree.identify_column(event.x)
        if not row or col != "#2":
            return
        box = self.attr_tree.bbox(row, col)
        if not box:
            return
        x, y, width, height = box
        value = self.attr_tree.set(row, "value")
        self._destroy_attr_editor()
        self.attr_editor = ttk.Entry(self.attr_tree)
        self.attr_editor.insert(0, value)
        self.attr_editor.place(x=x, y=y, width=width, height=height)
        self.attr_editor.focus_set()
        self.attr_editor.select_range(0, "end")
        self.attr_editor.bind("<Return>", lambda _e: self._commit_attr_tree_edit(row))
        self.attr_editor.bind("<FocusOut>", lambda _e: self._commit_attr_tree_edit(row))
        self.attr_editor.bind("<Escape>", lambda _e: self._destroy_attr_editor())

    def _commit_attr_tree_edit(self, row: str):
        if not self.attr_editor:
            return
        editor = self.attr_editor
        self.attr_editor = None
        try:
            value = editor.get()
            editor.destroy()
            selected = self.xml_tree.selection()
            element = self.tree_node_map.get(selected[0]) if selected else None
            name = row.removeprefix("attr:")
            if element is not None and name in element.attrib:
                element.set(name, value)
                self._refresh_attribute_tree(element)
                self._mark_dirty()
                self._tree_find_update()
                self._refresh_stats()
                self._refresh_inventory()
                self.refresh_diff()
        except tk.TclError:
            pass

    def _destroy_attr_editor(self):
        if self.attr_editor:
            try:
                self.attr_editor.destroy()
            except tk.TclError:
                pass
            self.attr_editor = None

    def add_attribute(self):
        element = self._selected_tree_element()
        if element is None:
            return
        name = simpledialog.askstring("New Attribute", "Attribute name:", parent=self.root)
        if not name:
            return
        if name in element.attrib:
            messagebox.showwarning("Attribute Already Exists", f"The element already has an attribute named {name}.")
            return
        value = simpledialog.askstring("New Attribute", f"Value for {name}:", parent=self.root)
        if value is None:
            return
        element.set(name, value)
        self._refresh_attribute_tree(element)
        self._refresh_stats()
        self._refresh_inventory()
        self._mark_dirty()
        self._tree_find_update()

    def remove_attribute(self):
        element = self._selected_tree_element()
        selected = self.attr_tree.selection()
        if element is None or not selected:
            return
        name = selected[0].removeprefix("attr:")
        if messagebox.askyesno("Remove Attribute", f"Remove attribute {name}?"):
            element.attrib.pop(name, None)
            self._refresh_attribute_tree(element)
            self._refresh_stats()
            self._refresh_inventory()
            self._mark_dirty()
            self._tree_find_update()

    def add_child_node(self):
        parent = self._selected_tree_element()
        if parent is None:
            return
        tag = simpledialog.askstring("Add Element", "New element name:", parent=self.root)
        if not tag:
            return
        child = ET.SubElement(parent, tag)
        self._refresh_xml_tree()
        self._reveal_element(child)
        self._refresh_stats()
        self._refresh_inventory()
        self._mark_dirty()

    def remove_selected_node(self):
        element = self._selected_tree_element()
        if element is None or element is self.document.root:
            messagebox.showwarning("Cannot Remove", "The root element cannot be removed.")
            return
        parent_map = {child: node for node in self.document.root.iter() for child in list(node)}
        parent = parent_map.get(element)
        if parent is not None and messagebox.askyesno("Remove Element", f"Remove <{element.tag}> and all of its child elements?"):
            parent.remove(element)
            self._refresh_all()
            self._mark_dirty()

    def _selected_tree_element(self):
        if not self.document:
            return None
        selected = self.xml_tree.selection()
        return self.tree_node_map.get(selected[0]) if selected else None

    def _refresh_stats(self):
        if not hasattr(self, "stats_tree"):
            return
        tree = self.stats_tree
        tree.delete(*tree.get_children())
        mapping = {}
        if self.document:
            actor, related = find_player_stats_context(self.document.root)
            if actor is not None:
                for element, name, value in iter_attribute_rows(related):
                    label = f"{element.tag}.{name}"
                    iid = tree.insert("", "end", values=(label, value))
                    mapping[iid] = (element, name)
            if not mapping:
                tree.insert("", "end", values=(
                    "Player character statistics unavailable in this document (open a .CSF save)", ""))
        else:
            tree.insert("", "end", values=("Open a .CSF save to view character statistics", ""))
        self.editable_tables["stats"] = mapping
        if hasattr(self, "player_advanced_tree"):
            self._refresh_advanced_player_editor()

    def _refresh_inventory(self):
        if not hasattr(self, "inventory_tree"):
            return
        tree = self.inventory_tree
        tree.delete(*tree.get_children())
        mapping = {}
        if self.document:
            inventory, player_extension, weapon_extensions = find_inventory_context(self.document.root)
            rows = []
            if inventory is not None:
                rows.extend((inventory, key, f"InventoryItems.{key}") for key in inventory.attrib)
                for child in list(inventory):
                    if child.tag == "Slots":
                        rows.extend((child, key, f"Slots.{key}") for key in child.attrib)
                        for index, slot in enumerate(list(child)):
                            rows.extend((slot, key, f"Slot {index+1}.{key}") for key in slot.attrib)
                    elif child.tag == "Ammo":
                        rows.extend((child, key, f"Inventory ammo.{key}") for key in child.attrib)
                        for ammo_index, ammo in enumerate(list(child), 1):
                            ammo_name = ammo.attrib.get("AmmoName", f"#{ammo_index}")
                            rows.extend((ammo, key, f"{ammo_name}.{key}") for key in ammo.attrib)
            for weapon_index, extension in enumerate(weapon_extensions, 1):
                weapon_name = extension.attrib.get("name", f"Weapon {weapon_index}")
                rows.extend((extension, key, f"{weapon_name}.{key}") for key in extension.attrib if key not in ("ownerId", "OwnerId"))
                for desc in list(extension):
                    if desc.tag in ("WeaponAmmo", "WeaponStats", "Ammo"):
                        rows.extend((desc, key, f"{weapon_name}.{desc.tag}.{key}") for key in desc.attrib)
                        for ammo_index, ammo in enumerate(list(desc), 1):
                            ammo_name = ammo.attrib.get("AmmoName", f"Ammo {ammo_index}")
                            rows.extend((ammo, key, f"{weapon_name}.{ammo_name}.{key}") for key in ammo.attrib)
            for element, key, label in rows:
                iid = tree.insert("", "end", values=(label, element.attrib.get(key, "")))
                mapping[iid] = (element, key)

            # Some checkpoint/chapter saves keep the player's inventory only
            # inside an embedded XML snapshot under LTLInventory. Show this
            # separately, read-only; never write to the decoded shadow copy.
            snapshot = parse_ltl_inventory_snapshot(self.document.root)
            if snapshot is not None:
                tree.insert("", "end", values=("--- Long-term inventory snapshot (read-only) ---", ""))
                for key, value in snapshot.attrib.items():
                    tree.insert("", "end", values=(f"Saved inventory.{key}", value))
                for item_index, item in enumerate(snapshot.findall("Items"), 1):
                    name = item.get("ItemName", f"Item #{item_index}")
                    tree.insert("", "end", values=(f"Saved item {item_index}", name))
                    for attachment in item.findall(".//Accessory"):
                        tree.insert("", "end", values=(f"{name}.attachment", attachment.get("Name", "")))
                for ammo in snapshot.findall("Ammo/Ammo"):
                    ammo_name = ammo.get("AmmoName", "Unknown ammo")
                    for key, value in ammo.attrib.items():
                        if key != "AmmoName":
                            tree.insert("", "end", values=(f"Saved ammo.{ammo_name}.{key}", value))
            elif not rows:
                # Explicitly distinguish an empty player inventory from
                # missing/unrecognized serialized inventory data.
                label = ("Player inventory is empty in this save"
                         if inventory is not None else
                         "No player inventory data available in this save")
                tree.insert("", "end", values=(label, ""))
        self.editable_tables["inventory"] = mapping

    def _edit_data_table_cell(self, event, tree: ttk.Treeview):
        if not self.document:
            return
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not row or col != "#2":
            return
        # Read-only snapshot rows have no backing editable XML element.
        table_name = "stats" if tree is self.stats_tree else "inventory"
        if row not in self.editable_tables.get(table_name, {}):
            return
        box = tree.bbox(row, col)
        if not box:
            return
        x, y, width, height = box
        current = tree.set(row, "value")
        editor = ttk.Entry(tree)
        editor.insert(0, current)
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        editor.select_range(0, "end")

        def finish(_event=None):
            if not editor.winfo_exists():
                return
            value = editor.get()
            editor.destroy()
            table_name = "stats" if tree is self.stats_tree else "inventory"
            target = self.editable_tables.get(table_name, {}).get(row)
            if target:
                element, attr = target
                element.set(attr, value)
                tree.set(row, "value", value)
                self._mark_dirty()
                self.refresh_diff()
                if table_name == "stats":
                    self._refresh_inventory()
                else:
                    self._refresh_stats()

        editor.bind("<Return>", finish)
        editor.bind("<FocusOut>", finish)
        editor.bind("<Escape>", lambda _e: editor.destroy())

    def refresh_diff(self):
        if not hasattr(self, "diff_tree"):
            return
        self.diff_tree.delete(*self.diff_tree.get_children())
        if not self.document:
            return
        if self.document.baseline_root is None:
            return  # No changes yet; avoid building an unnecessary baseline.
        old_map = self._flatten_tree(self.document.baseline_root)
        new_map = self._flatten_tree(self.document.root)
        changes = 0
        for path in sorted(set(old_map) | set(new_map)):
            old_el = old_map.get(path)
            new_el = new_map.get(path)
            if old_el is None:
                self.diff_tree.insert("", "end", text=path, values=("Added Element", "", f"<{new_el.tag}>"))
                changes += 1
                continue
            if new_el is None:
                self.diff_tree.insert("", "end", text=path, values=("Removed Element", f"<{old_el.tag}>", ""))
                changes += 1
                continue
            for attr in sorted(set(old_el.attrib) | set(new_el.attrib)):
                old_value = old_el.attrib.get(attr, "<missing>")
                new_value = new_el.attrib.get(attr, "<missing>")
                if old_value != new_value:
                    self.diff_tree.insert("", "end", text=f"{path}/@{attr}", values=("Attribute", old_value, new_value))
                    changes += 1
        if not changes:
            self.diff_tree.insert("", "end", text="No differences", values=("", "", ""))
        self.status_var.set(f"Diff Viewer: {changes} changes detected." if changes else self.status_var.get())

    def _flatten_tree(self, root: ET.Element) -> dict[str, ET.Element]:
        result = {}

        def walk(node, path):
            result[path] = node
            counts = {}
            for child in list(node):
                index = counts.get(child.tag, 0)
                counts[child.tag] = index + 1
                walk(child, f"{path}/{child.tag}[{index}]")
        walk(root, f"/{root.tag}[0]")
        return result

    def run_value_search(self):
        if not self._require_document():
            return
        query = self.search_var.get().strip().casefold()
        self.search_tree.delete(*self.search_tree.get_children())
        if not query:
            self.search_count.configure(text="Enter a search value.")
            return
        matches = 0
        for path, element in self._flatten_tree(self.document.root).items():
            if query in element.tag.casefold():
                self.search_tree.insert("", "end", text=path, values=("Element", element.tag, ""))
                matches += 1
            for name, value in element.attrib.items():
                if query in name.casefold() or query in value.casefold():
                    self.search_tree.insert("", "end", text=path, values=("Attribute", name, value[:500]))
                    matches += 1
            if matches >= 5000:
                break
        self.search_count.configure(text=f"Results: {matches:,}" + (" (limit 5000)" if matches >= 5000 else ""))

    def _search_result_open(self, _event=None):
        selected = self.search_tree.selection()
        if not selected:
            return
        path = self.search_tree.item(selected[0], "text")
        element = self._flatten_tree(self.document.root).get(path)
        if element is not None:
            self.tabs.select(self.tree_tab)
            self._reveal_element(element)

    def open_global_search(self, _event=None):
        if self._quick_search_window is not None and self._quick_search_window.winfo_exists():
            self._quick_search_window.deiconify()
            self._quick_search_window.lift()
            self._quick_search_entry.focus_set()
            return "break"

        window = tk.Toplevel(self.root)
        self._quick_search_window = window
        window.title("Global Quick Search")
        window.geometry("850x560")
        window.minsize(640, 400)
        window.transient(self.root)
        window.configure(background=self.colors["bg"])
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Global Quick Search", font=("Segoe UI Semibold", 16)).pack(anchor="w")
        ttk.Label(
            outer, text="Search commands, workspaces, recent files, and values in the open save. Press Enter to open a result.",
            style="Muted.TLabel", wraplength=800,
        ).pack(anchor="w", pady=(3, 10))
        query_var = tk.StringVar()
        self._quick_search_entry = ttk.Entry(outer, textvariable=query_var, font=("Segoe UI", 12))
        self._quick_search_entry.pack(fill="x", pady=(0, 10))
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        self._quick_search_tree = ttk.Treeview(frame, columns=("category", "details"), show="tree headings")
        self._quick_search_tree.heading("#0", text="Result")
        self._quick_search_tree.heading("category", text="Type")
        self._quick_search_tree.heading("details", text="Details")
        self._quick_search_tree.column("#0", width=260, stretch=False)
        self._quick_search_tree.column("category", width=120, stretch=False)
        self._quick_search_tree.column("details", width=420, stretch=True)
        ybar = ttk.Scrollbar(frame, orient="vertical", command=self._quick_search_tree.yview)
        self._quick_search_tree.configure(yscrollcommand=ybar.set)
        self._quick_search_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        self._quick_search_count = ttk.Label(footer, text="Type to search", style="Muted.TLabel")
        self._quick_search_count.pack(side="left")
        ttk.Button(footer, text="Close", command=window.destroy).pack(side="right")

        query_var.trace_add("write", lambda *_: self._refresh_global_search_results(query_var.get()))
        self._quick_search_entry.bind("<Return>", lambda _e: self._activate_global_search_result())
        self._quick_search_tree.bind("<Double-1>", lambda _e: self._activate_global_search_result())
        window.bind("<Escape>", lambda _e: window.destroy())
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._refresh_global_search_results("")
        self._quick_search_entry.focus_set()
        return "break"

    def _refresh_global_search_results(self, query: str):
        if not self._quick_search_window or not self._quick_search_window.winfo_exists():
            return
        tree = self._quick_search_tree
        tree.delete(*tree.get_children())
        self._quick_search_results.clear()
        query = query.strip().casefold()
        count = 0

        def add(label: str, category: str, details: str, payload: dict):
            nonlocal count
            if count >= 250:
                return
            iid = f"result-{count}"
            tree.insert("", "end", iid=iid, text=label, values=(category, details))
            self._quick_search_results[iid] = payload
            count += 1

        commands = [
            ("Open Encrypted Save…", "Command", "Ctrl+O", self.open_file),
            ("Open Last Save", "Command", "Reopen the most recent save", self.open_last_save),
            ("Select Game EXE…", "Command", "Choose the matching game executable", self.choose_exe),
            ("Open Game Profile…", "Command", "Edit encrypted attributes profile", self.open_profile),
            ("Save Game Profile", "Command", "Encrypt and save profile with backup", self.save_profile),
            ("Save Encrypted File", "Command", "Ctrl+S · overwrite the open save safely", self.save_file),
            ("Import XML…", "Command", "Replace the current editable XML tree", self.import_xml),
            ("Export XML…", "Command", "Export the current document as XML", lambda: self.export_data("xml")),
            ("Export JSON…", "Command", "Export the current document as JSON", lambda: self.export_data("json")),
            ("Export CSV…", "Command", "Export flattened attributes as CSV", lambda: self.export_data("csv")),
            ("Toggle Dark Mode", "Command", "Switch between dark and light themes", self.toggle_theme),
            ("Reset Layout", "Command", "Restore the default split positions", self.reset_layout),
        ]
        for label, category, details, callback in commands:
            if not query or query in label.casefold() or query in details.casefold():
                add(label, category, details, {"kind": "command", "callback": callback})

        workspaces = [
            ("XML Tree Viewer", self.tree_tab),
            ("Player Statistics", self.stats_tab),
            ("Advanced Player Editor", self.player_advanced_tab),
            ("Game Profile Editor", self.profile_tab),
            ("Inventory Editor", self.inventory_tab),
            ("Save Diff Viewer", self.diff_tab),
            ("Value Search", self.search_tab),
            ("Save Slot Manager", self.slot_tab),
            ("Backup Manager", self.backup_tab),
            ("Batch / Export", self.batch_tab),
        ]
        for label, tab in workspaces:
            if not query or query in label.casefold():
                add(label, "Workspace", "Switch to this editor tab", {"kind": "tab", "tab": tab})

        for path_text in self.recent_saves[:MAX_RECENT_FILES]:
            path = Path(path_text)
            detail = str(path.parent)
            if not query or query in path.name.casefold() or query in detail.casefold():
                add(path.name, "Recent Save", detail, {"kind": "file", "path": path})

        # Include persistent profile fields in the command palette.
        if self.profile_document and len(query) >= 2:
            for section, name, value, node in profile_fields(self.profile_document.root, self.profile_game):
                if query in name.casefold() or query in value.casefold() or query in section.casefold():
                    add(name, "Profile Field", f"{section} | {value[:100]}", {"kind": "profile_field", "name": name})
                if count >= 250:
                    break

        # Search the full in-memory XML tree after two characters. The tree and
        # attribute values are never written to disk by this command palette.
        if self.document and len(query) >= 2:
            root = self.document.root
            paths: dict[int, str] = {}
            stack = [(root, f"/{root.tag}[0]")]
            while stack:
                node, node_path = stack.pop()
                paths[id(node)] = node_path
                counts: dict[str, int] = {}
                children = list(node)
                for child in children:
                    index = counts.get(child.tag, 0)
                    counts[child.tag] = index + 1
                    stack.append((child, f"{node_path}/{child.tag}[{index}]"))

            for element in root.iter():
                path = paths.get(id(element), "")
                if query in element.tag.casefold():
                    add(f"<{element.tag}>", "XML Element", path, {"kind": "element", "element": element})
                text_value = (element.text or "").strip()
                if text_value and query in text_value.casefold():
                    add(f"Text in <{element.tag}>", "XML Text", f"{path} · {text_value[:160]}", {"kind": "element", "element": element})
                for name, value in element.attrib.items():
                    if query in name.casefold() or query in value.casefold():
                        add(f"{element.tag}.@{name}", "XML Attribute", f"{path} · {value[:180]}", {"kind": "element", "element": element})
                if count >= 250:
                    break

        if count == 0:
            add("No matching results", "Info", "Try another search term", {"kind": "none"})
        self._quick_search_count.configure(text=f"{count:,} results" + (" (limit 250)" if count >= 250 else ""))
        first = tree.get_children()
        if first:
            tree.selection_set(first[0])
            tree.focus(first[0])

    def _activate_global_search_result(self):
        selection = self._quick_search_tree.selection() if hasattr(self, "_quick_search_tree") else ()
        if not selection:
            return
        payload = self._quick_search_results.get(selection[0], {})
        window = self._quick_search_window
        if window is not None and window.winfo_exists():
            window.destroy()
        kind = payload.get("kind")
        if kind == "command":
            self.root.after(40, payload["callback"])
        elif kind == "tab":
            self.tabs.select(payload["tab"])
        elif kind == "file":
            self.root.after(40, lambda p=payload["path"]: self.open_profile(p) if p.name.casefold() in ("attributes_pc.xml", "attributes.xml") else self.open_file(p))
        elif kind == "profile_field":
            self.tabs.select(self.profile_tab)
            self.profile_category.set("All fields")
            self.profile_filter.set(payload["name"])
            self._refresh_profile_editor()
        elif kind == "element":
            self.tabs.select(self.tree_tab)
            self._navigate_to_element(payload["element"])

    def _navigate_to_element(self, element: ET.Element):
        self.tabs.select(self.tree_tab)
        self._reveal_element(element)

    @staticmethod
    def _running_elevated_on_windows():
        if os.name != "nt":
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False

    def _on_drop_activity(self, action):
        if action == "enter":
            self.drop_zone.configure(text="Release the file to open it")
        elif action == "received":
            self.status_var.set("File drop received. Opening...")
        elif action == "error":
            self.status_var.set("Drop error. See logs/editor.log.")
        else:
            self.drop_zone.configure(text="Drag encrypted CSF / attributes.xml / .meta here")

    def _install_windows_drop_target(self):
        try:
            target = WindowsDropTarget(
                self.root, self._handle_dropped_paths, widgets=(self.drop_zone,),
                on_activity=self._on_drop_activity, register_content=True,
            )
            registered = target.install()
            self._drop_target = target
            self.dnd_install_button.configure(text="Drag & Drop: ON", state="disabled")
            self.drop_zone.configure(text="Drag encrypted CSF / attributes.xml / .meta here")
            if self._running_elevated_on_windows():
                self.status_var.set(
                    "Drag & Drop enabled, but Windows may block drops from Explorer "
                    "while this editor runs as Administrator. Restart normally."
                )
            else:
                self.status_var.set(f"Drag & Drop enabled (TkDND; {registered} drop surfaces).")
        except Exception as exc:
            self._drop_target = None
            self._drop_load_error = str(exc)
            self.status_var.set(f"Drag & Drop OFF: {exc}")
            self.drop_zone.configure(text="Drag & Drop OFF — click Enable Drag & Drop →")
            self.dnd_install_button.configure(text="Enable Drag & Drop", state="normal")
            try:
                with (Path(__file__).resolve().parent / "logs" / "editor.log").open("a", encoding="utf-8") as log:
                    log.write(f"\nDrag & Drop initialization: {exc}\nRuntime: {runtime_console_python()}\n")
            except OSError:
                pass

    def _enable_drag_drop(self):
        if self._drop_install_in_progress or self._drop_target is not None:
            return
        if not messagebox.askyesno(
            "Enable Drag & Drop",
            "Install / repair tkinterdnd2 for the Python running this editor?\n\n"
            f"Python: {runtime_console_python()}\n\n"
            "An internet connection is required. Restart the editor after installation.",
        ):
            return
        self._drop_install_in_progress = True
        self.dnd_install_button.configure(state="disabled", text="Installing...")
        self.status_var.set("Installing tkinterdnd2 for the running Python interpreter...")
        threading.Thread(target=self._install_drag_drop_worker, daemon=True).start()
        self.root.after(200, self._poll_drag_drop_install)

    def _install_drag_drop_worker(self):
        succeeded, diagnostic = install_tkdnd_for_runtime()
        try:
            log_path = Path(__file__).resolve().parent / "logs" / "editor.log"
            log_path.parent.mkdir(exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n=== Drag & Drop installation ===\n" + diagnostic + "\n")
        except OSError:
            pass
        self._dnd_install_results.put(succeeded)

    def _poll_drag_drop_install(self):
        try:
            succeeded = self._dnd_install_results.get_nowait()
        except queue.Empty:
            self.root.after(200, self._poll_drag_drop_install)
            return
        self._finish_drag_drop_install(succeeded)

    def _finish_drag_drop_install(self, succeeded):
        self._drop_install_in_progress = False
        self.dnd_install_button.configure(state="normal", text="Enable Drag & Drop")
        if succeeded:
            self.status_var.set("TkDND installed. Close and reopen the editor to activate Drag & Drop.")
            self.drop_zone.configure(text="TkDND installed — restart the editor to enable drops")
            messagebox.showinfo(
                "Drag & Drop Installed",
                "TkDND was installed into the running Python environment.\n"
                "Close and reopen Crysis Save Editor to enable Drag & Drop.",
            )
        else:
            self.status_var.set("TkDND installation failed; see logs/editor.log.")
            messagebox.showerror(
                "Drag & Drop Installation Failed",
                "The installation did not succeed. Check logs/editor.log for the pip error.\n"
                f"Interpreter: {runtime_console_python()}",
            )

    def _select_key_for_dropped_save(self, path: Path) -> bool:
        """Match the encrypted file header to the bundled game key, if needed."""
        try:
            with path.open("rb") as stream:
                signature = stream.read(7)
        except OSError as exc:
            messagebox.showerror("File Drop Error", f"Cannot read {path.name}: {exc}")
            return False
        matching_game = {
            CRYSIS1_MAGIC: "CrysisRemastered",
            CRYSIS2_MAGIC: "Crysis2Remastered",
            CRYSIS3_MAGIC: "Crysis3Remastered",
        }.get(signature)
        if matching_game is None:
            messagebox.showerror(
                "Unsupported File",
                f"{path.name} is not a recognized encrypted Crysis save. "
                "For decrypted XML, use Import XML instead.",
            )
            return False
        if self.exe_path is not None:
            try:
                current_magic, _ = extract_cipher(self.exe_path)
                if current_magic == signature:
                    return True
            except Exception:
                pass
        bundled_key = Path(__file__).resolve().parent / "keys" / f"{matching_game}.crysiskey"
        if bundled_key.is_file():
            self._select_exe_path(bundled_key)
            if self.exe_path is not None:
                try:
                    selected_magic, _ = extract_cipher(self.exe_path)
                    if selected_magic == signature:
                        return True
                except Exception:
                    pass
        if self.exe_path is None:
            messagebox.showinfo(
                "Select Game Key",
                f"Select an EXE or .crysiskey for {matching_game} to open {path.name}.",
            )
            self.choose_exe()
        if self.exe_path is None:
            return False
        try:
            selected_magic, _ = extract_cipher(self.exe_path)
            return selected_magic == signature
        except Exception:
            return False

    def _handle_dropped_paths(self, dropped_paths):
        """Route file drops through normal protected open commands on Tk's event loop."""
        try:
            paths = [Path(item).expanduser() for item in dropped_paths]
            exe_paths = [p for p in paths if p.is_file() and p.suffix.casefold() in (".exe", ".crysiskey")]
            folders = [p for p in paths if p.is_dir()]
            saves = [
                p for p in paths if p.is_file() and p.suffix.casefold() in (".csf", ".meta", ".xml")
                and ".bak" not in p.name.casefold()
            ]
            if exe_paths:
                self._select_exe_path(exe_paths[0])
            if folders:
                self.slot_folder_var.set(str(folders[0].resolve()))
                self._save_settings()
                self.refresh_slot_list()
                self.tabs.select(self.slot_tab)
            if saves:
                # Prefer a playable save to its sidecar .meta if both are dropped.
                saves.sort(key=lambda p: (0 if p.name.casefold().startswith("attributes") and p.suffix.casefold() == ".xml"
                                          else 1 if p.suffix.casefold() == ".csf"
                                          else 2 if p.suffix.casefold() == ".xml" else 3))
                save_path = saves[0]
                if save_path.suffix.casefold() == ".meta":
                    # A .meta is a sidecar, not an encrypted save container.
                    companion = save_path.with_suffix(".CSF")
                    if not companion.is_file():
                        companion = save_path.with_suffix(".csf")
                    if not companion.is_file():
                        messagebox.showwarning(
                            "Missing Save File",
                            f"{save_path.name} is metadata only. Drop the corresponding .CSF file instead.",
                        )
                        return
                    save_path = companion
                if not self._select_key_for_dropped_save(save_path):
                    return
                if not self.slot_folder_var.get().strip():
                    self.slot_folder_var.set(str(save_path.parent))
                    self._save_settings()
                if save_path.name.casefold() in ("attributes_pc.xml", "attributes.xml"):
                    self.open_profile(save_path)
                else:
                    self.open_file(save_path)
                if len(saves) > 1:
                    self.status_var.set(f"Processed {save_path.name}; {len(saves) - 1} other dropped file(s) were not opened.")
            elif exe_paths and not folders:
                self.status_var.set(f"Game key selected: {exe_paths[0]}")
            elif folders:
                self.status_var.set(f"Save folder loaded: {folders[0]}")
            else:
                self.status_var.set("No supported game EXE, key, encrypted save, or save folder was dropped.")
        except Exception as exc:
            WindowsDropTarget._write_error()
            messagebox.showerror("File Drop Error", f"Unable to handle the dropped file(s):\n{exc}")

    def _remove_windows_drop_target(self):
        target = self._drop_target
        if target is not None:
            target.close()
            self._drop_target = None

    def _parent_map(self):
        return {child: node for node in self.document.root.iter() for child in list(node)}

    def _path_for(self, element: ET.Element) -> str:
        if not self.document:
            return ""
        parent_map = self._parent_map()
        parts = []
        current = element
        while current is not None:
            parent = parent_map.get(current)
            if parent is None:
                parts.append(f"{current.tag}[0]")
                break
            siblings = [c for c in list(parent) if c.tag == current.tag]
            index = siblings.index(current)
            parts.append(f"{current.tag}[{index}]")
            current = parent
        return "/" + "/".join(reversed(parts))

    def _reveal_element(self, element: ET.Element):
        if not self.document:
            return
        parent_map = self._parent_map()
        chain = []
        current = element
        while current is not None:
            chain.append(current)
            current = parent_map.get(current)
        chain.reverse()
        parent_iid = ""
        for index, node in enumerate(chain):
            if index == 0:
                iid = self.tree_iid_by_node.get(id(node))
            else:
                parent_node = chain[index - 1]
                parent_iid = self.tree_iid_by_node.get(id(parent_node), "")
                if parent_iid:
                    self._populate_tree_children(parent_iid, parent_node)
                iid = self.tree_iid_by_node.get(id(node))
            if not iid:
                self._refresh_xml_tree()
                return self._reveal_element(element)
            self.xml_tree.item(iid, open=True)
        iid = self.tree_iid_by_node.get(id(element))
        if iid:
            self.xml_tree.selection_set(iid)
            self.xml_tree.focus(iid)
            self.xml_tree.see(iid)
            self._tree_selected()

    def choose_batch_input(self):
        path = filedialog.askdirectory(title="Select Save Folder")
        if path:
            self.batch_input_var.set(path)

    def choose_batch_output(self):
        path = filedialog.askdirectory(title="Select Output Folder for Decrypted XML")
        if path:
            self.batch_output_var.set(path)

    def load_folder_into_memory(self):
        """Load supported files into RAM without writing decrypted XML to disk."""
        if (getattr(self, "memory_task_active", False) or getattr(self, "memory_loading_active", False)
                or getattr(self, "batch_export_active", False)):
            messagebox.showwarning("Task Running", "Finish the current task before loading RAM files.")
            return
        if self.exe_path is None or not self.exe_path.is_file():
            messagebox.showwarning("Missing Key", "Select a game EXE or saved key first.")
            return
        folder = Path(self.batch_input_var.get().strip())
        if not folder.is_dir():
            messagebox.showwarning("Missing Folder", "Select an existing input folder.")
            return
        if self.memory_documents and not messagebox.askyesno("Replace RAM Documents", "Discard the previously loaded RAM collection, including unsaved changes?"):
            return
        self.memory_documents.clear()
        self.memory_dirty_paths.clear()
        self.memory_list.delete(0, "end")
        self.memory_button.configure(state="disabled")
        self.memory_loading_active = True
        self._batch_log("Loading encrypted documents into RAM (no XML export)...\n")
        exe = self.exe_path
        recursive = self.recursive_var.get()

        def worker():
            try:
                self._load_ram_docs_worker(folder, recursive, exe)
            except Exception as exc:
                self.batch_queue.put(("memory_load_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _load_ram_docs_worker(self, folder, recursive, exe):
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        paths = [p for p in iterator if p.is_file() and p.suffix.lower() in (".csf", ".meta", ".xml")]
        loaded = {}
        failed = 0
        used = 0
        for index, path in enumerate(paths, 1):
            try:
                doc = open_encrypted_save(path, exe, snapshot_baseline=False)
                estimate = max(doc.decompressed_size or 0, doc.file_size, doc.node_count * 200)
                if used + estimate > self.memory_limit_bytes:
                    raise MemoryError("RAM collection limit (512 MiB estimate) reached")
                loaded[path.resolve()] = doc
                used += estimate
                status = "Loaded into RAM"
            except Exception as exc:
                failed += 1
                status = f"Skipped: {exc}"
            self.batch_queue.put(("progress", index, len(paths), str(path), status))
        self.batch_queue.put(("memory_done", loaded, failed, used))

    def _selected_memory_paths(self):
        return [self.memory_visible_paths[i] for i in self.memory_list.curselection()
                if i < len(self.memory_visible_paths)]

    def _dirty_memory_paths(self) -> set[Path]:
        """Detect GUI edits as well as edits made through Apply to RAM."""
        loaded = getattr(self, "memory_documents", {})
        tracked = getattr(self, "memory_dirty_paths", set())
        return {path for path, doc in loaded.items()
                if document_is_dirty(doc) or path in tracked}

    def _set_memory_preview(self, lines):
        self.memory_preview_text.configure(state="normal")
        self.memory_preview_text.delete("1.0", "end")
        self.memory_preview_text.insert("end", "\n".join(lines))
        self.memory_preview_text.configure(state="disabled")

    @staticmethod
    def _matching_memory_attributes(doc, field):
        # Change XML attribute values, not element names or structural identifiers.
        return [(element, element.attrib[field]) for element in doc.root.iter() if field in element.attrib]

    def _begin_memory_task(self, title):
        if self.memory_task_active:
            messagebox.showinfo("Task Running", "Wait for the current RAM task or cancel it.")
            return False
        self.memory_task_active = True
        self.memory_cancel.clear()
        for button in (self.memory_button, self.preview_button, self.apply_button, self.save_memory_button):
            button.configure(state="disabled")
        self.cancel_memory_button.configure(state="normal")
        # Prevent editing a document while a worker is traversing or saving it.
        self._memory_tab_states = [(i, self.tabs.tab(i, "state")) for i in range(self.tabs.index("end"))]
        for i, _ in self._memory_tab_states:
            if self.tabs.nametowidget(self.tabs.tabs()[i]) is not self.batch_tab:
                self.tabs.tab(i, state="disabled")
        self.status_var.set(title)
        return True

    def _end_memory_task(self):
        self.memory_task_active = False
        for button in (self.memory_button, self.preview_button, self.apply_button, self.save_memory_button):
            button.configure(state="normal")
        self.cancel_memory_button.configure(state="disabled")
        for i, state in self._memory_tab_states:
            self.tabs.tab(i, state=state)
        self._memory_tab_states = []

    def cancel_memory_task(self):
        self.memory_cancel.set()
        self.status_var.set("Cancellation requested; finishing the current save safely...")

    def preview_memory_batch(self):
        paths = self._selected_memory_paths()
        field = self.batch_field_var.get().strip()
        value = self.batch_value_var.get()
        if not paths or not field or not value.strip():
            messagebox.showwarning("Batch Edit", "Select save slots and enter an exact attribute name and a new value.")
            return
        if not self._begin_memory_task("Calculating batch preview..."):
            return
        self.memory_preview = {}
        self._set_memory_preview(["Calculating preview in background..."])
        def worker():
            preview, lines = {}, []
            for index, path in enumerate(paths, 1):
                if self.memory_cancel.is_set():
                    break
                matches = self._matching_memory_attributes(self.memory_documents[path], field)
                changed = [(field, old, value) for _, old in matches if old != value]
                if changed:
                    preview[path] = changed
                    lines.append(f"{path.name}: {len(changed)} changes; " + ", ".join(f"{old} -> {value}" for _, old, _ in changed[:3]))
                else:
                    lines.append(f"{path.name}: no changes")
                if index % 10 == 0:
                    self.batch_queue.put(("memory_status", f"Preview: {index}/{len(paths)}"))
            self.batch_queue.put(("memory_preview_done", preview, lines, len(paths), self.memory_cancel.is_set()))
        threading.Thread(target=worker, daemon=True).start()

    def apply_memory_batch(self):
        paths = self._selected_memory_paths()
        field = self.batch_field_var.get().strip()
        value = self.batch_value_var.get()
        if not paths or not field or not value.strip():
            messagebox.showwarning("Batch Edit", "Select save slots and enter an exact attribute name and a new value.")
            return
        if not messagebox.askyesno("Apply Batch Changes to RAM", f"Change '{field}' in {len(paths)} selected save slots?\nFiles on disk will not be changed yet."):
            return
        if not self._begin_memory_task("Applying changes to RAM..."):
            return
        def worker():
            count, touched = 0, []
            for index, path in enumerate(paths, 1):
                if self.memory_cancel.is_set():
                    break
                changed = 0
                doc = self.memory_documents[path]
                matches = self._matching_memory_attributes(doc, field)
                if any(old != value for _, old in matches) and doc.baseline_root is None:
                    doc.baseline_root = copy.deepcopy(doc.root)
                for element, old in matches:
                    if old != value:
                        element.set(field, value)
                        changed += 1
                if changed:
                    touched.append(path)
                    count += changed
                if index % 10 == 0:
                    self.batch_queue.put(("memory_status", f"Apply: {index}/{len(paths)}"))
            self.batch_queue.put(("memory_apply_done", count, touched, self.memory_cancel.is_set()))
        threading.Thread(target=worker, daemon=True).start()

    def save_selected_memory(self):
        dirty = self._dirty_memory_paths()
        paths = [p for p in self._selected_memory_paths() if p in dirty]
        if not paths:
            messagebox.showinfo("Save Selected", "No unsaved changes in the selected RAM save slots.")
            return
        if not messagebox.askyesno("Save Encrypted Files", f"Save {len(paths)} modified encrypted saves?\nEach file gets a .bak backup and post-save verification. Other saves remain unchanged."):
            return
        if not self._begin_memory_task("Saving encrypted files in background..."):
            return
        def worker():
            success, errors, saved = 0, [], []
            for index, path in enumerate(paths, 1):
                if self.memory_cancel.is_set():
                    break
                try:
                    result = save_over_encrypted_file(self.memory_documents[path])
                    saved.append(path)
                    success += 1
                    self.batch_queue.put(("memory_log", f"Saved {path.name}; backup: {result['backup']}\n"))
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                    self.batch_queue.put(("memory_log", f"FAILED {path.name}: {exc}\n"))
                self.batch_queue.put(("memory_status", f"Saving: {index}/{len(paths)}"))
            self.batch_queue.put(("memory_save_done", success, errors, saved, len(paths), self.memory_cancel.is_set()))
        threading.Thread(target=worker, daemon=True).start()

    def open_selected_memory_document(self):
        if self.memory_task_active:
            return
        paths = self._selected_memory_paths()
        if not paths:
            return
        doc = self.memory_documents[paths[0]]
        if not self._confirm_discard_changes(save=True, action="open a RAM document"):
            return
        self.document = doc
        self._remember_save(doc.path)
        self._refresh_all()
        self.file_var.set(f"File: {doc.path} | {doc.node_count:,} nodes | loaded from RAM")
        self.status_var.set(f"Opened {doc.path.name} from the RAM collection; no second decryption required.")
        self.dirty_label.configure(text="")

    def clear_memory_documents(self):
        if self.memory_task_active:
            return
        if self._dirty_memory_paths():
            if not messagebox.askyesno("Discard Unsaved RAM Changes", "Some RAM documents contain unsaved edits. Discard them?"):
                return
        self.memory_documents.clear()
        self.memory_dirty_paths.clear()
        self.memory_visible_paths.clear()
        self.memory_preview.clear()
        self.memory_list.delete(0, "end")
        self._set_memory_preview([])
        self.status_var.set("RAM collection cleared. The currently open document remains available.")

    def start_batch(self):
        if self.batch_export_active or self.memory_task_active or self.memory_loading_active:
            messagebox.showinfo("Task Running", "Finish the current task before starting another batch.")
            return
        if self.exe_path is None or not self.exe_path.is_file():
            messagebox.showwarning("Missing Key", "Select a game EXE or saved key first.")
            return
        source = self.batch_input_var.get().strip()
        output = self.batch_output_var.get().strip()
        if not source or not output:
            messagebox.showwarning("Missing Folder", "Select both an input and an output folder.")
            return
        if Path(source).expanduser().resolve() == Path(output).expanduser().resolve():
            messagebox.showerror("Batch Processing", "Input and output folders must be different. Choose a separate export folder.")
            return
        if not messagebox.askyesno("Batch Processing", "Decrypt supported files in the selected folder to XML?\nSource files will not be modified.\nExisting XML exports will be skipped, never overwritten."):
            return
        self.batch_button.configure(state="disabled")
        self.batch_export_active = True
        # Capture job parameters now: selection changes must not change the
        # cipher or traversal mode halfway through an asynchronous export.
        job_exe, job_recursive = self.exe_path, self.recursive_var.get()
        self._batch_log(f"Start: {source} -> {output}\n")

        def worker():
            try:
                result = batch_decrypt_folder(
                    source,
                    output,
                    job_exe,
                    recursive=job_recursive,
                    progress=lambda i, total, src, status: self.batch_queue.put(("progress", i, total, src, status)),
                )
                self.batch_queue.put(("done", result))
            except Exception as exc:
                self.batch_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_batch_queue(self):
        try:
            for _ in range(60):  # Bound UI work per tick to preserve responsiveness.
                item = self.batch_queue.get_nowait()
                if item[0] == "progress":
                    _, index, total, source, status = item
                    self._batch_log(f"[{index}/{total}] {source} — {status}\n")
                    self.status_var.set(f"Batch Processing: {index}/{total}")
                elif item[0] == "memory_status":
                    self.status_var.set(item[1])
                elif item[0] == "memory_log":
                    self._batch_log(item[1])
                elif item[0] == "memory_preview_done":
                    _, preview, lines, total, cancelled = item
                    self.memory_preview = preview if not cancelled else {}
                    self._set_memory_preview(lines[:200] + [f"Preview: {len(preview)}/{total} saves would change." if not cancelled else "Preview cancelled."])
                    self.status_var.set("Preview complete" if not cancelled else "Preview cancelled")
                    self._end_memory_task()
                elif item[0] == "memory_apply_done":
                    _, count, touched, cancelled = item
                    self.memory_dirty_paths.update(touched)
                    self.memory_preview.clear()
                    self._set_memory_preview([f"Applied {count} edits in {len(touched)} saves in RAM.", "Cancelled after completed slots." if cancelled else "Use Save Selected + Backups to write encrypted saves."])
                    self._batch_log(f"RAM edits: {count} attributes across {len(touched)} saves.\n")
                    if self.document and self.document.path.resolve() in touched:
                        self._refresh_all()
                        self._mark_dirty()
                    self._end_memory_task()
                elif item[0] == "memory_save_done":
                    _, success, errors, saved, total, cancelled = item
                    self.memory_dirty_paths.difference_update(saved)
                    self._set_memory_preview([f"Saved {success}/{total} selected saves with backups and verification."] + errors[:30] + (["Cancelled after current file."] if cancelled else []))
                    self.status_var.set(f"Saved {success}; errors {len(errors)}" + ("; cancelled" if cancelled else ""))
                    if self.document and self.document.path.resolve() in saved:
                        self._refresh_all()
                        self.dirty_label.configure(text="")
                    self._end_memory_task()
                    messagebox.showinfo("Batch Save Finished", f"Saved: {success}\nFailed: {len(errors)}" + ("\nCancelled." if cancelled else ""))
                elif item[0] == "memory_load_error":
                    self.memory_loading_active = False
                    self.memory_button.configure(state="normal")
                    self._batch_log(f"RAM load failed: {item[1]}\n")
                    messagebox.showerror("RAM Load Failed", item[1])
                elif item[0] == "memory_done":
                    self.memory_loading_active = False
                    _, loaded, failed, estimated = item
                    self.memory_documents = loaded
                    self.memory_list.delete(0, "end")
                    self.memory_visible_paths = [p for p, doc in loaded.items() if doc.kind == "csf"]
                    for path in self.memory_visible_paths:
                        self.memory_list.insert("end", path.stem)
                    self.memory_button.configure(state="normal")
                    self._batch_log(f"\nRAM loading completed. Loaded: {len(loaded)}, skipped: {failed}, estimated data: {estimated / (1024*1024):.1f} MiB\n")
                    self.status_var.set(f"{len(self.memory_visible_paths)} save slots available in RAM. Select multiple for batch editing.")
                elif item[0] == "done":
                    result = item[1]
                    self.batch_export_active = False
                    self.batch_button.configure(state="normal")
                    self._batch_log(f"\nCompleted. OK: {result['ok']}, skipped: {result.get('skipped', 0)}, errors: {result['failed']}\n")
                    self.status_var.set(f"Batch completed: {result['ok']} OK, {result.get('skipped', 0)} skipped, {result['failed']} errors.")
                elif item[0] == "error":
                    self.batch_export_active = False
                    self.batch_button.configure(state="normal")
                    self.memory_button.configure(state="normal")
                    self._batch_log(f"\nERROR: {item[1]}\n")
                    messagebox.showerror("Batch Processing", item[1])
        except queue.Empty:
            pass
        self._batch_poll_after_id = self.root.after(100, self._poll_batch_queue)

    def _batch_log(self, text: str):
        self.batch_log.configure(state="normal")
        self.batch_log.insert("end", text)
        self.batch_log.see("end")
        self.batch_log.configure(state="disabled")

    def _require_document(self) -> bool:
        if self.document is None:
            messagebox.showwarning("No Save Open", "Open an encrypted save file first.")
            return False
        return True

    def _mark_dirty(self):
        # Manual edits reach this hook after the change. Recover the original
        # baseline from the still-unmodified encrypted file only on first edit.
        if self.document is not None and self.document.baseline_root is None:
            original = open_encrypted_save(self.document.path, self.document.exe_path,
                                           snapshot_baseline=False)
            if original.source_hash != self.document.source_hash:
                raise SaveEditorError("The original file changed on disk; reopen it.")
            self.document.baseline_root = original.root
        self.dirty_label.configure(text="Unsaved Changes")
        # The main editor may hold the *same object* as a selected RAM slot.
        # Record those GUI edits for Save Selected, even if Apply to RAM was
        # never used; clear stale flags when a user reverts their change.
        loaded = getattr(self, "memory_documents", {})
        if self.document is not None and self.document.path.resolve() in loaded:
            if document_is_dirty(self.document):
                self.memory_dirty_paths.add(self.document.path.resolve())
            else:
                self.memory_dirty_paths.discard(self.document.path.resolve())
        self.refresh_diff()

    def show_about(self):
        messagebox.showinfo(
            "Crysis Save Editor",
            f"Crysis Save Editor {APP_VERSION}\n\nSupports Crysis Remastered, Crysis 2 Remastered, and Crysis 3 Remastered.\n\nGlobal Quick Search: Ctrl+K. Drag and drop uses TkDND; click Enable Drag & Drop if unavailable.\n\nSaving overwrites the open encrypted file after creating a .bak backup.",
        )

    def _on_close(self):
        if (getattr(self, "memory_task_active", False) or getattr(self, "memory_loading_active", False)
                or getattr(self, "batch_export_active", False)):
            messagebox.showwarning("Task Running", "A batch or RAM operation is running. Finish or cancel the task before closing.")
            return
        if not self._confirm_discard_changes(profile=True, save=True, ram=True, action="exit"):
            return
        self._save_settings()
        self._remove_windows_drop_target()
        pending_poll = getattr(self, "_batch_poll_after_id", None)
        if pending_poll is not None:
            try:
                self.root.after_cancel(pending_poll)
            except tk.TclError:
                pass
        self.root.destroy()


def main():
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except (ImportError, tk.TclError, OSError, RuntimeError) as exc:
        root = tk.Tk()
        root._crysis_dnd_startup_error = f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, ImportError):
            try:
                with (Path(__file__).resolve().parent / "logs" / "editor.log").open("a", encoding="utf-8") as log:
                    log.write(f"TkDND initialization skipped: {exc}\n")
            except OSError:
                pass
    CrysisSaveEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
