"""One-time, idempotent install of a `vc` PATH alias for `voice-copilot`.

Never registered as a packaging entry point: pip/uv would create it
unconditionally on every install, which could silently shadow or conflict
with an unrelated `vc` the user already has. Instead this checks PATH once
at runtime and only acts if nothing is there.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from voice_copilot.core.config import config_path

log = logging.getLogger(__name__)

_CMD_SHIM = '@echo off\r\n"{target}" %*\r\n'
_SH_SHIM = '#!/usr/bin/env sh\nexec "{target}" "$@"\n'


def marker_path() -> Path:
    return config_path().parent / "vc-alias-checked"


def ensure_vc_alias(*, voice_copilot_script: Path | None = None) -> None:
    """Install a `vc` → `voice-copilot` shim once, if nothing else owns `vc`."""
    try:
        marker = marker_path()
        if marker.exists():
            return
        _ensure_vc_alias_unchecked(voice_copilot_script)
    except Exception as e:
        log.debug("vc alias install skipped: %s", e)
    finally:
        try:
            marker = marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
        except Exception as e:
            log.debug("could not write vc alias marker: %s", e)


def _ensure_vc_alias_unchecked(voice_copilot_script: Path | None) -> None:
    if shutil.which("vc") is not None:
        return

    script = voice_copilot_script or _locate_voice_copilot_script()
    if script is None:
        return

    if sys.platform == "win32":
        shim = script.parent / "vc.cmd"
        shim.write_text(_CMD_SHIM.format(target=script), encoding="utf-8")
    else:
        shim = script.parent / "vc"
        shim.write_text(_SH_SHIM.format(target=script), encoding="utf-8")
        shim.chmod(0o755)


def _locate_voice_copilot_script() -> Path | None:
    found = shutil.which("voice-copilot")
    return Path(found) if found else None
