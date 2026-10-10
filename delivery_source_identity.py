"""Pinned text identities admit EOL-only delivery changes before fresh verification."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path
import subprocess


def text_identity(data):
    # No whitespace/BOM/encoding normalization. Binary and non-UTF8 inputs stay exact.
    if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", data):
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return {"byteHash": hashlib.sha256(data).hexdigest(),
            "textHash": hashlib.sha256(normalized).hexdigest()}


def file_identities(root, files):
    expected, identities = {}, {}
    for name in files:
        path = root / name
        data = path.read_bytes() if path.is_file() else None
        expected[name] = hashlib.sha256(data).hexdigest() if data is not None else None
        identity = text_identity(data) if data is not None else None
        if identity is not None:
            identities[name] = identity
    return expected, identities


def permits_text_eol(root, name):
    result = subprocess.run(["git", "-C", str(root), "check-attr", "-z",
                             "text", "filter", "working-tree-encoding", "--", name],
                            capture_output=True, timeout=120,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise RuntimeError("DELIVERY_GIT_ATTRIBUTES_FAILED")
    fields = result.stdout.split(b"\0")
    attributes = {fields[i+1]: fields[i+2] for i in range(0, len(fields)-1, 3)}
    return (attributes.get(b"text") != b"unset" and
            attributes.get(b"filter") in (b"unspecified", b"unset") and
            attributes.get(b"working-tree-encoding") in (b"unspecified", b"unset"))


class PreparedTextGuard:
    def __init__(self, root, identities, source_root):
        self.root = root
        self.identities = dict(identities)
        self.source_root = source_root
        self.source = None
        self.changes = []

    def admit(self, name, digest, data):
        identity = text_identity(data) if data is not None else None
        if digest is None or identity is None or not permits_text_eol(self.root, name):
            return False
        pinned = self.identities.get(name)
        if pinned is None:
            # Compatibility for an already prepared delivery. Never derive the
            # old identity from the agent's current tree or from Git's HEAD.
            if self.source is None:
                self.source = Path(self.source_root()).resolve()
            path = self.source / name
            if (not path.resolve().is_relative_to(self.source) or path.is_symlink() or
                    not path.is_file()):
                return False
            pinned = text_identity(path.read_bytes())
        if (pinned is None or pinned["byteHash"] != digest or
                pinned["textHash"] != identity["textHash"]):
            return False
        self.identities[name] = identity
        self.changes.append({"path": name, "before": digest, "after": identity["byteHash"]})
        return True
