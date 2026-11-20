"""Start Crysis Save Editor without a console and record startup failures."""
from pathlib import Path
import os
import sys
import traceback
from datetime import datetime

if os.name == "nt" and not sys.executable.lower().endswith("pythonw.exe"):
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        import subprocess
        subprocess.Popen([str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sys.exit(0)

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
sys.path.insert(0, str(BASE))
try:
    from crysis_save_editor import main
    main()
except BaseException:
    log_dir = BASE / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "editor.log").open("a", encoding="utf-8") as log:
            log.write("\n=== Startup error: " + datetime.now().isoformat() + " ===\n")
            traceback.print_exc(file=log)
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Crysis Save Editor", "Unable to start the editor. Check logs/editor.log for details.")
        root.destroy()
    except Exception:
        pass
    sys.exit(1)
