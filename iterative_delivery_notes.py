"""Explicit, resumable removal of this run's migration documentation/notes."""
from __future__ import annotations
import ast
import hashlib
import json
import subprocess
from pathlib import Path

DOCUMENT_NAMES = ("DEVELOPER_UPGRADE_GUIDE.md", "MIGRATION_REPORT.md")
LINE_COMMENTS = {ext: "//" for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".java")}
LINE_COMMENTS.update({ext: "/*" for ext in (".css", ".scss", ".less")})
LINE_COMMENTS.update({ext: "//" for ext in (".go", ".rs", ".kt", ".swift", ".php")})
LINE_COMMENTS[".rb"] = "#"
LINE_COMMENTS.update({ext: "#" for ext in (".py", ".ps1", ".sh", ".toml", ".yaml", ".yml")})


def without_migration_notes(data: bytes, name: str, run_id: str):
    prefix = LINE_COMMENTS.get(Path(name).suffix.lower())
    if not prefix:
        return data, 0
    # Only full-line notes bearing this exact run's marker are removable.
    # Ordinary comments and code with a trailing explanation remain intact.
    marker = prefix.encode() + b" DEPLOOM-MIGRATION-NOTE:" + run_id.encode() + b": "
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    lines = data[len(bom):].splitlines(keepends=True)
    def owned(line):
        return line.lstrip(b" \t").startswith(marker) and (prefix != "/*" or line.rstrip().endswith(b"*/"))
    kept = [line for line in lines if not owned(line)]
    return bom + b"".join(kept), len(lines) - len(kept)


def assert_comment_only_removal(before, after, name, config, state):
    """A marker inside a literal/data is not an authorized comment removal."""
    suffix = Path(name).suffix.lower()
    original, cleaned = before.decode("utf-8-sig"), after.decode("utf-8-sig")
    if suffix == ".py":
        equal = ast.dump(ast.parse(original)) == ast.dump(ast.parse(cleaned))
    elif suffix == ".toml":
        import tomllib
        equal = tomllib.loads(original) == tomllib.loads(cleaned)
    elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".yaml", ".yml"}:
        import iterative_migration as core
        node = (config.get("runtime") or {}).get("nodePath") or core.resolve_executable("node")
        if not node:
            raise RuntimeError("DELIVERY_CLEANUP_COMMENT_PARSER_UNAVAILABLE")
        script = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let normalized;
if (/\.ya?ml$/.test(input.name)) {
  try {
    const yaml = require(require.resolve('js-yaml', {paths: input.roots}));
    normalized = text => JSON.stringify(yaml.loadAll(text));
  } catch {
    const yaml = require(require.resolve('yaml', {paths: input.roots}));
    normalized = text => JSON.stringify(yaml.parseAllDocuments(text).map(doc => {
      if (doc.errors.length) throw new Error('invalid YAML');
      return doc.toJSON();
    }));
  }
} else if (input.name.endsWith('.css')) {
  const postcss = require(require.resolve('postcss', {paths: input.roots}));
  function semantic(node) {
    const value = {};
    for (const key of ['type','selector','name','params','prop','value','important']) {
      if (node[key] !== undefined) value[key] = node[key];
    }
    if (node.nodes) value.nodes = node.nodes.filter(child => child.type !== 'comment').map(semantic);
    return value;
  }
  normalized = text => JSON.stringify(semantic(postcss.parse(text)));
} else {
  const ts = require(require.resolve('typescript', {paths: input.roots}));
  const printer = ts.createPrinter({removeComments: true});
  normalized = text => {
    const file = ts.createSourceFile(input.name, text, ts.ScriptTarget.Latest, true);
    if (file.parseDiagnostics.length) throw new Error('invalid syntax');
    return printer.printFile(file);
  };
}
process.stdout.write(JSON.stringify({equal: normalized(input.before) === normalized(input.after)}));
"""
        roots = [str(Path(state.get("verificationWorkspace") or state["workspaceRoot"]) / state.get("projectRelative", ".")),
                 str(Path(state["workspaceRoot"]) / state.get("projectRelative", ".")),
                 str(Path(__file__).resolve().parent / "desktop")]
        try:
            result = subprocess.run([str(node), "-e", script], input=json.dumps({"name": str(Path(name).with_suffix(suffix)), "before": original, "after": cleaned, "roots": roots}),
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            if result.returncode:
                raise RuntimeError("DELIVERY_CLEANUP_COMMENT_PARSER_UNAVAILABLE: TypeScript/PostCSS/YAML parser or valid syntax required")
            equal = json.loads(result.stdout)["equal"] is True
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise RuntimeError("DELIVERY_CLEANUP_COMMENT_PARSER_UNAVAILABLE") from exc
    else:
        raise RuntimeError(f"DELIVERY_CLEANUP_UNSUPPORTED_COMMENT_LANGUAGE: {name}")
    if not equal:
        raise RuntimeError(f"DELIVERY_CLEANUP_MARKER_IN_CODE_OR_DATA: {name}")


def validate_notes_archive(run_dir, state):
    import iterative_delivery as delivery
    notes = state["cleanup"]
    archive = run_dir / "delivery" / "notes-archive"
    if Path(notes["archiveRoot"]).resolve() != archive.resolve() or not delivery.contained(run_dir, archive):
        raise RuntimeError("DELIVERY_CLEANUP_ARCHIVE_OUTSIDE_RUN")
    for change in notes["changes"]:
        if change["kind"] != "document":
            continue
        path = archive / Path(change["path"]).name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != notes["before"][change["path"]]:
            raise RuntimeError("DELIVERY_CLEANUP_ARCHIVE_CHANGED")


def cleanup_prepare(run_dir, inputs):
    import iterative_delivery as delivery
    import iterative_migration as core
    path = run_dir / "delivery-state.json"
    state = delivery.read(path)
    run = core.load_run(run_dir)
    config = core.load_config(run_dir)
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    if (run.get("phase") != "TERMINAL" or run.get("activeCandidateId") or
            state.get("status") != "done" or state.get("runId") != run["runId"] or
            state.get("checkpointId") != checkpoint["checkpointId"] or
            state.get("sourceSnapshotKey") != checkpoint["sourceSnapshotKey"]):
        raise RuntimeError("DELIVERY_CLEANUP_REQUIRES_VERIFIED_BRANCH")
    root = Path(state["workspaceRoot"]).resolve()
    if root != delivery.delivery_root(run_dir, run["runId"]).resolve():
        raise RuntimeError("DELIVERY_PATH_OUTSIDE_RUN")
    if delivery.git(root, "branch", "--show-current") != state["branch"]:
        raise RuntimeError("DELIVERY_BRANCH_CHANGED")
    previous = state.get("cleanup")
    if previous:
        validate_notes_archive(run_dir, state)
        if previous.get("status") == "done" and (delivery.git(root, "rev-parse", "HEAD") != state["head"] or delivery.git(root, "status", "--porcelain")):
            raise RuntimeError("DELIVERY_CLEANUP_BRANCH_CHANGED: preserve user edits")
        # Resume only the original or exactly prepared bytes, preserving user edits.
        delivery.git(root, "merge-base", "--is-ancestor", previous["baseHead"], "HEAD")
        from delivery_source_identity import PreparedTextGuard
        def missing_cleanup_identity():
            raise RuntimeError("DELIVERY_CLEANUP_TEXT_IDENTITY_MISSING")
        text_guard = PreparedTextGuard(root, previous.get("textIdentities", {}), missing_cleanup_identity)
        for name, digest in previous["expected"].items():
            target = root / name
            if not delivery.contained(root, target) or target.is_symlink():
                raise RuntimeError(f"DELIVERY_INVALID_PATH: {name}")
            data = target.read_bytes() if target.is_file() else None
            actual = hashlib.sha256(data).hexdigest() if data is not None else None
            allowed = {digest} if previous.get("status") == "done" else {previous["before"].get(name), digest}
            if actual not in allowed:
                if text_guard.admit(name, digest, data):
                    continue
                comment_change = any(change["path"] == name and change["kind"] == "comments" for change in previous["changes"])
                if previous.get("status") == "ready" and comment_change and data is not None:
                    stripped, _count = without_migration_notes(data, name, state["runId"])
                    if hashlib.sha256(stripped).hexdigest() == digest or text_guard.admit(name, digest, stripped):
                        assert_comment_only_removal(data, stripped, name, config, state)
                        continue
                raise RuntimeError(f"DELIVERY_CLEANUP_BYTES_CHANGED: {name}")
        unexpected = set(delivery.git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""} - set(previous["expected"])
        tracked = set(delivery.git(root, "ls-files", "-z").split("\0")) - {""}
        if unexpected or not tracked.issubset(set(previous["expected"])):
            raise RuntimeError("DELIVERY_CLEANUP_UNEXPECTED_FILES")
        return state
    if delivery.git(root, "rev-parse", "HEAD") != state["head"] or delivery.git(root, "status", "--porcelain"):
        raise RuntimeError("DELIVERY_CLEANUP_BRANCH_CHANGED: preserve user edits")
    expected = {**state["expected"], **state.get("documentationHashes", {})}
    before = dict(expected)
    archive = run_dir / "delivery" / "notes-archive"
    documents = {f"docs/dependency-migration/{state['runId']}/{name}" for name in DOCUMENT_NAMES}
    changes = []
    from delivery_source_identity import text_identity
    identities = {}
    for name, digest in expected.items():
        target = root / name
        if not delivery.contained(root, target) or target.is_symlink():
            raise RuntimeError(f"DELIVERY_INVALID_PATH: {name}")
        data = target.read_bytes() if target.is_file() else None
        actual = hashlib.sha256(data).hexdigest() if data is not None else None
        if actual != digest:
            raise RuntimeError(f"DELIVERY_CLEANUP_BYTES_CHANGED: {name}")
        if name in documents and data is not None:
            saved = archive / Path(name).name
            if not delivery.contained(run_dir, saved) or saved.is_symlink():
                raise RuntimeError("DELIVERY_CLEANUP_ARCHIVE_OUTSIDE_RUN")
            saved.parent.mkdir(parents=True, exist_ok=True)
            if saved.exists() and saved.read_bytes() != data:
                raise RuntimeError("DELIVERY_CLEANUP_ARCHIVE_CHANGED")
            if not saved.exists():
                temporary = saved.with_suffix(saved.suffix + ".tmp")
                temporary.write_bytes(data)
                temporary.replace(saved)
            expected[name] = None
            changes.append({"path": name, "kind": "document"})
        elif data is not None:
            suffix = Path(name).suffix.lower()
            if suffix not in LINE_COMMENTS and suffix not in {".md", ".json", ".lock"}:
                marker = b" DEPLOOM-MIGRATION-NOTE:" + state["runId"].encode() + b": "
                if any(line.lstrip(b" \t").startswith(prefix + marker) for line in data.splitlines()
                       for prefix in (b"//", b"#", b"/*", b"<!--", b";", b"--")):
                    raise RuntimeError(f"DELIVERY_CLEANUP_UNSUPPORTED_COMMENT_LANGUAGE: {name}")
            stripped, count = without_migration_notes(data, name, state["runId"])
            if count:
                if delivery.git(root, "ls-tree", "--name-only", state["sourceHead"], "--", name):
                    baseline = delivery.git(root, "show", f"{state['sourceHead']}:{name}").encode("utf-8")
                    if without_migration_notes(baseline, name, state["runId"])[1]:
                        raise RuntimeError(f"DELIVERY_CLEANUP_PREEXISTING_MARKER: {name}")
                assert_comment_only_removal(data, stripped, name, config, state)
                expected[name] = hashlib.sha256(stripped).hexdigest()
                changes.append({"path": name, "kind": "comments", "count": count})
                data = stripped
        if expected[name] is not None and data is not None:
            identity = text_identity(data)
            if identity is not None:
                identities[name] = identity
    state["cleanup"] = {"status": "ready", "baseHead": state["head"], "expected": expected, "before": before,
                        "changes": changes, "archiveRoot": str(archive), "textIdentities": identities,
                        "commentCount": sum(change.get("count", 0) for change in changes)}
    core._write_json_atomic(path, state)
    return state


def cleanup_verify(run_dir, inputs):
    import iterative_delivery as delivery
    state = delivery.read(run_dir / "delivery-state.json")
    if not state.get("cleanup"):
        raise RuntimeError("DELIVERY_CLEANUP_NOT_PREPARED")
    return delivery.delivery_verify(run_dir, inputs, cleanup=True)
