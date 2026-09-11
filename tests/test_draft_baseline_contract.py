from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
DESKTOP_MAIN = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
DIALOG = (ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx").read_text(encoding="utf-8")
TYPES = (ROOT / "desktop" / "src" / "types.ts").read_text(encoding="utf-8")


def test_draft_generator_python_syntax_is_valid() -> None:
    ast.parse(GENERATOR)


def test_draft_cli_and_authority_markers_are_explicit() -> None:
    assert '"--draft-baseline"' in GENERATOR
    assert "--capture-baseline and --draft-baseline are mutually exclusive" in GENERATOR
    assert '"verificationStatus": "NOT_VERIFIED"' in GENERATOR
    assert '"authority": "PLANNING_ONLY"' in GENERATOR
    assert '"compatibility": "UNKNOWN"' in GENERATOR


def test_draft_static_path_skips_physical_verification_and_proof_publication() -> None:
    assert "if args.draft_baseline:" in GENERATOR
    assert "resolve_peer_compatibility(" in GENERATOR
    assert "Draft Baseline: ProofEnvelope publication skipped" in GENERATOR
    assert 'proven_dependency_state = {"schemaVersion": 1, "projects": {}}' in GENERATOR
    assert "existing verified proof state left untouched" in GENERATOR


def test_desktop_draft_is_invocation_local_and_does_not_advance_flow() -> None:
    assert "export type BaselineProofMode = 'VERIFIED' | 'DRAFT'" in TYPES
    assert "delete durable.proofMode" in DESKTOP_MAIN
    assert "job.action === 'baseline' && job.baselineProofMode === 'DRAFT'" in DESKTOP_MAIN
    assert "job.baselineProofMode !== 'DRAFT'" in DESKTOP_MAIN
    assert "DRAFT_BASELINE_AUTOPILOT_FORBIDDEN" in DESKTOP_MAIN


def test_draft_ui_and_external_agent_handoff_are_visible() -> None:
    assert "Create Draft and show prompt" in DIALOG
    assert "Build Draft for agent handoff" in DIALOG
    assert "DRAFT BASELINE / PLANNING ONLY" in GENERATOR
