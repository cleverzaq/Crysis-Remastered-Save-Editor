"""Locate save files for all three Crysis Remastered games without fixed user paths.

The game is inferred from the opened profile when possible; a selected EXE's
verified magic can also be supplied by the GUI. Nothing is created or changed.
"""
from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

GAME_FOLDERS = ("CrysisRemastered", "Crysis2Remastered", "Crysis3Remastered")


def game_from_path(path: Path | str | None) -> str | None:
    """Recognize a game root in any ancestor of an open profile/save path."""
    if path is None:
        return None
    for part in reversed(Path(path).parts):
        for game in GAME_FOLDERS:
            if part.casefold() == game.casefold():
                return game
    return None



def windows_saved_games_folder() -> Path | None:
    """Return Windows' actual Saved Games known folder, including redirects."""
    if sys.platform != "win32":
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
        ]

    # FOLDERID_SavedGames documented by Microsoft (not a username/path).
    folder_id = GUID.from_buffer_copy(uuid.UUID("4C5C32FF-BB9D-43B0-BF9D-DFD27F23D51B").bytes_le)
    pointer = ctypes.c_void_p()
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer))
        if result == 0 and pointer.value:
            return Path(ctypes.wstring_at(pointer.value))
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if pointer.value:
            ctypes.windll.ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
            ctypes.windll.ole32.CoTaskMemFree(pointer)
    return None


def candidate_savegame_dirs(
    profile_path: Path | str | None,
    game: str | None = None,
    *,
    saved_games: Path | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Ordered candidate folders; no directory creation or filesystem writes."""
    candidates: list[Path] = []

    def add(folder: Path | None) -> None:
        if folder is None:
            return
        folder = Path(folder).expanduser()
        if folder not in candidates:
            candidates.append(folder)

    inferred = game_from_path(profile_path)
    selected = game or inferred
    if selected is not None and selected not in GAME_FOLDERS:
        raise ValueError(f"Unsupported Crysis save directory: {selected}")

    # A live profile path has priority even for redirected/moved Saved Games.
    # Do not adopt an unrelated game's directory if the selected EXE disagrees.
    if profile_path:
        path = Path(profile_path).expanduser()
        directory = path if path.is_dir() else path.parent
        if selected is None or inferred is None or selected == inferred:
            for parent in (directory, *directory.parents):
                if parent.name.casefold() == "savegames":
                    add(parent)
                    break
                if parent.name.casefold() in {g.casefold() for g in GAME_FOLDERS}:
                    add(parent / "SaveGames")
                    break
                if parent.name.casefold() == "profiles":
                    add(parent.parent / "SaveGames")
                    break

    known = saved_games if saved_games is not None else windows_saved_games_folder()
    user_home = Path(home) if home is not None else Path(os.environ.get("USERPROFILE") or Path.home())
    # Without a profile or known game, scan the three supported names rather
    # than hardcode Crysis 3; never pick an absent directory as the answer.
    games = (selected,) if selected else GAME_FOLDERS
    for target in games:
        if known is not None:
            add(Path(known) / target / "SaveGames")
        add(user_home / "Saved Games" / target / "SaveGames")
    return candidates


def is_profile_directory(folder: Path | str) -> bool:
    """Distinguish Profiles/default from a folder intended for .CSF files."""
    path = Path(folder)
    return any(part.casefold() == "profiles" for part in path.parts) and not any(
        part.casefold() == "savegames" for part in path.parts
    )


def resolve_savegame_dir(
    profile_path: Path | str | None,
    configured_folder: Path | str | None = None,
    game: str | None = None,
    *,
    saved_games: Path | None = None,
    home: Path | None = None,
) -> Path | None:
    """Respect a valid custom selection, but fix a mistakenly selected profile dir."""
    configured = Path(configured_folder).expanduser() if configured_folder else None
    if configured and configured.is_dir() and not is_profile_directory(configured):
        # A previous run may have persisted Crysis 3's SaveGames folder. It
        # must not override an opened Crysis 1/2 profile. Custom folders with
        # no known Crysis game ancestor are still respected.
        expected = game or game_from_path(profile_path)
        configured_game = game_from_path(configured)
        if expected is None or configured_game is None or configured_game == expected:
            return configured
    for folder in candidate_savegame_dirs(profile_path, game, saved_games=saved_games, home=home):
        if folder.is_dir():
            return folder
    return None


def profile_name_from_path(profile_path: Path | str | None, *, fallback: str = "default") -> str:
    """Get the game profile name from .../Profiles/<name>/attributes.xml.

    The encrypted XML does not contain the profile folder name. If the editor
    loads a detached XML copy, use the standard single-player profile name.
    """
    if profile_path:
        path = Path(profile_path)
        directory = path if path.is_dir() else path.parent
        if directory.parent.name.casefold() == "profiles" and directory.name not in ("", ".", ".."):
            return directory.name
    return fallback


def save_file_candidates(filename: str, profile_name: str | None = None) -> tuple[str, ...]:
    """Physical filenames to look for, preferring the profile-prefixed file.

    Singleplayer.LastSavedGame stores e.g. '81_Cave_1288.CSF', while the
    SaveGames directory stores 'default_81_Cave_1288.CSF'. Never alter XML.
    Some copies have unprefixed filenames, so retain that fallback.
    """
    if not isinstance(filename, str) or not filename or filename != Path(filename).name or "\\" in filename:
        return ()
    if profile_name and profile_name not in (".", "..") and all(ch not in profile_name for ch in "/\\"):
        prefix = profile_name + "_"
        if not filename.casefold().startswith(prefix.casefold()):
            return (prefix + filename, filename)
    return (filename,)


def find_save_file(
    folder: Path, filename: str, *, recursive: bool = True, profile_name: str | None = None
) -> Path | None:
    """Find an existing save, resolving the profile prefix without renaming files.

    If a profile name is supplied, search for profile_filename first, then for
    the unprefixed filename (older exports or manually copied saves).
    """
    folder = Path(folder)
    candidates = save_file_candidates(filename, profile_name)
    if not folder.is_dir() or not candidates:
        return None
    for candidate in candidates:
        direct = folder / candidate
        if direct.is_file():
            return direct
    # Preserve candidate preference even when one is in a subdirectory.
    for candidate in candidates:
        wanted = candidate.casefold()
        files = folder.rglob("*") if recursive else folder.iterdir()
        match = next((p for p in files if p.is_file() and p.name.casefold() == wanted), None)
        if match is not None:
            return match
    return None


def classify_save_filename(filename: str) -> str:
    """Classify CSF filenames for all three Remastered titles.

    Checkpoints contain a number, level (which may itself contain underscores),
    and a final numeric checkpoint marker. Quicksaves and chapter saves have
    separate patterns; profile prefixes need not be hardcoded or stripped.
    """
    import re
    name = Path(filename).name
    if not name.lower().endswith('.csf'):
        return 'Other'
    stem = name[:-4]
    if re.search(r'(?:^|_)\d+_quicksave$', stem, flags=re.IGNORECASE):
        return 'Quicksave'
    if re.search(r'(?:^|_)\d+_.+_\d+$', stem):
        return 'Checkpoint'
    if stem.casefold().endswith('_crysis'):
        return 'Chapter'
    return 'Other'
