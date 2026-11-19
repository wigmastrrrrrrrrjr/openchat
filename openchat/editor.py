"""Open the user's editor for a long, multi-line prompt."""

import os
import subprocess
import tempfile
from pathlib import Path


def compose(tmp):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    suffix = ".md"
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="prompt-", suffix=suffix, dir=str(tmp))
    os.close(fd)
    path = Path(name)
    path.write_text("")
    try:
        subprocess.call([*editor.split(), str(path)])
        text = path.read_text(errors="replace").strip()
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return text if text else None
