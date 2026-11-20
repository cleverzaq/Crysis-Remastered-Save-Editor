"""GUI-independent safeguards for changing active documents and exporting backups."""
from __future__ import annotations
import shutil
from pathlib import Path
from save_editor_core import _xml_signature, SaveDocument, SaveEditorError


def document_is_dirty(document: SaveDocument | None) -> bool:
    """Check actual XML contents instead of a UI label that can be reset."""
    return bool(document is not None and document.baseline_root is not None
                and _xml_signature(document.root) != _xml_signature(document.baseline_root))


def export_slot_backup(source: Path | str, destination_folder: Path | str) -> tuple[Path, ...]:
    """Export intact CSF and matching sidecars OUTSIDE SaveGames, not a playable slot.

    No renamed _copy.CSF that the game may reject. All original filenames and
    sidecars are preserved. Existing targets never get overwritten.
    """
    source, folder = Path(source).expanduser().resolve(), Path(destination_folder).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != '.csf':
        raise SaveEditorError('Choose an existing .CSF file.')
    if not folder.is_dir():
        raise SaveEditorError('Choose an existing backup folder.')
    if any(part.casefold() == 'savegames' for part in folder.parts):
        raise SaveEditorError('Choose a backup folder outside SaveGames. These copies are not new playable slots.')
    # A full playable-slot backup must include its matching .meta.  The .bmp
    # thumbnail only exists for some game versions and is optional.
    meta = source.with_suffix('.meta')
    if not meta.is_file():
        raise SaveEditorError(f'Missing matching .meta file: {meta.name}. Full slot backup was not created.')
    originals = [source, meta]
    thumbnail = source.with_suffix('.bmp')
    if thumbnail.is_file():
        originals.append(thumbnail)
    targets = [folder / p.name for p in originals]
    if any(p.exists() for p in targets):
        raise SaveEditorError('Backup target already exists. Choose an empty backup folder; nothing was overwritten.')
    created = []
    try:
        for original, destination in zip(originals, targets):
            # Exclusive creation prevents a concurrent operation from replacing
            # an existing backup between the earlier check and this copy.
            with original.open('rb') as source_stream:
                with destination.open('xb') as dest_stream:
                    created.append(destination)
                    shutil.copyfileobj(source_stream, dest_stream)
            shutil.copystat(original, destination)
    except OSError as exc:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise SaveEditorError(f'Unable to export slot backup: {exc}') from exc
    return tuple(created)
