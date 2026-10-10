"""Distinguish stale Git stat/EOL status from uncommitted content without rewriting files."""
import subprocess
import os
from pathlib import Path


def committed_worktree(root):
    def run(*args, input=None):
        result = subprocess.run(["git", "-C", str(root), *args], input=input, capture_output=True,
                                timeout=120, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if result.returncode: raise RuntimeError("DELIVERY_GIT_STATUS_FAILED")
        return result.stdout
    status = run("status", "--porcelain=v1", "-z")
    if not status: return True
    names = []
    for record in status.split(b"\0"):
        if not record: continue
        if record[:3] != b" M ": return False
        name = record[3:]
        if b"\n" in name or b"\r" in name: return False
        names.append(name)
    # No actual Git diff: only stat-cache/EOL noise may reach byte comparison.
    if run("diff", "--name-only", "-z", "HEAD", "--"): return False
    blobs = run("cat-file", "--batch", input=b"".join(b"HEAD:" + n + b"\n" for n in names))
    offset = 0
    for name in names:
        end = blobs.find(b"\n", offset)
        header = blobs[offset:end].split()
        if end < 0 or len(header) != 3 or header[1] != b"blob": return False
        size = int(header[2]); start = end + 1
        committed = blobs[start:start + size]; offset = start + size + 1
        path = Path(root) / name.decode("utf-8")
        if path.is_symlink() or not path.is_file(): return False
        actual = path.read_bytes()
        if actual != committed:
            if b"\0" in actual or b"\0" in committed: return False
            if actual.replace(b"\r\n", b"\n") != committed.replace(b"\r\n", b"\n"): return False
    return offset == len(blobs)
