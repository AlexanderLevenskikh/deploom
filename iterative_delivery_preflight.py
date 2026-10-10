"""Fast complete delivery comparison; never grants project verification authority."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
from delivery_source_identity import PreparedTextGuard, text_identity


def scan_files(root, expected, identities, source_root, documentation=()):
    started = time.monotonic()
    root = Path(root).resolve()
    expected = dict(expected)
    guard = PreparedTextGuard(root, identities, source_root)
    changes, blocked = [], []
    for name, digest in sorted(expected.items()):
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            blocked.append({"path": name, "reason": "DELIVERY_INVALID_PATH"})
            continue
        try:
            data = path.read_bytes() if path.is_file() else None
        except OSError:
            blocked.append({"path": name, "reason": "DELIVERY_FILE_UNREADABLE"})
            continue
        current = hashlib.sha256(data).hexdigest() if data is not None else None
        if name in documentation:
            try:
                valid = data is not None and data.decode("utf-8-sig").strip()
            except UnicodeError:
                valid = False
            if not valid:
                blocked.append({"path": name, "reason": "DELIVERY_DOCUMENTATION_MISSING"})
                continue
            expected[name] = current
            identity = text_identity(data)
            if identity is not None:
                guard.identities[name] = identity
        elif current != digest:
            changes.append((name, digest, data, current))
    guard.prime([name for name, *_ in changes])
    for name, digest, data, current in changes:
        try:
            accepted = guard.admit(name, digest, data)
        except (OSError, RuntimeError) as error:
            blocked.append({"path": name, "reason": "DELIVERY_REFERENCE_UNAVAILABLE", "detail": str(error)[:300]})
            continue
        if not accepted:
            blocked.append({"path": name, "reason": "DELIVERY_SOURCE_CHANGED"})
        else:
            expected[name] = current
    report = {"schemaVersion": 1, "status": "blocked" if blocked else "ready", "checkedFiles": len(expected),
              "lineEndingChangeCount": len(guard.changes), "lineEndingChanges": guard.changes,
              "blockedChangeCount": len(blocked), "blockedChanges": blocked,
              "elapsedSeconds": round(time.monotonic()-started, 3),
              "generatedAt": datetime.now(timezone.utc).isoformat(),
              "authority": "comparison-only; fresh project checks and audit still required"}
    return expected, guard, report


def inspect_delivery(run_dir, state, checkpoint, *, cleanup=False):
    import iterative_migration as core
    notes = state.get("cleanup") or {}
    cleaned = cleanup or notes.get("status") == "done"
    root = Path(state["workspaceRoot"])
    expected = notes["expected"] if cleanup else state["expected"]
    def reference_root():
        if cleaned:
            raise RuntimeError("DELIVERY_CLEANUP_TEXT_IDENTITY_MISSING")
        # The prepared raw hash pins each old reference independently. Reading
        # that file is sufficient for comparison and must not reopen/reverify
        # an entire historical checkpoint with a new verifier build.
        tree = Path(checkpoint["sourceSnapshotContainer"]) / "tree"
        if not tree.is_dir():
            raise RuntimeError("DELIVERY_REFERENCE_TREE_UNAVAILABLE")
        return tree
    documentation = set() if cleaned else {f"docs/dependency-migration/{state['runId']}/{name}" for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md")}
    expected, guard, report = scan_files(root, expected, (notes if cleanup else state).get("textIdentities", {}), reference_root, documentation)
    report.update(runId=state["runId"], checkpointId=checkpoint["checkpointId"], sourceSnapshotKey=checkpoint["sourceSnapshotKey"])
    report_path = Path(run_dir) / "delivery" / "preflight.json"
    report["evidenceRef"] = str(report_path)
    core._write_json_atomic(report_path, report)
    if report["blockedChanges"]:
        reasons = sorted({item["reason"] for item in report["blockedChanges"]})
        names = "; ".join(item["path"] for item in report["blockedChanges"][:6])
        raise RuntimeError(f"DELIVERY_SOURCE_CHANGED: {report['blockedChangeCount']} blocked files ({', '.join(reasons)}): {names}; full report: {report_path}")
    return expected, guard, report


def delivery_preflight(run_dir, inputs):
    import iterative_migration as core
    import iterative_delivery as delivery
    state = delivery.read(Path(run_dir) / "delivery-state.json")
    run = core.load_run(run_dir)
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    root = Path(state["workspaceRoot"]).resolve()
    if (state.get("status") == "preparing" or state["runId"] != run["runId"] or
            state.get("checkpointId") != checkpoint["checkpointId"] or
            state["sourceSnapshotKey"] != checkpoint["sourceSnapshotKey"]):
        raise RuntimeError("DELIVERY_STALE_CHECKPOINT")
    if root != delivery.delivery_root(run_dir, run["runId"]).resolve():
        raise RuntimeError("DELIVERY_PATH_OUTSIDE_RUN")
    if delivery.git(root, "branch", "--show-current") != state["branch"]:
        raise RuntimeError("DELIVERY_BRANCH_CHANGED")
    delivery.git(root, "merge-base", "--is-ancestor", state["sourceHead"], "HEAD")
    _, _, report = inspect_delivery(run_dir, state, checkpoint)
    return {key: value for key, value in report.items() if key not in {"lineEndingChanges", "blockedChanges"}}
