"""
pr_publisher.py

A separate CLI step that runs *after* Sonar fixes have been applied to the
local ``sonarfixes/<timestamp>`` branch created by ``repo_checkout.py``.

Responsibilities
----------------
1. **Commit** – stage every modified/new file in the cloned repo and create a
   Git commit with the supplied message.
2. **Push** – push the ``sonarfixes/<timestamp>`` branch to the remote origin,
   authenticating with a PAT when supplied.
3. **Pull Request** – open a PR from the fix branch back to the originally
   requested base branch using the hosting platform's REST API.
   Supported platforms: GitHub, Azure DevOps.

This step is intentionally *separate* from ``repo_checkout.py`` so that the
fix-engine can make and verify its changes before committing/publishing.

Usage
-----
    python src/pr_publisher.py \\
        --clone-dir  ./workdir/my-repo \\
        --repo-url   https://github.com/org/repo.git \\
        --fix-branch sonarfixes/20260227_153042 \\
        --base-branch main \\
        --commit-message "fix: apply sonarqube auto-fixes" \\
        [--pat TOKEN] \\
        [--pr-title  "Sonar auto-fixes"] \\
        [--pr-body   "Automated fixes by the Sonar Auto-Fix platform."]
"""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from git import GitCommandError, InvalidGitRepositoryError, Repo

# Re-use URL helpers from the checkout module which lives in the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_checkout import inject_pat_into_url, safe_display_url


# ---------------------------------------------------------------------------
# Platform detection & URL parsing
# ---------------------------------------------------------------------------

def detect_platform(repo_url: str) -> str:
    """
    Identify the Git hosting platform from *repo_url*.

    Returns
    -------
    str
        One of ``"github"``, ``"azure_devops"``, or ``"unknown"``.
    """
    host = urlparse(repo_url).hostname or ""
    if "github.com" in host:
        return "github"
    if "dev.azure.com" in host or "visualstudio.com" in host:
        return "azure_devops"
    return "unknown"


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """
    Extract ``(owner, repo_name)`` from a GitHub repository URL.

    Supports both HTTPS and SSH formats:
    - ``https://github.com/owner/repo.git``
    - ``git@github.com:owner/repo.git``

    Returns
    -------
    tuple[str, str]
        ``(owner, repo_name)`` with ``.git`` suffix stripped from repo_name.

    Raises
    ------
    ValueError
        If the URL cannot be parsed as a GitHub repository URL.
    """
    parsed = urlparse(repo_url)
    # Normalise SSH style: git@github.com:owner/repo.git  →  /owner/repo.git
    path = parsed.path.lstrip("/")
    parts = path.rstrip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse GitHub owner/repo from URL: {repo_url!r}")
    owner = parts[0]
    repo_name = parts[1].removesuffix(".git")
    return owner, repo_name


def parse_azure_repo(repo_url: str) -> tuple[str, str, str]:
    """
    Extract ``(org, project, repo_name)`` from an Azure DevOps repository URL.

    Expects the canonical format:
    ``https://dev.azure.com/{org}/{project}/_git/{repo}``

    Returns
    -------
    tuple[str, str, str]
        ``(org, project, repo_name)``.

    Raises
    ------
    ValueError
        If the URL cannot be parsed as an Azure DevOps repository URL.
    """
    parsed = urlparse(repo_url)
    parts = [p for p in parsed.path.split("/") if p]
    # Expected: [org, project, "_git", repo]
    try:
        git_idx = parts.index("_git")
        org = parts[0]
        project = parts[git_idx - 1]
        repo_name = parts[git_idx + 1].removesuffix(".git")
        return org, project, repo_name
    except (ValueError, IndexError):
        raise ValueError(
            f"Cannot parse Azure DevOps org/project/repo from URL: {repo_url!r}"
        )


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def commit_changes(clone_dir: Path, message: str) -> str:
    """
    Stage **all** tracked and untracked changes in *clone_dir* and create a
    Git commit with *message*.

    Parameters
    ----------
    clone_dir : Path
        Absolute path to the cloned repository root.
    message : str
        Commit message.

    Returns
    -------
    str
        The full SHA of the newly created commit.

    Raises
    ------
    ValueError
        If there are no changes to commit.
    InvalidGitRepositoryError
        If *clone_dir* is not a valid Git repository.
    """
    repo = Repo(clone_dir)

    if not repo.is_dirty(untracked_files=True):
        raise ValueError(
            f"No changes detected in '{clone_dir}'. Nothing to commit."
        )

    repo.git.add("--all")
    repo.index.commit(message)
    sha = repo.head.commit.hexsha
    print(f"[OK] Committed changes: {sha[:12]}  \"{message}\"")
    return sha


def push_fix_branch(
    clone_dir: Path,
    fix_branch: str,
    repo_url: str | None = None,
    pat: str | None = None,
) -> None:
    """
    Push *fix_branch* from the local clone to the remote origin.

    If *pat* is supplied the remote URL is temporarily overridden with the
    authenticated form so the push succeeds without an interactive prompt.

    Parameters
    ----------
    clone_dir : Path
        Absolute path to the cloned repository root.
    fix_branch : str
        Name of the local branch to push (e.g. ``sonarfixes/20260227_153042``).
    repo_url : str | None
        Original (unauthenticated) remote URL. Required when *pat* is given.
    pat : str | None
        Personal Access Token for HTTPS push authentication.

    Raises
    ------
    GitCommandError
        If the push fails for any reason.
    """
    repo = Repo(clone_dir)
    origin = repo.remotes.origin

    if pat and repo_url:
        authenticated_url = inject_pat_into_url(repo_url, pat)
        origin.set_url(authenticated_url)
        display = safe_display_url(authenticated_url)
    else:
        display = origin.url

    print(f"[INFO] Pushing '{fix_branch}' to {display} ...")
    try:
        origin.push(refspec=f"{fix_branch}:{fix_branch}", set_upstream=True)
    except GitCommandError as exc:
        print(f"[ERROR] Push failed:\n{exc}", file=sys.stderr)
        raise
    finally:
        # Restore the unauthenticated URL so credentials are not persisted on disk
        if pat and repo_url:
            origin.set_url(repo_url)

    print(f"[OK] Branch '{fix_branch}' pushed to origin.")


# ---------------------------------------------------------------------------
# Pull Request creation
# ---------------------------------------------------------------------------

def create_github_pr(
    owner: str,
    repo_name: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    pat: str,
) -> str:
    """
    Open a GitHub Pull Request via the REST API.

    Parameters
    ----------
    owner : str        GitHub username or organisation.
    repo_name : str    Repository name (without ``.git``).
    head_branch : str  Source branch (the sonarfix branch).
    base_branch : str  Target branch (the originally requested branch).
    title : str        PR title.
    body : str         PR description.
    pat : str          GitHub Personal Access Token (needs ``repo`` scope).

    Returns
    -------
    str
        URL of the newly created Pull Request.

    Raises
    ------
    requests.HTTPError
        If the GitHub API returns a non-2xx response.
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch,
    }
    response = requests.post(api_url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    pr_url = response.json()["html_url"]
    print(f"[OK] GitHub PR created: {pr_url}")
    return pr_url


def create_azure_pr(
    org: str,
    project: str,
    repo_name: str,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    pat: str,
) -> str:
    """
    Open an Azure DevOps Pull Request via the REST API.

    Parameters
    ----------
    org : str          Azure DevOps organisation name.
    project : str      Project name.
    repo_name : str    Repository name.
    head_branch : str  Source branch (the sonarfix branch, without ``refs/heads/`` prefix).
    base_branch : str  Target branch (the originally requested branch).
    title : str        PR title.
    body : str         PR description.
    pat : str          Azure DevOps Personal Access Token.

    Returns
    -------
    str
        URL of the newly created Pull Request.

    Raises
    ------
    requests.HTTPError
        If the Azure DevOps API returns a non-2xx response.
    """
    import base64 as _b64

    api_url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories"
        f"/{repo_name}/pullrequests?api-version=7.0"
    )
    # Azure DevOps uses Basic auth with a base64-encoded PAT
    token_b64 = _b64.b64encode(f":{pat}".encode()).decode()
    headers = {
        "Authorization": f"Basic {token_b64}",
        "Content-Type": "application/json",
    }
    payload = {
        "title": title,
        "description": body,
        "sourceRefName": f"refs/heads/{head_branch}",
        "targetRefName": f"refs/heads/{base_branch}",
    }
    response = requests.post(api_url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    pr_url = (
        f"https://dev.azure.com/{org}/{project}/_git/{repo_name}/pullrequest"
        f"/{data['pullRequestId']}"
    )
    print(f"[OK] Azure DevOps PR created: {pr_url}")
    return pr_url


def create_pull_request(
    repo_url: str,
    fix_branch: str,
    base_branch: str,
    title: str,
    body: str,
    pat: str,
) -> str:
    """
    Detect the hosting platform and open a Pull Request from *fix_branch* to
    *base_branch*.

    Parameters
    ----------
    repo_url : str      Original repository URL (used to detect the platform).
    fix_branch : str    Source branch (the sonarfix branch).
    base_branch : str   Target branch (the originally requested branch).
    title : str         PR title.
    body : str          PR body / description.
    pat : str           Personal Access Token.

    Returns
    -------
    str
        URL of the newly created Pull Request.

    Raises
    ------
    ValueError
        If the platform cannot be detected or the URL cannot be parsed.
    requests.HTTPError
        If the hosting platform API returns an error.
    """
    platform = detect_platform(repo_url)

    if platform == "github":
        owner, repo_name = parse_github_repo(repo_url)
        return create_github_pr(
            owner, repo_name, fix_branch, base_branch, title, body, pat
        )

    if platform == "azure_devops":
        org, project, repo_name = parse_azure_repo(repo_url)
        return create_azure_pr(
            org, project, repo_name, fix_branch, base_branch, title, body, pat
        )

    raise ValueError(
        f"Unsupported hosting platform for URL: {repo_url!r}. "
        "Supported platforms: GitHub, Azure DevOps."
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def publish_and_create_pr(
    clone_dir: Path,
    repo_url: str,
    fix_branch: str,
    base_branch: str,
    commit_message: str,
    pr_title: str | None = None,
    pr_body: str | None = None,
    pat: str | None = None,
) -> str:
    """
    Full publish pipeline: commit → push → open PR.

    Parameters
    ----------
    clone_dir : Path        Path to the cloned repository on disk.
    repo_url : str          Original remote URL (for push auth + PR API).
    fix_branch : str        The sonarfix branch that contains the fixes.
    base_branch : str       Branch the PR should target.
    commit_message : str    Commit message for the fix commit.
    pr_title : str | None   PR title; defaults to a sensible generated title.
    pr_body : str | None    PR body; defaults to a generic description.
    pat : str | None        PAT for push authentication and PR API calls.

    Returns
    -------
    str
        URL of the created Pull Request.

    Raises
    ------
    ValueError
        If there are no changes to commit or the platform is unsupported.
    GitCommandError
        If the push fails.
    requests.HTTPError
        If the PR API call fails.
    """
    if pr_title is None:
        pr_title = f"Sonar auto-fix: {fix_branch}"
    if pr_body is None:
        pr_body = (
            f"Automated Sonar fixes applied on branch `{fix_branch}`.\n\n"
            f"Base branch: `{base_branch}`\n"
            "Generated by the Sonar Auto-Fix Platform."
        )

    # Step 1 — commit
    commit_changes(clone_dir, commit_message)

    # Step 2 — push
    push_fix_branch(clone_dir, fix_branch, repo_url=repo_url, pat=pat)

    # Step 3 — open PR
    if not pat:
        print(
            "[WARN] No PAT supplied; skipping PR creation. "
            "Push the branch manually and open a PR on the hosting platform."
        )
        return ""

    pr_url = create_pull_request(
        repo_url=repo_url,
        fix_branch=fix_branch,
        base_branch=base_branch,
        title=pr_title,
        body=pr_body,
        pat=pat,
    )
    return pr_url


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Commit local fixes, push the sonarfix branch to origin, "
            "and open a Pull Request against the base branch."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--clone-dir",
        required=True,
        metavar="PATH",
        help="Path to the cloned repository (e.g. ./workdir/my-repo).",
    )
    parser.add_argument(
        "--repo-url",
        required=True,
        metavar="URL",
        help="Original remote repository URL (HTTPS). Used for push auth and PR API.",
    )
    parser.add_argument(
        "--fix-branch",
        required=True,
        metavar="BRANCH",
        help="The sonarfix branch to commit, push, and PR (e.g. sonarfixes/20260227_153042).",
    )
    parser.add_argument(
        "--base-branch",
        required=True,
        metavar="BRANCH",
        help="Target branch for the Pull Request (the branch originally checked out).",
    )
    parser.add_argument(
        "--commit-message",
        required=True,
        metavar="MSG",
        help="Git commit message for the fix commit.",
    )
    parser.add_argument(
        "--pat",
        default=None,
        metavar="TOKEN",
        help="Personal Access Token for HTTPS push auth and PR API calls.",
    )
    parser.add_argument(
        "--pr-title",
        default=None,
        metavar="TITLE",
        help="Pull Request title. Defaults to 'Sonar auto-fix: <fix-branch>'.",
    )
    parser.add_argument(
        "--pr-body",
        default=None,
        metavar="BODY",
        help="Pull Request description. Defaults to a generated description.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        pr_url = publish_and_create_pr(
            clone_dir=Path(args.clone_dir),
            repo_url=args.repo_url,
            fix_branch=args.fix_branch,
            base_branch=args.base_branch,
            commit_message=args.commit_message,
            pr_title=args.pr_title,
            pr_body=args.pr_body,
            pat=args.pat,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    if pr_url:
        print(f"\nPull Request: {pr_url}")


if __name__ == "__main__":
    main()
