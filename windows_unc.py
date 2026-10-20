"""Preserve a Windows shell command's working directory on UNC shares."""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Mapping, Sequence

_bootstrap: Path | None = None
_bootstrap_lock = threading.Lock()


def prepare_unc_shell(
    argv: Sequence[str], cwd: Path, environment: Mapping[str, str],
) -> tuple[list[str], Path, dict[str, str]]:
    """Use pushd only for cmd.exe; native executables support UNC cwd directly.

    Keep the original command in a nested cmd so batch shims return to the
    bootstrap. Save the real exit status before popd and retain tree supervision.
    """
    global _bootstrap
    args = list(argv)
    env = dict(environment)
    if not (os.name == "nt" and str(cwd).startswith(("\\\\", "//"))
            and len(args) >= 3 and Path(args[0]).name.lower() in {"cmd", "cmd.exe"}
            and args[-2].lower() == "/c"):
        return args, cwd, env
    with _bootstrap_lock:
        if _bootstrap is None:
            directory = Path(tempfile.mkdtemp(prefix="deploom-unc-"))
            _bootstrap = directory / "run.cmd"
            _bootstrap.write_bytes((
                '@echo off\r\n'
                'setlocal DisableDelayedExpansion\r\n'
                'pushd "%DEPLOOM_UNC_CWD%"\r\n'
                'if errorlevel 1 exit /b 1\r\n'
                '"%ComSpec%" /d /s /c %DEPLOOM_UNC_COMMAND%\r\n'
                'set "DEPLOOM_UNC_EXIT=%errorlevel%"\r\n'
                'popd\r\n'
                'exit /b %DEPLOOM_UNC_EXIT%\r\n'
            ).encode("ascii"))
            atexit.register(shutil.rmtree, directory, ignore_errors=True)
    env.update(DEPLOOM_UNC_CWD=str(cwd), DEPLOOM_UNC_COMMAND='"' + args[-1] + '"')
    env["ComSpec"] = env.get("ComSpec") or env.get("COMSPEC") or args[0]
    return [args[0], "/d", "/s", "/c", '"' + str(_bootstrap) + '"'], _bootstrap.parent, env
