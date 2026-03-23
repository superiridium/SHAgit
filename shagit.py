#!/usr/bin/env python3
"""
Bulk-pin GitHub Actions references to full commit SHAs across a GitHub org.

What this script does:
  - walks repositories in an organization, or a specific list of repos
  - scans workflow YAML and composite action YAML files
  - rewrites mutable action refs like:
        uses: actions/checkout@v4
    into immutable full-SHA refs like:
        uses: actions/checkout@<40-char-sha> # v4
  - optionally creates a branch, commit, push, and PR per changed repo

Why:
  GitHub Actions tags are mutable. A full commit SHA is the only immutable
  reference for an action. This script helps migrate an org to SHA pinning.

Requirements:
  - gh CLI authenticated with repo access
  - git
  - Python 3.9+

Examples:
  python3 shagit.py my-org --dry-run
  python3 shagit.py my-org
  python3 shagit.py my-org --repo my-org/repo-a --repo my-org/repo-b
  python3 shagit.py my-org --base-branch main
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


# Match a "uses:" line in either of these common forms:
#
#   - uses: actions/checkout@v4
#   uses: actions/checkout@v4
#   - uses: "actions/checkout@v4"
#
# Groups:
#   1: leading "uses:" including indentation / "- "
#   2: optional opening quote
#   3: target, e.g. actions/checkout
#   4: ref, e.g. v4 or main or a SHA
#   5: optional closing quote
#   6: optional trailing comment
#
# We intentionally keep this conservative rather than trying to parse all YAML.
# For this job, we only care about straightforward single-line uses: entries.
USES_RE = re.compile(
    r'^(\s*-\s*uses:\s*|\s*uses:\s*)(["\']?)([^@"\'\s]+)@([^"\'\s#]+)(["\']?)(\s*#.*)?\s*$'
)

# Full immutable Git commit SHA: exactly 40 lowercase hex characters.
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Standard GitHub Actions workflow location.
WORKFLOW_DIR = ".github/workflows"

# Common location for local composite actions in a repo.
COMPOSITE_ACTIONS_DIR = ".github/actions"


@dataclass
class RepoResult:
    """
    Outcome summary for one repository.

    status values:
      - changed
      - no_changes
      - skipped
      - failed
    """
    repo: str
    status: str
    base_branch: str | None = None
    changed_files: int = 0
    changed_refs: int = 0
    message: str = ""


def run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """
    Run a subprocess and capture stdout/stderr as text.

    This central wrapper keeps subprocess handling uniform throughout the script:
      - text mode enabled
      - stdout/stderr captured for logging and error messages
      - caller decides whether non-zero exit codes should raise
    """
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def gh_json(args: list[str]) -> object:
    """
    Run a gh command and parse its stdout as JSON.

    Example:
      gh_json(["repo", "view", "org/repo", "--json", "nameWithOwner"])
    """
    cp = run(["gh", *args])
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse JSON from: gh {' '.join(args)}\n{cp.stdout}"
        ) from e


def list_repos(org: str, explicit_repos: list[str]) -> list[dict]:
    """
    Return repo metadata in a normalized shape.

    If explicit repos were provided, use `gh repo view` on each one.
    Otherwise, list org repos via the REST API.

    Why REST here instead of `gh repo list`?
      We previously hit transient GraphQL issues from `gh repo list`, so this
      uses the REST endpoint for org enumeration to be more robust.

    Returned dict shape is intentionally normalized so the rest of the code
    doesn't care whether the repo came from the REST API or `gh repo view`.
    """
    if explicit_repos:
        repos = []
        for repo in explicit_repos:
            data = gh_json([
                "repo", "view", repo,
                "--json", "nameWithOwner,isArchived,defaultBranchRef"
            ])
            repos.append(data)
        return repos

    repos: list[dict] = []
    page = 1

    while True:
        data = gh_json([
            "api",
            f"/orgs/{org}/repos?per_page=100&page={page}&type=all"
        ])

        # Empty page means we're done.
        if not data:
            break

        for r in data:
            # Archived repos are ignored entirely; we don't want to open churn PRs
            # against frozen history.
            if r.get("archived"):
                continue

            repos.append({
                "nameWithOwner": r["full_name"],
                "isArchived": r["archived"],
                "defaultBranchRef": {"name": r.get("default_branch") or "main"},
                # Empty repos have no commits and cannot be cloned meaningfully.
                "isEmpty": r.get("size", 0) == 0,
            })

        # Fewer than 100 results means this was the last page.
        if len(data) < 100:
            break

        page += 1

    return repos


def default_branch(repo: dict, fallback: str | None) -> str:
    """
    Pick the branch we *want* to use as the PR base.

    Priority:
      1. explicit --base-branch override from the user
      2. repo metadata defaultBranchRef.name
      3. final fallback to 'main'

    Note that this is only the *requested* branch. The actual checked-out
    branch may differ if clone fallback logic kicks in.
    """
    if fallback:
        return fallback

    ref = repo.get("defaultBranchRef")
    if ref and ref.get("name"):
        return ref["name"]

    return "main"


def repo_is_empty(repo: dict) -> bool:
    """Return True if repo metadata says this repository is empty."""
    return bool(repo.get("isEmpty"))


def find_yaml_files(repo_dir: Path) -> list[Path]:
    """
    Find workflow and composite-action YAML files under a cloned repo.

    We scan:
      - .github/workflows/**/*.yml|yaml
      - .github/actions/**/action.yml|yaml

    The result is de-duplicated while preserving discovery order.
    """
    files: list[Path] = []

    workflow_dir = repo_dir / WORKFLOW_DIR
    if workflow_dir.exists():
        files.extend(workflow_dir.rglob("*.yml"))
        files.extend(workflow_dir.rglob("*.yaml"))

    composite_dir = repo_dir / COMPOSITE_ACTIONS_DIR
    if composite_dir.exists():
        files.extend(composite_dir.rglob("action.yml"))
        files.extend(composite_dir.rglob("action.yaml"))

    seen = set()
    result = []
    for f in files:
        s = str(f)
        if s not in seen:
            seen.add(s)
            result.append(f)

    return result


def is_local_or_docker(target: str) -> bool:
    """
    Return True for references we must not rewrite.

    Examples:
      ./local-action
      ../other-action
      docker://alpine:3.20

    These are not GitHub-hosted action refs and therefore don't map to
    owner/repo@ref resolution.
    """
    return (
        target.startswith("./")
        or target.startswith("../")
        or target.startswith("docker://")
    )


def action_repo_from_uses(target: str) -> str | None:
    """
    Extract the GitHub repo portion from a uses target.

    Example:
      actions/checkout                -> actions/checkout
      owner/repo/path/to/action       -> owner/repo

    Returns None if the target doesn't look like owner/repo[/...].
    """
    parts = target.split("/")
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def is_probably_reusable_workflow(target: str) -> bool:
    """
    Return True if this looks like a reusable workflow reference.

    Example:
      owner/repo/.github/workflows/build.yml

    Those are intentionally left unchanged here. GitHub treats reusable
    workflows differently from normal actions, and org policy may still allow
    them by tag.
    """
    return ".github/workflows/" in target


def resolve_sha(action_repo: str, ref: str) -> str:
    """
    Resolve owner/repo@ref to a full 40-character commit SHA.

    We ask the GitHub API for the commit behind the given ref, which may be
    a tag, branch, or already a commit-ish string.

    The result must be a full SHA or we fail loudly.
    """
    encoded_ref = urllib.parse.quote(ref, safe="")
    cp = run([
        "gh", "api",
        f"/repos/{action_repo}/commits/{encoded_ref}",
        "--jq", ".sha"
    ])

    sha = cp.stdout.strip()
    if not FULL_SHA_RE.fullmatch(sha):
        raise RuntimeError(
            f"Could not resolve full SHA for {action_repo}@{ref}: got {sha!r}"
        )

    return sha


def rewrite_file(path: Path, cache: dict[tuple[str, str], str]) -> tuple[bool, list[str], int]:
    """
    Rewrite mutable action refs in one YAML file.

    Returns:
      (changed, logs, replacements)

    changed:
      True if the file content was modified

    logs:
      human-readable descriptions of each rewritten reference

    replacements:
      number of action refs pinned in this file

    Notes:
      - existing full SHAs are left alone
      - local actions are left alone
      - docker:// refs are left alone
      - reusable workflow refs are left alone
      - a same-line comment with the original ref is added:
            uses: actions/checkout@<sha> # v4
    """
    original = path.read_text(encoding="utf-8")
    changed = False
    logs: list[str] = []
    out_lines: list[str] = []
    replacements = 0

    for line in original.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = USES_RE.match(stripped)

        # Non-matching lines pass through untouched.
        if not m:
            out_lines.append(line)
            continue

        prefix, open_q, target, ref, close_q, comment = m.groups()

        # Local paths and container references are not GitHub action repos.
        if is_local_or_docker(target):
            out_lines.append(line)
            continue

        # Already immutable; keep as-is.
        if FULL_SHA_RE.fullmatch(ref):
            out_lines.append(line)
            continue

        # Reusable workflows are deliberately excluded from this rewrite.
        if is_probably_reusable_workflow(target):
            out_lines.append(line)
            continue

        # Extract owner/repo from owner/repo[/subpath] form.
        action_repo = action_repo_from_uses(target)
        if not action_repo:
            out_lines.append(line)
            continue

        # Cache API lookups so repeated refs like actions/checkout@v4
        # across many files are only resolved once per repo.
        key = (action_repo, ref)
        if key not in cache:
            cache[key] = resolve_sha(action_repo, ref)

        sha = cache[key]

        # Preserve readability and future automation hints by keeping the
        # original symbolic ref as a comment.
        new_comment = f" # {ref}"
        new_line = f"{prefix}{open_q}{target}@{sha}{close_q}{new_comment}\n"

        out_lines.append(new_line)
        changed = True
        replacements += 1

        # Show paths relative to the repo root-ish area for nicer logging.
        logs.append(f"{path.relative_to(path.parents[1])}: {target}@{ref} -> {sha}")

    if changed:
        path.write_text("".join(out_lines), encoding="utf-8")

    return changed, logs, replacements


def checkout_branch(repo_dir: Path, branch: str) -> None:
    """Create and switch to a new working branch."""
    run(["git", "checkout", "-b", branch], cwd=str(repo_dir))


def commit_changes(repo_dir: Path, message: str) -> None:
    """Stage all modifications and create a single commit."""
    run(["git", "add", "."], cwd=str(repo_dir))
    run(["git", "commit", "-m", message], cwd=str(repo_dir))


def push_branch(repo_dir: Path, branch: str) -> None:
    """Push the working branch to origin and set upstream."""
    run(["git", "push", "-u", "origin", branch], cwd=str(repo_dir))


def pr_exists(repo: str, branch: str) -> bool:
    """
    Return True if an open PR already exists for repo:branch.

    This avoids creating duplicate PRs if the script is re-run.
    """
    cp = run([
        "gh", "pr", "list",
        "--repo", repo,
        "--head", branch,
        "--state", "open",
        "--json", "number"
    ])
    data = json.loads(cp.stdout)
    return len(data) > 0


def create_pr(repo: str, branch: str, base: str, title: str, body: str) -> None:
    """
    Open a PR unless one already exists for this head branch.
    """
    if pr_exists(repo, branch):
        print(f"  PR already exists for {repo}:{branch}")
        return

    run([
        "gh", "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", branch,
        "--title", title,
        "--body", body,
    ])


def clone_repo(repo: str, dest: Path, preferred_base: str) -> str:
    """
    Clone a repo and return the actual checked-out branch name.

    Strategy:
      1. Try to clone the requested branch explicitly.
      2. If that branch does not exist, fall back to default clone behavior.
      3. Ask git what branch we actually landed on.

    Why this exists:
      Repo metadata is not always trustworthy enough to bet the whole run on.
      A repo may not actually have the branch we expected, especially in odd,
      migrated, or partially initialized repositories.
    """
    try:
        run([
            "gh", "repo", "clone", repo, str(dest),
            "--", "--depth", "1", "--branch", preferred_base
        ])
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""

        # Only recover from the specific "requested branch is missing" case.
        # Other clone failures should bubble up and fail the repo loudly.
        if "Remote branch" in stderr and "not found" in stderr:
            print(
                f"  Preferred branch '{preferred_base}' not found, "
                "falling back to repo default branch"
            )
            run([
                "gh", "repo", "clone", repo, str(dest),
                "--", "--depth", "1"
            ])
        else:
            raise

    # In the normal case, git knows the branch immediately.
    cp = run(["git", "branch", "--show-current"], cwd=str(dest))
    actual = cp.stdout.strip()
    if actual:
        return actual

    # Fallback for detached or odd states.
    cp = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(dest))
    actual = cp.stdout.strip()
    if actual and actual != "HEAD":
        return actual

    # Last resort: return what we intended to use.
    return preferred_base


def process_repo(
    repo: dict,
    dry_run: bool,
    branch_name: str,
    pr_title: str,
    pr_body: str,
    base_branch_override: str | None,
) -> RepoResult:
    """
    Process one repository end-to-end.

    Steps:
      - decide target base branch
      - skip empty repos
      - clone
      - discover YAML files
      - rewrite mutable refs to SHAs
      - optionally commit/push/open PR
      - return a structured RepoResult for final summary
    """
    repo_full = repo["nameWithOwner"]
    requested_base = default_branch(repo, base_branch_override)

    print(f"\n==> {repo_full} ({requested_base})")

    # Per-repo SHA resolution cache.
    # If a repo uses actions/checkout@v4 in 30 places, resolve once.
    cache: dict[tuple[str, str], str] = {}

    if repo_is_empty(repo):
        print("  Skipped: repository appears to be empty")
        return RepoResult(
            repo=repo_full,
            status="skipped",
            base_branch=requested_base,
            message="repository appears to be empty",
        )

    # Each repo is processed in an isolated temp dir so the run is stateless
    # and leaves no local clone mess behind.
    with tempfile.TemporaryDirectory(prefix="pin-actions-") as td:
        repo_dir = Path(td) / repo_full.split("/")[-1]

        try:
            actual_base = clone_repo(repo_full, repo_dir, requested_base)
            if actual_base != requested_base:
                print(f"  Using actual base branch: {actual_base}")
        except subprocess.CalledProcessError as e:
            message = e.stderr.strip() or str(e)
            print(f"  Clone failed: {message}")
            return RepoResult(
                repo=repo_full,
                status="failed",
                base_branch=requested_base,
                message=f"clone failed: {message}",
            )

        files = find_yaml_files(repo_dir)
        if not files:
            print("  No workflow/composite action YAML files found")
            return RepoResult(
                repo=repo_full,
                status="skipped",
                base_branch=actual_base,
                message="no workflow/composite action YAML files found",
            )

        any_changed = False
        all_logs: list[str] = []
        changed_files = 0
        changed_refs = 0

        for path in files:
            try:
                changed, logs, replacements = rewrite_file(path, cache)
                if changed:
                    any_changed = True
                    changed_files += 1
                    changed_refs += replacements
                    all_logs.extend(logs)
            except subprocess.CalledProcessError as e:
                message = e.stderr.strip() or str(e)
                print(f"  Failed resolving action ref in {path}: {message}")
                return RepoResult(
                    repo=repo_full,
                    status="failed",
                    base_branch=actual_base,
                    message=f"failed resolving action ref in {path.name}: {message}",
                )
            except Exception as e:
                print(f"  Error rewriting {path}: {e}")
                return RepoResult(
                    repo=repo_full,
                    status="failed",
                    base_branch=actual_base,
                    message=f"error rewriting {path.name}: {e}",
                )

        if not any_changed:
            print("  No changes needed")
            return RepoResult(
                repo=repo_full,
                status="no_changes",
                base_branch=actual_base,
                message="no mutable action refs found",
            )

        # Print only the first few changes to avoid flooding output on large repos.
        for entry in all_logs[:20]:
            print(f"  {entry}")
        if len(all_logs) > 20:
            print(f"  ... and {len(all_logs) - 20} more")

        if dry_run:
            print("  Dry run only; not creating branch/commit/PR")
            return RepoResult(
                repo=repo_full,
                status="changed",
                base_branch=actual_base,
                changed_files=changed_files,
                changed_refs=changed_refs,
                message="dry run",
            )

        try:
            checkout_branch(repo_dir, branch_name)
            commit_changes(repo_dir, "Pin GitHub Actions to full commit SHAs")
            push_branch(repo_dir, branch_name)
            create_pr(repo_full, branch_name, actual_base, pr_title, pr_body)

            print("  PR created")
            return RepoResult(
                repo=repo_full,
                status="changed",
                base_branch=actual_base,
                changed_files=changed_files,
                changed_refs=changed_refs,
                message=f"branch pushed and PR opened against {actual_base}",
            )
        except subprocess.CalledProcessError as e:
            message = e.stderr.strip() or str(e)
            print(f"  Git/PR step failed: {message}")
            return RepoResult(
                repo=repo_full,
                status="failed",
                base_branch=actual_base,
                changed_files=changed_files,
                changed_refs=changed_refs,
                message=f"git/pr step failed: {message}",
            )


def print_summary(results: list[RepoResult], dry_run: bool) -> None:
    """
    Print an end-of-run summary with totals and per-repo outcomes.
    """
    total = len(results)
    changed = sum(1 for r in results if r.status == "changed")
    no_changes = sum(1 for r in results if r.status == "no_changes")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")

    total_files = sum(r.changed_files for r in results)
    total_refs = sum(r.changed_refs for r in results)

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Mode:              {'dry-run' if dry_run else 'live'}")
    print(f"Repositories:      {total}")
    print(f"Changed:           {changed}")
    print(f"No changes:        {no_changes}")
    print(f"Skipped:           {skipped}")
    print(f"Failed:            {failed}")
    print(f"Files changed:     {total_files}")
    print(f"Action refs pinned:{total_refs}")
    print("=" * 72)

    if changed:
        print("\nChanged repositories:")
        for r in results:
            if r.status == "changed":
                print(
                    f"  - {r.repo} [{r.base_branch}] "
                    f"files={r.changed_files} refs={r.changed_refs} ({r.message})"
                )

    if no_changes:
        print("\nNo changes needed:")
        for r in results:
            if r.status == "no_changes":
                print(f"  - {r.repo} [{r.base_branch}] ({r.message})")

    if skipped:
        print("\nSkipped:")
        for r in results:
            if r.status == "skipped":
                print(f"  - {r.repo} [{r.base_branch}] ({r.message})")

    if failed:
        print("\nFailed:")
        for r in results:
            if r.status == "failed":
                print(f"  - {r.repo} [{r.base_branch}] ({r.message})")

    print()


def main() -> int:
    """
    Parse arguments, enumerate repositories, process them, print summary,
    and return a sensible process exit code.

    Exit codes:
      0: success, or only non-fatal skips/no-change repos
      1: one or more repos failed during processing
      2: startup / repo enumeration failure
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Specific repo(s) as org/name",
    )
    parser.add_argument(
        "--base-branch",
        default=None,
        help="Override base branch for all repos",
    )
    parser.add_argument(
        "--branch-name",
        default="chore/pin-actions-to-sha",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        repos = list_repos(args.org, args.repo)
    except subprocess.CalledProcessError as e:
        print(e.stderr.strip(), file=sys.stderr)
        return 2
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2

    repos = [r for r in repos if not r.get("isArchived")]
    if not repos:
        print("No matching repositories found.")
        return 0

    pr_title = "Pin GitHub Actions to full commit SHAs"
    pr_body = """This PR updates GitHub Actions references from mutable tags/versions to full-length commit SHAs.

Why:
- Full commit SHAs are immutable
- This prepares the repository for SHA pinning policies
- The original version/tag is kept as a same-line comment to help future updates

Notes:
- Local actions (./...) are left unchanged
- docker:// references are left unchanged
- Existing full SHA pins are left unchanged
- Reusable workflow references are left unchanged
"""

    results: list[RepoResult] = []

    for repo in repos:
        result = process_repo(
            repo=repo,
            dry_run=args.dry_run,
            branch_name=args.branch_name,
            pr_title=pr_title,
            pr_body=pr_body,
            base_branch_override=args.base_branch,
        )
        results.append(result)

    print_summary(results, args.dry_run)

    # Non-zero exit if any repo failed so this can be used in automation.
    if any(r.status == "failed" for r in results):
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
