"""Console banners + alert sounds, and a logging filter that masks credentials."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("ems_swap")


class RedactSecrets(logging.Filter):
    """Defence in depth: never let LBS_USER / LBS_PASS reach a log line."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets = [v for v in (os.environ.get("LBS_PASS"), os.environ.get("LBS_USER")) if v]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self._secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging(log_file: Optional[Path], verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("ems_swap")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    redact = RedactSecrets()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(redact)
    root.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redact)
        root.addHandler(fh)


class Alerts:
    """Plays a sound (best effort, never blocks) and prints a big banner."""

    def __init__(self, sound: bool = True) -> None:
        self.sound = sound

    # kind: "success" | "failure" | "attention"
    def notify(self, kind: str, title: str, detail: str = "") -> None:
        bar = {"success": "=", "failure": "!", "attention": "*"}.get(kind, "*") * 78
        lines = [bar, f"  {title}"]
        if detail:
            lines += [f"  {line}" for line in detail.splitlines()]
        lines.append(bar)
        level = logging.ERROR if kind == "failure" else logging.WARNING if kind == "attention" else logging.INFO
        log.log(level, "\n" + "\n".join(lines))
        if self.sound:
            threading.Thread(target=self._play, args=(kind,), daemon=True).start()

    def success(self, title: str, detail: str = "") -> None:
        self.notify("success", title, detail)

    def failure(self, title: str, detail: str = "") -> None:
        self.notify("failure", title, detail)

    def attention(self, title: str, detail: str = "") -> None:
        self.notify("attention", title, detail)

    # ------------------------------------------------------------------------------
    def _play(self, kind: str) -> None:
        repeats = {"success": 2, "failure": 4, "attention": 3}.get(kind, 2)
        try:
            system = platform.system()
            if system == "Darwin":
                sound = {"success": "Glass", "failure": "Basso", "attention": "Sosumi"}.get(kind, "Ping")
                for _ in range(repeats):
                    subprocess.run(["afplay", f"/System/Library/Sounds/{sound}.aiff"], check=False, timeout=10)
                return
            if system == "Windows":
                import winsound  # type: ignore[import-not-found]

                freq = {"success": 1200, "failure": 400, "attention": 900}.get(kind, 800)
                for _ in range(repeats):
                    winsound.Beep(freq, 350)
                return
            cmd = self._linux_player()
            if cmd:
                for _ in range(repeats):
                    subprocess.run(cmd, check=False, timeout=10,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception:  # sound is best-effort
            pass
        for _ in range(repeats):
            sys.stdout.write("\a")
            sys.stdout.flush()

    @staticmethod
    def _linux_player() -> Optional[List[str]]:
        candidates = [
            ("paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
            ("aplay", "/usr/share/sounds/alsa/Front_Center.wav"),
        ]
        for exe, f in candidates:
            if shutil.which(exe) and Path(f).exists():
                return [exe, f]
        return None
