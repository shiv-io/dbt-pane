from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

CLIPBOARD_COMMANDS = (
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
)


def copy_text(text: str, terminal_fallback: Callable[[str], None]) -> str:
    """Copy with the system clipboard tool, or ask the terminal to do it (OSC 52) over SSH."""
    for command in CLIPBOARD_COMMANDS:
        if not shutil.which(command[0]):
            continue
        try:
            completed = subprocess.run(command, input=text, text=True, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            return command[0]
    terminal_fallback(text)
    return "the terminal (OSC 52)"
