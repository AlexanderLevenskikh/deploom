"""Release the result branch without deleting its verified inspection checkout."""
from pathlib import Path


def assert_delivery_branch(root, state):
    import iterative_delivery as delivery
    branch = delivery.git(root, "branch", "--show-current")
    if branch == state["branch"]:
        return
    if not branch and state.get("status") == "done" and delivery.git(root, "rev-parse", "HEAD") == state.get("head"):
        return
    raise RuntimeError("DELIVERY_BRANCH_CHANGED")


def release_branch(run_dir, state):
    import iterative_delivery as delivery
    import iterative_migration as core
    from delivery_git_status import committed_worktree
    root = Path(state["workspaceRoot"]).resolve()
    if root != delivery.delivery_root(run_dir, state["runId"]).resolve() or state.get("status") != "done":
        raise RuntimeError("DELIVERY_RELEASE_REQUIRES_VERIFIED_RESULT")
    # An already released or user-modified result is never switched or removed.
    branch = delivery.git(root, "branch", "--show-current")
    if (not committed_worktree(root) or delivery.git(root, "rev-parse", "HEAD") != state["head"]
            or branch not in {"", state["branch"]}
            or delivery.git(root, "rev-parse", f"refs/heads/{state['branch']}") != state["head"]):
        state["branchRelease"] = "preserved-user-changes"
    else:
        if branch:
            from iterative_delivery_checkout import checkout_hook_args
            delivery.git(root, *checkout_hook_args(run_dir, state), "checkout", "--detach", state["head"])
        state["branchRelease"] = "released"
    core._write_json_atomic(run_dir / "delivery-state.json", state)
    return state


def prepare_cleanup_branch(run_dir, state):
    import iterative_delivery as delivery
    import iterative_migration as core
    from delivery_git_status import committed_worktree
    root = Path(state["workspaceRoot"]).resolve()
    assert_delivery_branch(root, state)
    if delivery.git(root, "branch", "--show-current") or (state.get("cleanup") or {}).get("status") == "done":
        return
    if not committed_worktree(root) or delivery.git(root, "rev-parse", f"refs/heads/{state['branch']}") != state["head"]:
        raise RuntimeError("DELIVERY_CLEANUP_BRANCH_CHANGED: preserve user edits")
    from iterative_delivery_checkout import checkout_hook_args
    try:
        delivery.git(root, *checkout_hook_args(run_dir, state), "checkout", state["branch"])
    except RuntimeError as exc:
        raise RuntimeError("DELIVERY_CLEANUP_BRANCH_IN_USE: release the result branch in other checkouts before cleaning notes") from exc
    state["branchRelease"] = "cleanup-in-progress"
    core._write_json_atomic(run_dir / "delivery-state.json", state)
