"""
sonar_fix_engine.py

Drives the GitHub Copilot SDK to fix SonarQube issues in a local Git clone.

The engine creates **one Copilot session per SonarQube rule** so that the
agent can build up context about the codebase while addressing all violations
of the same rule before moving on.

Within each session, issues are processed sequentially.  After the agent
reports it is idle (``session.idle`` event), the result is inspected and the
engine moves on to the next issue.

Usage (programmatic)
---------------------
    import asyncio
    from pathlib import Path
    from execution.sonar_fix_engine import SonarFixEngine, FixResult
    from ingestion.excel_reader import IssueModel, RuleInfo

    async def fix():
        engine = SonarFixEngine(
            repo_path=Path("./workdir/my-repo"),
            model="claude-sonnet-4-5",   # or None / "auto" for the SDK default
            github_token="ghp_...",      # optional; uses CLI login if omitted
        )
        await engine.start()

        results = await engine.fix_rule(rule, issues)
        for res in results:
            print(res.issue_key, res.success, res.summary)

        await engine.stop()

``fix_rule`` will raise ``SonarFixError`` only on unrecoverable engine errors;
per-issue failures are captured inside :class:`FixResult` rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FixResult:
    """Outcome of a single issue fix attempt."""
    issue_key: str
    success: bool          # True if the agent applied a fix without errors
    summary: str           # Human-readable description of what was done
    error: str = ""        # Set when success=False


class SonarFixError(RuntimeError):
    """Raised when the fix engine encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_SYSTEM_MESSAGE = """You are an automated code-quality agent fixing SonarQube \
static-analysis issues in a software repository.  You have full access to the \
file system and can read and modify source files.

Rules:
- Fix ONLY the specific issue described in each user message.
- Do NOT refactor, rename, or change anything beyond the minimum required fix.
- Preserve the original file encoding, line endings, and indentation style.
- After applying the fix, save the file.
- If the issue is already fixed, report that no change was needed.
- Do NOT add explanatory comments about the fix unless the rule explicitly \
  requires a documentation-style comment.
"""


def _build_fix_prompt(
    rule_key: str,
    rule_name: str,
    rule_severity: str,
    issue_message: str,
    file_path: str,
    line: int,
    issue_key: str,
) -> str:
    """
    Build the user-turn prompt that asks the agent to fix one issue.

    Parameters
    ----------
    rule_key : str     SonarQube rule key (e.g. ``cs-S1006``).
    rule_name : str    Human-readable rule name.
    rule_severity : str  BLOCKER | CRITICAL | MAJOR | MINOR | INFO.
    issue_message : str  Issue-specific description from Sonar.
    file_path : str    Repo-relative path to the file containing the issue.
    line : int         Line number where the issue was detected.
    issue_key : str    Sonar issue UUID (for traceability).
    """
    return (
        f"## SonarQube Issue  \n"
        f"**Issue ID:** `{issue_key}`  \n"
        f"**Rule:** `{rule_key}` — {rule_name}  \n"
        f"**Severity:** {rule_severity}  \n"
        f"**File:** `{file_path}`  \n"
        f"**Line:** {line}  \n"
        f"**Message:** {issue_message}  \n"
        f"\n"
        f"Please:\n"
        f"1. Read the file `{file_path}`.\n"
        f"2. Locate and understand the problem at or around line {line}.\n"
        f"3. Apply the minimal fix that satisfies the rule `{rule_key}`.\n"
        f"4. Save the updated file.\n"
        f"5. Briefly summarise what you changed (or confirm no change was needed).\n"
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SonarFixEngine:
    """
    Orchestrates the GitHub Copilot SDK to apply SonarQube fixes.

    Parameters
    ----------
    repo_path : Path
        Absolute path to the local clone of the target repository.  The
        Copilot CLI will use this as its working directory.
    model : str | None
        Model identifier (e.g. ``"claude-sonnet-4-5"``, ``"gpt-4o"``).
        Pass ``None`` or ``"auto"`` to use whichever model is configured in
        the Copilot CLI.
    github_token : str | None
        GitHub OAuth token for Copilot authentication.  When ``None`` the CLI
        falls back to the token stored by ``gh auth login`` / ``copilot login``.
    issue_timeout : float
        Maximum seconds to wait for the agent to become idle after each
        per-issue prompt.  Defaults to 300 s (5 min).
    """

    def __init__(
        self,
        repo_path: Path,
        model: str | None = None,
        github_token: str | None = None,
        issue_timeout: float = 300.0,
    ) -> None:
        self._repo_path = repo_path
        self._model = None if model in (None, "", "auto") else model
        self._github_token = github_token
        self._issue_timeout = issue_timeout
        self._client = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise and start the underlying Copilot SDK client."""
        try:
            from copilot import CopilotClient  # type: ignore[import]
        except ImportError as exc:
            raise SonarFixError(
                "The 'github-copilot-sdk' package is not installed.  "
                "Run: pip install github-copilot-sdk"
            ) from exc

        client_opts: dict = {
            "cwd": str(self._repo_path),
            "auto_start": True,
            "auto_restart": True,
        }
        if self._github_token:
            client_opts["github_token"] = self._github_token

        self._client = CopilotClient(client_opts)
        await self._client.start()
        logger.info("[CopilotClient] Started; cwd='%s'.", self._repo_path)

    async def stop(self) -> None:
        """Shut down the Copilot SDK client."""
        if self._client is not None:
            await self._client.stop()
            self._client = None
            logger.info("[CopilotClient] Stopped.")

    async def __aenter__(self) -> "SonarFixEngine":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def fix_rule(
        self,
        rule,          # RuleInfo
        issues: list,  # list[IssueModel]
    ) -> list[FixResult]:
        """
        Open one Copilot session for *rule* and fix every issue sequentially.

        Parameters
        ----------
        rule : RuleInfo
            The SonarQube rule being addressed.
        issues : list[IssueModel]
            Issues belonging to *rule* that need to be fixed.

        Returns
        -------
        list[FixResult]
            One :class:`FixResult` per issue.  Failures are captured inside the
            result rather than raised.
        """
        if self._client is None:
            raise SonarFixError("Engine not started; call await engine.start() first.")

        if not issues:
            return []

        logger.info(
            "[Rule %s] Opening Copilot session for %d issue(s).",
            rule.key, len(issues),
        )

        from copilot import PermissionHandler  # type: ignore[import]

        session_config: dict = {
            "system_message": {"content": _SYSTEM_MESSAGE},
            "on_permission_request": PermissionHandler.approve_all,
        }
        if self._model:
            session_config["model"] = self._model

        session = await self._client.create_session(session_config)
        results: list[FixResult] = []

        try:
            for issue in issues:
                result = await self._fix_single_issue(session, rule, issue)
                results.append(result)
                status = "OK" if result.success else "FAIL"
                logger.info(
                    "[Rule %s | Issue %s] %s — %s",
                    rule.key, issue.key, status, result.summary,
                )
        finally:
            await session.destroy()
            logger.info("[Rule %s] Copilot session closed.", rule.key)

        return results

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _fix_single_issue(self, session, rule, issue) -> FixResult:
        """Send one fix prompt and await the agent's idle state."""
        from ingestion.component_parser import extract_relative_path  # local import

        try:
            file_path = extract_relative_path(issue.component) or issue.component
        except Exception:
            file_path = issue.component

        prompt = _build_fix_prompt(
            rule_key=rule.key,
            rule_name=rule.name,
            rule_severity=rule.severity,
            issue_message=issue.message,
            file_path=file_path,
            line=issue.line,
            issue_key=issue.key,
        )

        done = asyncio.Event()
        response_content: list[str] = []
        error_details: list[str] = []

        def on_event(event):
            evt_type = (
                event.type.value
                if hasattr(event.type, "value")
                else str(event.type)
            )

            if evt_type == "assistant.message":
                content = getattr(event.data, "content", "") or ""
                response_content.append(content)

            elif evt_type == "session.idle":
                done.set()

            elif evt_type in ("tool.error", "session.error"):
                err = getattr(event.data, "error", str(event.data)) or ""
                error_details.append(str(err))
                done.set()  # unblock even on errors

        unsub = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await asyncio.wait_for(done.wait(), timeout=self._issue_timeout)
        except asyncio.TimeoutError:
            return FixResult(
                issue_key=issue.key,
                success=False,
                summary="Timed out waiting for the Copilot agent.",
                error="TimeoutError",
            )
        except Exception as exc:  # noqa: BLE001
            return FixResult(
                issue_key=issue.key,
                success=False,
                summary=f"Unexpected error: {exc}",
                error=str(exc),
            )
        finally:
            unsub()

        full_response = "\n".join(response_content).strip()

        if error_details:
            return FixResult(
                issue_key=issue.key,
                success=False,
                summary=full_response or "Agent reported an error.",
                error="; ".join(error_details),
            )

        return FixResult(
            issue_key=issue.key,
            success=True,
            summary=full_response or "Fix applied (no summary returned by agent).",
        )
