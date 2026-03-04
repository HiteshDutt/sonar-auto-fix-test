"""
sonar_autofix.py

Main CLI entry-point for the Sonar Auto-Fix Platform.

End-to-end pipeline:
    1. Parse the SonarQube issue export workbook (``--excel``).
    2. Clone the target repository at the specified branch (``--repo``, ``--branch``).
    3. Create a ``sonarfixes/<timestamp>`` working branch.
    4. Invoke the GitHub Copilot SDK to fix each issue, rule by rule.
    5. Commit each rule's batch of fixes with a descriptive commit message.
    6. Push the fix branch to GitHub and open a Pull Request against the
       original branch.

Usage
-----
    python src/sonar_autofix.py \\
        --excel  data/issues.xlsx \\
        --repo   https://github.com/org/my-repo.git \\
        --branch main \\
        --pat    ghp_xxxxxxxxxxxxxxxxxxxx \\
        [--model auto | claude-sonnet-4-5 | gpt-4o | ...] \\
        [--rules cs-S1006,cs-S1110] \\
        [--severity MINOR] \\
        [--workdir ./workdir] \\
        [--pr-title "Sonar auto-fixes"] \\
        [--pr-body  "..."] \\
        [--github-token ghp_xxx] \\
        [--timeout 300] \\
        [--log-level INFO]

Arguments
---------
    --excel         Path to the ``.xlsx`` workbook (Sheet 1: Instructions,
                    Sheet 2: Rules, Sheet 3…N: per-rule issues).           [required]
    --repo          HTTPS URL of the target repository to clone.           [required]
    --branch        Branch to check out as the fix base.                   [required]
    --pat           GitHub Personal Access Token for push + PR API.        [optional]
    --model         Copilot model identifier.  Omit or use "auto" to rely
                    on the model configured in the Copilot CLI.            [optional]
    --rules         Comma-separated list of Sonar rule keys to process.
                    Omit to fix all OPEN issues regardless of rule.       [optional]
    --severity      Minimum severity to process: INFO|MINOR|MAJOR|
                    CRITICAL|BLOCKER.  Issues below this level are skipped.[optional]
    --workdir       Directory where repos are cloned.
                    Defaults to ./workdir relative to this project.       [optional]
    --pr-title      Custom Pull Request title.                             [optional]
    --pr-body       Custom Pull Request body (Markdown).                   [optional]
    --github-token  Separate GitHub token for the Copilot SDK (falls back
                    to --pat when not supplied).                           [optional]
    --timeout       Per-issue agent timeout in seconds (default: 300).    [optional]
    --log-level     Python logging level: DEBUG|INFO|WARNING|ERROR.       [optional]

Exit codes
----------
    0  — All issues successfully fixed and PR created (or no issues found).
    1  — At least one issue could not be fixed (partial success).
    2  — Fatal error (bad arguments, clone failure, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sonar_autofix",
        description=(
            "Sonar Auto-Fix Platform — reads a SonarQube Excel export, clones "
            "the target repository, fixes issues using the GitHub Copilot SDK, "
            "and opens a Pull Request with the fixes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Required
    req = parser.add_argument_group("required arguments")
    req.add_argument(
        "--excel",
        required=True,
        metavar="PATH",
        help="Path to the SonarQube issue export workbook (.xlsx).",
    )
    req.add_argument(
        "--repo",
        required=True,
        metavar="URL",
        help="HTTPS clone URL of the target repository.",
    )
    req.add_argument(
        "--branch",
        required=True,
        metavar="BRANCH",
        help="Branch to check out and target as the PR base.",
    )

    # Optional — authentication
    auth = parser.add_argument_group("authentication")
    auth.add_argument(
        "--pat",
        default=None,
        metavar="TOKEN",
        help=(
            "GitHub Personal Access Token (PAT).  Used for cloning private "
            "repos, pushing the fix branch, and creating the PR.  Also used "
            "as the Copilot SDK token when --github-token is not set."
        ),
    )
    auth.add_argument(
        "--github-token",
        default=None,
        metavar="TOKEN",
        help=(
            "GitHub OAuth token for the Copilot SDK specifically.  Falls back "
            "to --pat when omitted.  Can also be set via the GITHUB_TOKEN or "
            "GH_TOKEN environment variable (handled by the SDK automatically)."
        ),
    )

    # Optional — Copilot SDK
    sdk = parser.add_argument_group("copilot sdk")
    sdk.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "Model to use for the Copilot SDK (e.g. 'claude-sonnet-4-5', "
            "'gpt-4o').  Omit or pass 'auto' to use the model configured in "
            "the Copilot CLI."
        ),
    )
    sdk.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Per-issue agent timeout in seconds (default: 300).",
    )

    # Optional — filtering
    filt = parser.add_argument_group("filtering")
    filt.add_argument(
        "--rules",
        default=None,
        metavar="KEY,KEY,...",
        help=(
            "Comma-separated list of Sonar rule keys to process "
            "(e.g. 'cs-S1006,cs-S1110').  Omit to process all rules."
        ),
    )
    filt.add_argument(
        "--severity",
        default=None,
        metavar="LEVEL",
        choices=["INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER"],
        help=(
            "Minimum severity threshold.  Issues below this level are skipped "
            "(e.g. '--severity MAJOR' skips INFO and MINOR issues)."
        ),
    )

    # Optional — output / PR
    out = parser.add_argument_group("output")
    out.add_argument(
        "--workdir",
        default=None,
        metavar="PATH",
        help="Root directory for cloned repos (default: ./workdir).",
    )
    out.add_argument(
        "--pr-title",
        default=None,
        metavar="TITLE",
        help="Custom Pull Request title.",
    )
    out.add_argument(
        "--pr-body",
        default=None,
        metavar="BODY",
        help="Custom Pull Request body (Markdown).",
    )

    # Optional — diagnostics
    diag = parser.add_argument_group("diagnostics")
    diag.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level (default: INFO).",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.log_level)
    log = logging.getLogger("sonar_autofix")

    # Ensure the src directory is on the path for sibling imports
    src_dir = Path(__file__).resolve().parent
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    # Validate inputs
    excel_path = Path(args.excel)
    if not excel_path.exists():
        log.error("Excel file not found: '%s'", excel_path)
        sys.exit(2)

    allowed_rules: set[str] | None = None
    if args.rules:
        allowed_rules = {r.strip() for r in args.rules.split(",") if r.strip()}
        log.info("Rule filter: %s", sorted(allowed_rules))

    workdir = Path(args.workdir) if args.workdir else None

    # Build orchestrator config
    from orchestration.orchestrator import Orchestrator, OrchestratorConfig

    cfg = OrchestratorConfig(
        excel_path=excel_path,
        repo_url=args.repo,
        branch=args.branch,
        pat=args.pat,
        model=args.model or None,    # treat "auto" as None inside the engine
        github_token=args.github_token,
        allowed_rules=allowed_rules,
        severity_threshold=args.severity,
        pr_title=args.pr_title,
        pr_body=args.pr_body,
        workdir=workdir,
        issue_timeout=args.timeout,
    )

    log.info(
        "Starting Sonar Auto-Fix pipeline | repo=%s branch=%s model=%s",
        args.repo, args.branch, cfg.model or "auto (SDK default)",
    )

    try:
        summary = asyncio.run(Orchestrator(cfg).run())
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        log.exception("Fatal error: %s", exc)
        sys.exit(2)

    # Print summary
    print()
    print(summary)
    print()

    if summary.pr_url:
        print(f"Pull Request: {summary.pr_url}")

    # Exit with code 1 if any issues were not fixed
    sys.exit(0 if summary.failed == 0 else 1)


if __name__ == "__main__":
    main()
