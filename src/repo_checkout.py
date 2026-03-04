"""
repo_checkout.py

Clones (or updates) an external Git repository into the local `workdir/` folder.
The workdir is listed in .gitignore and is never committed to this repository.

After the specified branch is checked out a new local branch is automatically
created with the naming convention:

    sonarfixes/<YYYYMMDD_HHMMSS>

This branch is the working branch on which Sonar fixes will be applied.

Usage
-----
    python src/repo_checkout.py --repo <url> --branch <branch> [--pat <token>] [--workdir <path>]

Arguments
---------
    --repo      URL or path of the remote repository to clone.
    --branch    Branch to checkout after cloning.
    --pat       (Optional) Personal Access Token for authenticated access.
                For GitHub  : https://github.com/org/repo.git
                For Azure DO: https://dev.azure.com/org/project/_git/repo
    --workdir   (Optional) Local directory to clone into.
                Defaults to ./workdir relative to this script's project root.
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import git
from git import GitCommandError, InvalidGitRepositoryError, Repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def inject_pat_into_url(repo_url: str, pat: str, username: str | None = None) -> str:
    """
    Embed a PAT token (and optional username) into an HTTPS remote URL so that
    git can authenticate without an interactive prompt.

    Format produced
    ---------------
    With username : ``https://<username>:<pat>@host/path``
    Without       : ``https://<pat>@host/path``

    Examples
    --------
    GitHub (token only) : https://x-access-token:<pat>@github.com/org/repo.git
    GitHub (user + PAT) : https://alice:<pat>@github.com/org/repo.git
    Azure DevOps        : https://alice:<pat>@dev.azure.com/org/project/_git/repo
    """
    parsed = urlparse(repo_url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"PAT injection is only supported for HTTP/HTTPS URLs, got: {repo_url!r}"
        )

    # Replace any existing credentials in the URL
    netloc_without_creds = parsed.hostname
    if parsed.port:
        netloc_without_creds = f"{netloc_without_creds}:{parsed.port}"

    # Build  username:pat  or  pat  depending on whether a username was supplied
    credentials = f"{username}:{pat}" if username else pat
    authenticated_netloc = f"{credentials}@{netloc_without_creds}"

    authenticated_url = urlunparse(
        (
            parsed.scheme,
            authenticated_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    return authenticated_url


def safe_display_url(repo_url: str) -> str:
    """Return the URL with the PAT token masked for safe logging."""
    # Replace anything between :// and @ with ***
    return re.sub(r"(https?://)([^@]+)@", r"\1***@", repo_url)


def make_sonarfix_branch_name(timestamp: datetime | None = None) -> str:
    """
    Generate a unique Sonar-fix branch name based on the current UTC timestamp.

    Format:  sonarfixes/<YYYYMMDD_HHMMSS>

    Parameters
    ----------
    timestamp : datetime | None
        Explicit datetime to use (UTC).  Defaults to ``datetime.now(timezone.utc)``.
        Injecting the timestamp makes the function deterministic in tests.

    Returns
    -------
    str
        Branch name, e.g. ``sonarfixes/20260227_153042``.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    return f"sonarfixes/{timestamp.strftime('%Y%m%d_%H%M%S')}"


def create_sonarfix_branch(repo: "Repo", branch_name: str) -> str:
    """
    Create a new local branch *branch_name* in *repo* from the current HEAD
    and check it out immediately.

    Parameters
    ----------
    repo : git.Repo
        An already-open GitPython Repo object.
    branch_name : str
        Name of the new branch to create (e.g. ``sonarfixes/20260227_153042``).

    Returns
    -------
    str
        The name of the newly created and checked-out branch.
    """
    repo.git.checkout("-b", branch_name)
    print(f"[OK] Created and checked out new branch '{branch_name}'.")
    return branch_name


def resolve_clone_target(workdir: Path, repo_url: str) -> Path:
    """
    Derive a sub-folder name from the repository URL so that multiple repos
    can coexist inside workdir without colliding.

    Examples
    --------
    https://github.com/org/my-repo.git  ->  workdir/my-repo
    https://dev.azure.com/org/project/_git/repo  ->  workdir/repo
    """
    name = Path(urlparse(repo_url).path).stem  # strip .git if present
    if not name:
        name = "repo"
    return workdir / name


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def checkout_repo(
    repo_url: str,
    branch: str,
    pat: str | None = None,
    workdir: Path | None = None,
    git_username: str | None = None,
) -> tuple[Path, str]:
    """
    Clone *repo_url* at *branch* into *workdir*, optionally using *pat* for auth.
    After checking out *branch* a new local branch ``sonarfixes/<timestamp>`` is
    created automatically and checked out ready for fixes to be applied.

    If the target directory already contains a valid Git repository it is
    updated (fetch + checkout) rather than re-cloned from scratch.

    Parameters
    ----------
    repo_url : str
        Remote repository URL (HTTPS or SSH).
    branch : str
        Branch name to checkout.
    pat : str | None
        Personal Access Token.  When supplied the token is injected into the
        HTTPS URL so the clone is non-interactive.
    workdir : Path | None
        Root working directory.  Defaults to <project_root>/workdir.
    git_username : str | None
        Git username to pair with the PAT (e.g. ``"x-access-token"`` for
        GitHub, or an Azure DevOps username).  When ``None`` the PAT is used
        as the credential on its own (``https://<pat>@host/...``).

    Returns
    -------
    tuple[Path, str]
        ``(clone_path, fix_branch_name)`` where *clone_path* is the absolute
        path to the cloned repository and *fix_branch_name* is the newly
        created ``sonarfixes/<timestamp>`` branch name.
    """
    # Default workdir is two levels up from this file (project root) / workdir
    project_root = Path(__file__).resolve().parent.parent
    if workdir is None:
        workdir = project_root / "workdir"

    workdir.mkdir(parents=True, exist_ok=True)

    # Determine the effective URL (with PAT / username if supplied)
    effective_url = inject_pat_into_url(repo_url, pat, username=git_username) if pat else repo_url
    display_url = safe_display_url(effective_url)

    target_dir = resolve_clone_target(workdir, repo_url)

    # ---- Update existing clone ----------------------------------------
    if target_dir.exists():
        try:
            repo = Repo(target_dir)
            print(f"[INFO] Existing repo found at '{target_dir}'. Fetching updates...")
            origin = repo.remotes.origin
            # Re-set URL in case it changed (e.g. PAT rotated)
            origin.set_url(effective_url)
            origin.fetch()
            repo.git.checkout(branch)
            repo.git.pull("origin", branch)
            print(f"[OK] Branch '{branch}' updated successfully.")
            fix_branch = create_sonarfix_branch(repo, make_sonarfix_branch_name())
            return target_dir.resolve(), fix_branch
        except InvalidGitRepositoryError:
            print(
                f"[WARN] '{target_dir}' exists but is not a valid Git repo. "
                "Removing and re-cloning..."
            )
            import shutil
            shutil.rmtree(target_dir)

    # ---- Fresh clone -----------------------------------------------------
    print(f"[INFO] Cloning {display_url} (branch: {branch}) into '{target_dir}' ...")
    try:
        repo = Repo.clone_from(
            effective_url,
            target_dir,
            branch=branch,
            depth=1,           # shallow clone for speed; remove if full history is needed
        )
    except GitCommandError as exc:
        print(f"[ERROR] Clone failed:\n{exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Repository cloned to '{target_dir}'.")
    fix_branch = create_sonarfix_branch(repo, make_sonarfix_branch_name())
    return target_dir.resolve(), fix_branch


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone an external Git repository into the local workdir.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="URL",
        help="URL (or path) of the remote repository to clone.",
    )
    parser.add_argument(
        "--branch",
        required=True,
        metavar="BRANCH",
        help="Branch name to checkout.",
    )
    parser.add_argument(
        "--pat",
        default=None,
        metavar="TOKEN",
        help=(
            "Personal Access Token for HTTPS authentication. "
            "If omitted the URL is used as-is (SSH keys / public repos)."
        ),
    )
    parser.add_argument(
        "--workdir",
        default=None,
        metavar="PATH",
        help=(
            "Root directory where repos are cloned. "
            "Defaults to <project_root>/workdir."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    workdir = Path(args.workdir) if args.workdir else None

    clone_path, fix_branch = checkout_repo(
        repo_url=args.repo,
        branch=args.branch,
        pat=args.pat,
        workdir=workdir,
    )

    print(f"\nRepository is available at : {clone_path}")
    print(f"Sonar-fix branch           : {fix_branch}")


if __name__ == "__main__":
    main()
