"""
orchestrator.py

Top-level coordinator for the Sonar Auto-Fix pipeline.

Pipeline steps
--------------
1. Read the Excel workbook (ExcelReader) to collect all OPEN issues.
2. Group issues by rule (preserving severity order — highest first).
3. Clone / update the target repository and create a ``sonarfixes/<ts>``
   branch (reuses ``repo_checkout.checkout_repo``).
4. For each rule:
   a. Run all per-issue fixes through the Copilot SDK (SonarFixEngine).
   b. Commit every changed file with a message that references the rule.
5. Push the fix branch and open a Pull Request back to the base branch
   (reuses ``pr_publisher.publish_and_create_pr``).
6. Return a :class:`RunSummary` with per-issue outcomes.

Usage (programmatic)
---------------------
    import asyncio
    from pathlib import Path
    from orchestration.orchestrator import Orchestrator, OrchestratorConfig

    cfg = OrchestratorConfig(
        excel_path=Path("data/issues.xlsx"),
        repo_url="https://github.com/org/my-api.git",
        branch="main",
        pat="ghp_...",
        model="auto",           # or "claude-sonnet-4-5", "gpt-4o", etc.
        allowed_rules=None,     # None = fix all rules
        severity_threshold=None,  # None = fix all severities
    )
    summary = asyncio.run(Orchestrator(cfg).run())
    print(summary)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ensure src/ is on sys.path for sibling imports when run as a script
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ingestion.excel_reader import ExcelReader, IssueModel, RuleInfo
from execution.sonar_fix_engine import FixResult, SonarFixEngine, SonarFixError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorConfig:
    """All settings required to run the full auto-fix pipeline."""

    # Source data
    excel_path: Path
    """Path to the ``.xlsx`` workbook with SonarQube issue export."""

    # Target repository
    repo_url: str
    """HTTPS clone URL of the repository containing the buggy code."""

    branch: str
    """Git branch to check out and target as the PR base."""

    # Authentication
    pat: Optional[str] = None
    """GitHub Personal Access Token (PAT).  Required for private repos and
    PR creation.  When ``None`` the tool uses SSH / CLI credentials."""

    # Copilot SDK
    model: Optional[str] = None
    """Model to use (e.g. ``"claude-sonnet-4-5"``).  ``None`` / ``"auto"``
    uses whatever is configured in the Copilot CLI."""

    github_token: Optional[str] = None
    """GitHub OAuth token passed to the Copilot SDK.  Falls back to *pat*
    if not set separately."""

    # Filtering
    allowed_rules: Optional[set[str]] = None
    """If set, only rules whose key appears in this set are processed."""

    severity_threshold: Optional[str] = None
    """Minimum severity level: INFO | MINOR | MAJOR | CRITICAL | BLOCKER."""

    # PR settings
    pr_title: Optional[str] = None
    """Pull Request title.  Auto-generated when ``None``."""

    pr_body: Optional[str] = None
    """Pull Request body.  Auto-generated when ``None``."""

    # Internals
    workdir: Optional[Path] = None
    """Root directory for cloned repos.  Defaults to ``<project_root>/workdir``."""

    git_username: Optional[str] = None
    """Git username paired with *pat* for HTTPS clone authentication.
    Examples: ``"x-access-token"`` (GitHub), ``"myuser"`` (Azure DevOps).
    When ``None`` the PAT alone is used as the credential."""

    issue_timeout: float = 300.0
    """Seconds to wait for the Copilot agent per issue (default 5 min)."""


# ---------------------------------------------------------------------------
# Summary / result types
# ---------------------------------------------------------------------------

@dataclass
class IssueOutcome:
    """Final status of one SonarQube issue after the pipeline ran."""
    issue_key: str
    rule_key: str
    file_path: str
    line: int
    fixed: bool
    summary: str
    error: str = ""


@dataclass
class RunSummary:
    """Aggregated result of an :class:`Orchestrator` run."""
    clone_path: Path
    fix_branch: str
    pr_url: str
    outcomes: list[IssueOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def fixed(self) -> int:
        return sum(1 for o in self.outcomes if o.fixed)

    @property
    def failed(self) -> int:
        return self.total - self.fixed

    def __str__(self) -> str:  # noqa: D105
        lines = [
            f"Run Summary",
            f"  Clone path : {self.clone_path}",
            f"  Fix branch : {self.fix_branch}",
            f"  PR URL     : {self.pr_url or '(no PR created)'}",
            f"  Total      : {self.total}",
            f"  Fixed      : {self.fixed}",
            f"  Failed     : {self.failed}",
        ]
        if self.failed:
            lines.append("  Failed issues:")
            for o in self.outcomes:
                if not o.fixed:
                    lines.append(f"    [{o.rule_key}] {o.issue_key} — {o.error or o.summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Runs the end-to-end Sonar Auto-Fix pipeline.

    Parameters
    ----------
    config : OrchestratorConfig
        Full pipeline configuration.
    """

    def __init__(self, config: OrchestratorConfig) -> None:
        self._cfg = config

    async def run(self) -> RunSummary:
        """
        Execute the full pipeline and return a :class:`RunSummary`.

        This method is safe to call from a ``asyncio.run()`` context.
        """
        cfg = self._cfg

        # ---------------------------------------------------------------
        # Step 1 — Clone / update repo
        # Step 2 — Check out the target branch
        # Step 3 — Create the sonarfixes/<timestamp> fix branch
        # ---------------------------------------------------------------
        logger.info(
            "[Step 1] Cloning '%s' @ branch '%s'.", cfg.repo_url, cfg.branch
        )
        from repo_checkout import checkout_repo

        clone_path, fix_branch = checkout_repo(
            repo_url=cfg.repo_url,
            branch=cfg.branch,
            pat=cfg.pat,
            workdir=cfg.workdir,
            git_username=cfg.git_username,
        )
        logger.info(
            "[Step 2/3] Repository cloned at '%s'; target branch '%s' checked out; "
            "fix branch '%s' created.",
            clone_path, cfg.branch, fix_branch,
        )

        # ---------------------------------------------------------------
        # Step 4 — Read issues from Excel
        # ---------------------------------------------------------------
        logger.info("[Step 4] Reading issues from '%s'.", cfg.excel_path)
        with ExcelReader(
            cfg.excel_path,
            severity_threshold=cfg.severity_threshold,
        ) as reader:
            issues = reader.load_all_issues(allowed_rules=cfg.allowed_rules)

        if not issues:
            logger.warning("[Orchestrator] No actionable issues found. Exiting early.")
            return RunSummary(clone_path=clone_path, fix_branch=fix_branch, pr_url="")

        logger.info("[Orchestrator] %d actionable issue(s) to process.", len(issues))

        # ---------------------------------------------------------------
        # Group issues by rule (prep for Step 5 per-rule fix loop)
        # ---------------------------------------------------------------
        rules_seen: dict[str, RuleInfo] = {}
        issues_by_rule: dict[str, list[IssueModel]] = {}
        for issue in issues:
            rule = issue.rule
            if rule is None:
                continue
            rules_seen.setdefault(rule.key, rule)
            issues_by_rule.setdefault(rule.key, []).append(issue)

        # ---------------------------------------------------------------
        # Step 5 — Fix rule by rule; commit after each sheet/rule
        # ---------------------------------------------------------------
        all_outcomes: list[IssueOutcome] = []

        effective_token = cfg.github_token or cfg.pat

        async with SonarFixEngine(
            repo_path=clone_path,
            model=cfg.model,
            github_token=effective_token,
            issue_timeout=cfg.issue_timeout,
        ) as engine:
            for rule_key, rule in rules_seen.items():
                rule_issues = issues_by_rule.get(rule_key, [])
                logger.info(
                    "[Orchestrator] Fixing rule '%s' (%d issue(s)).",
                    rule_key, len(rule_issues),
                )

                results: list[FixResult] = []
                try:
                    results = await engine.fix_rule(rule, rule_issues)
                except SonarFixError as exc:
                    logger.error(
                        "[Orchestrator] Engine error for rule '%s': %s", rule_key, exc
                    )
                    # Mark all issues in this rule as failed
                    results = [
                        FixResult(
                            issue_key=iss.key,
                            success=False,
                            summary="Engine error",
                            error=str(exc),
                        )
                        for iss in rule_issues
                    ]

                # Map results back to outcomes
                result_map = {r.issue_key: r for r in results}
                for issue in rule_issues:
                    res = result_map.get(issue.key, FixResult(
                        issue_key=issue.key, success=False,
                        summary="No result returned", error="missing result"
                    ))
                    from ingestion.component_parser import extract_relative_path
                    all_outcomes.append(IssueOutcome(
                        issue_key=issue.key,
                        rule_key=rule_key,
                        file_path=extract_relative_path(issue.component) or issue.component,
                        line=issue.line,
                        fixed=res.success,
                        summary=res.summary,
                        error=res.error,
                    ))

                # Step 5 — Commit changes for this rule (after each sheet/rule)
                fix_count = sum(1 for r in results if r.success)
                # Use the rule key as the commit message (rule identifier)
                commit_msg = rule_key
                try:
                    from pr_publisher import commit_changes
                    commit_changes(clone_path, commit_msg)
                    logger.info(
                        "[Step 5] Committed changes for rule '%s' (message: '%s').",
                        rule_key, commit_msg,
                    )
                except ValueError as exc:
                    # No changes on disk — not fatal, log and continue
                    logger.info(
                        "[Orchestrator] Nothing to commit for rule '%s': %s",
                        rule_key, exc,
                    )

        # ---------------------------------------------------------------
        # Step 6 — Push fix branch and open PR targeting --branch
        # Always push the branch (even if no fixes succeeded) so the work
        # is not lost, and create a PR when a token is available.
        # ---------------------------------------------------------------
        any_fixed = any(o.fixed for o in all_outcomes)
        pr_url = ""

        if not any_fixed:
            logger.warning(
                "[Orchestrator] No issues were marked as successfully fixed. "
                "Branch will still be pushed for manual review."
            )

        # Prefer github_token over pat for API calls; both are acceptable PATs.
        effective_pr_token = cfg.github_token or cfg.pat

        pr_title = cfg.pr_title or _default_pr_title(fix_branch, all_outcomes)
        pr_body = cfg.pr_body or _default_pr_body(fix_branch, cfg.branch, all_outcomes)

        logger.info(
            "[Step 6] Pushing fix branch '%s' and creating PR → '%s' on %s.",
            fix_branch, cfg.branch, cfg.repo_url,
        )

        try:
            from pr_publisher import publish_and_create_pr
            pr_url = publish_and_create_pr(
                clone_dir=clone_path,
                repo_url=cfg.repo_url,
                fix_branch=fix_branch,
                base_branch=cfg.branch,
                commit_message="chore: final auto-fix batch commit",
                pr_title=pr_title,
                pr_body=pr_body,
                pat=effective_pr_token,
            )
        except ValueError as exc:
            # No uncommitted changes remain — all commits were done per rule.
            logger.info(
                "[Orchestrator] No uncommitted changes remain at push time: %s", exc
            )
            # Still push the already-committed branch and open the PR.
            from pr_publisher import push_fix_branch, create_pull_request
            push_fix_branch(clone_path, fix_branch, repo_url=cfg.repo_url, pat=effective_pr_token)
            if effective_pr_token:
                pr_url = create_pull_request(
                    repo_url=cfg.repo_url,
                    fix_branch=fix_branch,
                    base_branch=cfg.branch,
                    title=pr_title,
                    body=pr_body,
                    pat=effective_pr_token,
                )
            else:
                logger.warning(
                    "[Step 6] No PAT or GitHub token provided — branch was pushed but "
                    "PR was NOT created. Re-run with --pat <token> or --github-token <token> "
                    "to enable automatic PR creation."
                )

        summary = RunSummary(
            clone_path=clone_path,
            fix_branch=fix_branch,
            pr_url=pr_url,
            outcomes=all_outcomes,
        )
        logger.info("[Orchestrator] Run complete.\n%s", summary)
        return summary


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _build_commit_message(rule: RuleInfo, results: list[FixResult]) -> str:
    """
    Build a commit message using the rule identifier as the message.
    """
    return rule.key


def _default_pr_title(fix_branch: str, outcomes: list[IssueOutcome]) -> str:
    n = sum(1 for o in outcomes if o.fixed)
    rules = len({o.rule_key for o in outcomes if o.fixed})
    return f"fix(sonar): auto-fix {n} issue(s) across {rules} rule(s) [{fix_branch}]"


def _default_pr_body(
    fix_branch: str,
    base_branch: str,
    outcomes: list[IssueOutcome],
) -> str:
    fixed = [o for o in outcomes if o.fixed]
    failed = [o for o in outcomes if not o.fixed]

    rules_fixed: dict[str, list[IssueOutcome]] = {}
    for o in fixed:
        rules_fixed.setdefault(o.rule_key, []).append(o)

    lines = [
        "## Sonar Auto-Fix — Automated Pull Request",
        "",
        f"**Fix branch:** `{fix_branch}`  ",
        f"**Target branch:** `{base_branch}`  ",
        f"**Total issues fixed:** {len(fixed)} / {len(outcomes)}  ",
        "",
        "### Fixed Issues by Rule",
        "",
    ]

    for rule_key, rule_outcomes in rules_fixed.items():
        lines.append(f"#### `{rule_key}` — {len(rule_outcomes)} fix(es)")
        for o in rule_outcomes:
            lines.append(f"- `{o.file_path}` (line {o.line}): {o.summary[:120]}")
        lines.append("")

    if failed:
        lines.append("### Issues Not Fixed")
        lines.append("")
        for o in failed:
            reason = o.error or o.summary
            lines.append(f"- `{o.issue_key}` [{o.rule_key}] — {reason[:120]}")
        lines.append("")

    lines += [
        "---",
        "_Generated automatically by the Sonar Auto-Fix Platform using the "
        "GitHub Copilot SDK.  Please review the changes carefully before merging._",
    ]
    return "\n".join(lines)
