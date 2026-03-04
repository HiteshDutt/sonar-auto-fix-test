"""
Tests for ingestion/component_parser.py
"""
from __future__ import annotations

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ingestion.component_parser import ComponentParser, ComponentInfo, extract_relative_path


# ---------------------------------------------------------------------------
# _split_component
# ---------------------------------------------------------------------------

class TestSplitComponent:
    def test_three_part_format(self):
        """Format A: repoKey:branch:filePath"""
        parser = ComponentParser(repo_url="https://github.com/org/repo.git", branch="main")
        info = parser.parse("my-api:main:src/api/Program.cs")
        assert info.repo_key == "my-api"
        assert info.branch == "main"
        assert info.relative_path == "src/api/Program.cs"

    def test_two_part_sonarqube_format(self):
        """Format B: projectKey_branch:filePath"""
        parser = ComponentParser(repo_url="https://github.com/org/repo.git", branch="main")
        info = parser.parse("EMRSN-MSOL-MAS-API_main:src/api/Mas.Api.WebApi/Program.cs")
        assert info.relative_path == "src/api/Mas.Api.WebApi/Program.cs"

    def test_branch_override(self):
        """CLI --branch should override whatever is parsed from component."""
        parser = ComponentParser(
            repo_url="https://github.com/org/repo.git",
            branch="feature/my-branch",
        )
        info = parser.parse("myrepo:main:src/file.cs")
        assert info.branch == "feature/my-branch"

    def test_repo_url_override(self):
        """CLI --repo should always win regardless of component contents."""
        explicit_url = "https://github.com/org/explicit-repo.git"
        parser = ComponentParser(repo_url=explicit_url)
        info = parser.parse("somekey:develop:src/x.py")
        assert info.repo_url == explicit_url

    def test_github_base_url_construction(self):
        """When no repo_url is given, construct URL from github_base_url + repo key."""
        parser = ComponentParser(
            github_base_url="https://github.com/myorg",
            branch="main",
        )
        info = parser.parse("my-service:main:src/main.py")
        assert info.repo_url == "https://github.com/myorg/my-service.git"

    def test_no_repo_url_raises(self):
        """Neither repo_url nor github_base_url → ValueError."""
        parser = ComponentParser()
        with pytest.raises(ValueError, match="Cannot determine the repository URL"):
            parser.parse("some-key:main:src/file.py")

    def test_empty_component_raises(self):
        parser = ComponentParser(repo_url="https://github.com/org/repo.git")
        with pytest.raises(ValueError, match="empty"):
            parser.parse("")

    def test_path_with_colons_preserved(self):
        """Windows-style drive paths (rare, but should not break the parser)."""
        parser = ComponentParser(repo_url="https://github.com/org/repo.git", branch="main")
        info = parser.parse("my-repo:main:src/Dir:File.cs")
        # The path after the second colon is preserved
        assert "src/Dir:File.cs" == info.relative_path


# ---------------------------------------------------------------------------
# extract_relative_path convenience function
# ---------------------------------------------------------------------------

class TestExtractRelativePath:
    def test_three_part(self):
        assert extract_relative_path("repo:branch:src/x.cs") == "src/x.cs"

    def test_two_part(self):
        assert extract_relative_path("project_main:src/x.cs") == "src/x.cs"

    def test_single_part_returns_empty(self):
        assert extract_relative_path("justkey") == ""
