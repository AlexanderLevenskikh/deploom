"""Pinned text identities admit EOL-only delivery changes before fresh verification."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path
import subprocess


def text_identity(data):
    # Preserve encoding/BOM and every non-EOL byte. No transcoding or trimming.
    encoding = "utf-8"
    for bom, codec in ((b"\xff\xfe\0\0", "utf-32-le"), (b"\0\0\xfe\xff", "utf-32-be"),
                       (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                text = data[len(bom):].decode(codec)
                if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
                    return None
                normalized = bom + text.replace("\r\n", "\n").replace("\r", "\n").encode(codec)
                encoding = codec
            except UnicodeError:
                return None
            break
    else:
        if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", data):
            return None
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "byte-text"
        # ASCII-compatible 8-bit encodings (e.g. Windows-1251) can compare
        # newline bytes directly; every other byte remains authoritative.
        normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return {"byteHash": hashlib.sha256(data).hexdigest(),
            "textHash": hashlib.sha256(normalized).hexdigest(), "encoding": encoding}


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


TEXT_SUFFIXES = {".sql", ".txt", ".md", ".csv", ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs",
                 ".json", ".lock", ".xml", ".cs", ".py", ".sh", ".ps1", ".yaml", ".yml", ".ini",
                 ".config", ".props", ".targets", ".sln", ".csproj", ".html", ".css", ".scss", ".resx"}


def text_attributes(root, names):
    if not names:
        return {}
    result = subprocess.run(["git", "-C", str(root), "check-attr", "-z", "--stdin",
                             "text", "filter", "working-tree-encoding"],
                            input=b"".join(name.encode("utf-8") + b"\0" for name in names),
                            capture_output=True, timeout=120,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise RuntimeError("DELIVERY_GIT_ATTRIBUTES_FAILED")
    fields = result.stdout.split(b"\0")
    if fields[-1] != b"" or (len(fields)-1) % 3:
        raise RuntimeError("DELIVERY_GIT_ATTRIBUTES_FAILED")
    values = {}
    for index in range(0, len(fields)-1, 3):
        values.setdefault(fields[index].decode("utf-8"), {})[fields[index+1]] = fields[index+2]
    if set(values) != set(names):
        raise RuntimeError("DELIVERY_GIT_ATTRIBUTES_FAILED")
    return values


def permits_text_eol(name, identity, attributes):
    if (attributes.get(b"text") == b"unset" or
            attributes.get(b"filter") not in (b"unspecified", b"unset") or
            attributes.get(b"working-tree-encoding") not in (b"unspecified", b"unset")):
        return False
    # Opaque 8-bit data needs a recognized text filename or explicit Git text
    # declaration; arbitrary non-UTF8 binary payloads do not gain EOL tolerance.
    return (identity["encoding"] != "byte-text" or attributes.get(b"text") in (b"set", b"auto") or
            Path(name).suffix.lower() in TEXT_SUFFIXES or Path(name).name in {".gitignore", ".gitattributes", ".editorconfig"})


class PreparedTextGuard:
    def __init__(self, root, identities, source_root):
        self.root = root
        self.identities = dict(identities)
        self.source_root = source_root
        self.source = None
        self.changes = []
        self.attributes = {}

    def prime(self, names):
        self.attributes.update(text_attributes(self.root, names))

    def admit(self, name, digest, data):
        identity = text_identity(data) if data is not None else None
        if digest is None or identity is None:
            return False
        if name not in self.attributes:
            self.prime([name])
        if not permits_text_eol(name, identity, self.attributes[name]):
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
