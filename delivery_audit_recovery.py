"""Recover controller-owned audit copies without accepting agent-authored evidence."""
from pathlib import Path
import hashlib
import os
from uuid import uuid4
from migration_documents import relative_document_names


def restore_audit_copies(run_dir, state):
    root = Path(state["workspaceRoot"]).resolve()
    reports = Path(run_dir) / "reports"
    prefix = f"docs/dependency-migration/{state['runId']}/"
    names = [n for n in relative_document_names(state.get("documentLanguage") or "en")
             if Path(n).name in {"pre-fix-audit-report.md", "post-fix-audit-report.md"}]
    names += ["pre-fix-audit-report.json", "post-fix-audit-report.json"]
    restored = []
    for name in names:
        relative = prefix + name
        digest = state.get("expected", {}).get(relative)
        if not digest:
            continue
        target, reference = root / relative, reports / name
        if (not target.resolve().is_relative_to(root) or target.is_symlink()
                or not reference.resolve().is_relative_to(reports.resolve()) or reference.is_symlink()
                or not target.is_file() or not reference.is_file()):
            continue
        original = reference.read_bytes()
        if hashlib.sha256(original).hexdigest() != digest:
            continue  # Missing/corrupt references never redefine the prepared subject.
        displaced = target.read_bytes()
        current = hashlib.sha256(displaced).hexdigest()
        if current == digest:
            continue
        archive = Path(run_dir) / "delivery" / "displaced-audit-reports" / current / name
        if not archive.resolve().is_relative_to(Path(run_dir).resolve()):
            raise RuntimeError("DELIVERY_AUDIT_ARCHIVE_PATH_UNSAFE")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.is_symlink():
            raise RuntimeError("DELIVERY_AUDIT_ARCHIVE_PATH_UNSAFE")
        if archive.exists():
            if archive.read_bytes() != displaced:
                raise RuntimeError("DELIVERY_AUDIT_ARCHIVE_CHANGED")
        else:
            with archive.open("xb") as handle:
                handle.write(displaced)
        if target.read_bytes() != displaced:
            raise RuntimeError("DELIVERY_AUDIT_CHANGED_DURING_RECOVERY")
        temporary = target.with_name(target.name + f".{uuid4().hex}.recovery-tmp")
        with temporary.open("xb") as handle:
            handle.write(original)
        os.replace(temporary, target)
        restored.append({"path": relative, "displacedHash": current, "archiveRef": str(archive)})
    return restored
