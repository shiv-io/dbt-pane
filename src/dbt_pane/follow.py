from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class FollowSource:
    """Reports the folder of the pane the user is working in, skipping the panel's own pane."""

    name = "none"

    def current_dir(self) -> Path | None:
        raise NotImplementedError


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


class HerdrFocus(FollowSource):
    name = "Herdr"

    def __init__(self) -> None:
        self.herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"
        self.own_pane = os.environ.get("HERDR_PANE_ID")

    def current_dir(self) -> Path | None:
        output = _run([self.herdr, "pane", "list"])
        if not output:
            return None
        try:
            panes = json.loads(output)["result"]["panes"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        for pane in panes:
            if pane.get("focused") and pane.get("pane_id") != self.own_pane:
                folder = pane.get("foreground_cwd") or pane.get("cwd")
                return Path(folder) if folder else None
        return None


class TmuxFocus(FollowSource):
    name = "tmux"

    def __init__(self) -> None:
        self.own_pane = os.environ.get("TMUX_PANE")

    def current_dir(self) -> Path | None:
        target = ["-t", self.own_pane] if self.own_pane else []
        session = _run(["tmux", "display-message", "-p", *target, "#{session_id}"])
        if not session:
            return None
        output = _run(
            [
                "tmux",
                "list-panes",
                "-s",
                "-t",
                session.strip(),
                "-F",
                "#{window_active}#{pane_active} #{pane_id} #{pane_current_path}",
            ]
        )
        for line in (output or "").splitlines():
            flags, pane_id, folder = (line.split(" ", 2) + ["", ""])[:3]
            if flags == "11" and pane_id != self.own_pane and folder:
                return Path(folder)
        return None


def detect_follow_source() -> FollowSource | None:
    if os.environ.get("HERDR_ENV") == "1":
        return HerdrFocus()
    if os.environ.get("TMUX"):
        return TmuxFocus()
    return None
