"""
component_parser.py

Parses the SonarQube ``component`` column value into its constituent parts:
repository key, branch, and relative file path.

SonarQube encodes the file location as a colon-separated string.  Two common
formats are observed in the wild:

Format A (3-part) — repo key, branch, and file path as separate segments:
    ``<RepoKey>:<Branch>:<src/relative/path/to/File.cs>``

Format B (2-part) — project key (which may encode the branch as a suffix) and
file path only:
    ``<ProjectKey_Branch>:<src/relative/path/to/File.cs>``

Examples
--------
Format A:  ``my-api:main:src/api/Program.cs``
           → repo_key="my-api", branch="main", file_path="src/api/Program.cs"

Format B:  ``EMRSN-MSOL-MAS-API_main:src/api/Mas.Api.WebApi/Program.cs``
           → repo_key="EMRSN-MSOL-MAS-API", branch="main",
             file_path="src/api/Mas.Api.WebApi/Program.cs"

When the caller already knows the repository URL and branch (supplied on the
command line), these override the values parsed from the component string.

Public API
----------
    from ingestion.component_parser import ComponentParser, ComponentInfo

    parser = ComponentParser(
        repo_url="https://github.com/org/my-api.git",
        branch="main",
        github_base_url="https://github.com/myorg",  # optional
    )
    info = parser.parse("my-api:main:src/api/Program.cs")
    print(info.relative_path)  # "src/api/Program.cs"
    print(info.repo_url)       # "https://github.com/org/my-api.git"
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ComponentInfo:
    """Parsed result of a SonarQube ``component`` field value."""
    repo_key: str           # SonarQube project key / repo name portion
    branch: str             # Git branch derived from the component or CLI arg
    relative_path: str      # Repo-relative file path (e.g. src/api/Program.cs)
    repo_url: str           # Full GitHub/ADO clone URL (HTTPS)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ComponentParser:
    """
    Converts a raw SonarQube ``component`` string into a :class:`ComponentInfo`.

    Parameters
    ----------
    repo_url : str | None
        If supplied, the resolved :attr:`ComponentInfo.repo_url` will always be
        this value.  When ``None`` the parser attempts to construct a URL from
        *github_base_url* and the repo key extracted from the component string.
    branch : str | None
        When supplied this branch overrides anything parsed from the component
        string.
    github_base_url : str | None
        Base URL used when constructing repo URLs from the repo key (e.g.
        ``"https://github.com/myorg"``).  Required when *repo_url* is ``None``.
    """

    def __init__(
        self,
        repo_url: str | None = None,
        branch: str | None = None,
        github_base_url: str | None = None,
    ) -> None:
        self._repo_url = repo_url
        self._branch_override = branch
        self._github_base_url = (github_base_url or "").rstrip("/")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def parse(self, component: str) -> ComponentInfo:
        """
        Parse *component* and return a :class:`ComponentInfo`.

        Parameters
        ----------
        component : str
            Raw value from the SonarQube ``component`` Excel column.

        Returns
        -------
        ComponentInfo

        Raises
        ------
        ValueError
            If the file path cannot be extracted or the repo URL cannot be
            determined.
        """
        component = component.strip()
        if not component:
            raise ValueError("component field is empty.")

        repo_key, branch, relative_path = self._split_component(component)

        # CLI-supplied overrides take precedence
        if self._branch_override:
            branch = self._branch_override

        # Resolve the repo URL
        if self._repo_url:
            repo_url = self._repo_url
        else:
            repo_url = self._build_repo_url(repo_key)

        if not relative_path:
            raise ValueError(
                f"Could not extract a file path from component: {component!r}"
            )

        return ComponentInfo(
            repo_key=repo_key,
            branch=branch,
            relative_path=relative_path,
            repo_url=repo_url,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _split_component(component: str) -> tuple[str, str, str]:
        """
        Split *component* into ``(repo_key, branch, file_path)``.

        Handles both 3-part (``key:branch:path``) and 2-part
        (``key_branch:path`` or ``key:path``) formats.
        """
        parts = component.split(":")

        if len(parts) >= 3:
            # Format A: repoKey:branch:path/to/file
            repo_key = parts[0]
            branch = parts[1]
            file_path = ":".join(parts[2:])  # handle Windows paths with drive letters
            return repo_key, branch, file_path

        if len(parts) == 2:
            # Format B: projectKey_branch:path/to/file
            raw_key, file_path = parts[0], parts[1]
            repo_key, branch = ComponentParser._split_repo_branch(raw_key)
            return repo_key, branch, file_path

        # Single segment — no file path extractable
        return component, "", ""

    @staticmethod
    def _split_repo_branch(raw_key: str) -> tuple[str, str]:
        """
        Try to split ``<repo>_<branch>`` into ``(repo, branch)``.

        Heuristic: split on the *last* underscore that is immediately followed
        by a plausible branch name (e.g. ``main``, ``develop``, ``feature/…``).
        If no recognisable branch suffix is found the full string is treated as
        the repo key with an empty branch.
        """
        # Common branch name suffixes after the last underscore
        _BRANCH_PATTERN = re.compile(
            r"^(.+?)_(main|master|develop|development|release|feature|hotfix|.+)$",
            re.IGNORECASE,
        )
        m = _BRANCH_PATTERN.match(raw_key)
        if m:
            return m.group(1), m.group(2)

        # Last-resort: just use the whole key as the repo name
        return raw_key, ""

    def _build_repo_url(self, repo_key: str) -> str:
        """
        Construct a clone URL from *repo_key* and ``github_base_url``.

        Parameters
        ----------
        repo_key : str
            The repository name / key extracted from the component string.

        Returns
        -------
        str
            Full HTTPS clone URL.

        Raises
        ------
        ValueError
            When neither ``repo_url`` nor ``github_base_url`` was supplied.
        """
        if not self._github_base_url:
            raise ValueError(
                "Cannot determine the repository URL: neither --repo nor "
                "--github-base-url was supplied, and the component field does "
                f"not encode a full URL (component key: {repo_key!r})."
            )
        # Normalise the repo key to a sensible slug (strip branch suffix)
        slug, _ = self._split_repo_branch(repo_key)
        return f"{self._github_base_url}/{slug}.git"


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def extract_relative_path(component: str) -> str:
    """
    Quick helper that extracts only the relative file path from *component*
    without constructing a full :class:`ComponentInfo`.

    Useful when the caller manages repo URL / branch independently.
    """
    _, _, path = ComponentParser._split_component(component)
    return path
