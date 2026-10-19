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


def assert_committed_files(root, expected, gitlinks=(), *, commit='HEAD'):
    """Compare physical delivery bytes to HEAD blobs independently of stat flags."""
    root = Path(root)
    result = subprocess.run(['git', '-C', str(root), 'ls-tree', '-rlz', commit],
        capture_output=True, timeout=120, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'})
    if result.returncode:
        raise RuntimeError('DELIVERY_COMMITTED_TREE_UNAVAILABLE')
    tree = {}
    for row in result.stdout.split(b'\0'):
        if not row: continue
        metadata, name = row.split(b'\t', 1)
        mode, kind, oid, size = metadata.split()
        tree[name.decode('utf-8')] = (mode, kind, oid, int(size) if kind == b'blob' else 0)
    names = [name for name in expected if name not in gitlinks]
    selected = [name for name in names if expected[name] is not None]
    if any(name not in tree or tree[name][1] != b'blob' for name in selected):
        raise RuntimeError('DELIVERY_COMMITTED_TREE_MISMATCH: missing/non-blob input')
    if any(name in tree for name in names if expected[name] is None):
        raise RuntimeError('DELIVERY_COMMITTED_TREE_MISMATCH: deletion not committed')
    attributes = text_attributes(root, selected)
    for batch, blob_bytes in _committed_blob_batches(root, tree, selected):
        offset = 0
        for name in batch:
            end = blob_bytes.find(b'\n', offset)
            header = blob_bytes[offset:end].split()
            if end < 0 or len(header) != 3 or header[1] != b'blob':
                raise RuntimeError('DELIVERY_COMMITTED_TREE_UNAVAILABLE')
            size = int(header[2]); start = end + 1
            committed = blob_bytes[start:start+size]; offset = start+size+1
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('DELIVERY_COMMITTED_TREE_MISMATCH: ' + name)
            actual = path.read_bytes()
            if hashlib.sha256(actual).hexdigest() != expected[name]:
                raise RuntimeError('DELIVERY_BYTES_CHANGED_DURING_VERIFICATION: ' + name)
            if actual == committed: continue
            # LFS commits a pointer, while validation consumes the physical object.
            # Prove the object's digest and size without running arbitrary filters.
            if attributes[name].get(b"filter") == b"lfs":
                pointer = re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\n"
                    rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n", committed)
                if (pointer and pointer[1].decode('ascii') == expected[name]
                        and int(pointer[2]) == len(actual)):
                    continue
            left, right = text_identity(committed), text_identity(actual)
            if (left is None or right is None or left['textHash'] != right['textHash']
                    or not permits_text_eol(name, right, attributes[name])):
                raise RuntimeError('DELIVERY_COMMITTED_TREE_MISMATCH: ' + name)
        if offset != len(blob_bytes): raise RuntimeError('DELIVERY_COMMITTED_TREE_UNAVAILABLE')


def _committed_blob_batches(root, tree, selected):
    # Bound cumulative output, retaining a per-subprocess timeout. A single
    # oversized object gets its own batch rather than accumulating other blobs.
    def read(names):
        result = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
            input=b''.join(tree[name][2] + b'\n' for name in names), capture_output=True,
            timeout=120, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'})
        if result.returncode:
            raise RuntimeError('DELIVERY_COMMITTED_TREE_UNAVAILABLE')
        return names, result.stdout
    batch, size = [], 0
    for name in selected:
        if batch and (len(batch) >= 256 or size + tree[name][3] > 8 * 1024 * 1024):
            yield read(batch)
            batch, size = [], 0
        batch.append(name)
        size += tree[name][3]
    if batch:
        yield read(batch)


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
