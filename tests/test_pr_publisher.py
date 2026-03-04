"""
tests/test_pr_publisher.py

Unit tests for src/pr_publisher.py.

Run with:
    pytest tests/test_pr_publisher.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import requests

# Make src importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pr_publisher import (
    detect_platform,
    parse_github_repo,
    parse_azure_repo,
    commit_changes,
    push_fix_branch,
    create_github_pr,
    create_azure_pr,
    create_pull_request,
    publish_and_create_pr,
    parse_args,
)
from git import GitCommandError, InvalidGitRepositoryError


# ===========================================================================
# detect_platform
# ===========================================================================

class TestDetectPlatform:
    """Tests for detect_platform()."""

    def test_github_https_url(self):
        assert detect_platform("https://github.com/org/repo.git") == "github"

    def test_github_url_no_path(self):
        assert detect_platform("https://github.com/") == "github"

    def test_azure_devops_url(self):
        assert detect_platform("https://dev.azure.com/org/project/_git/repo") == "azure_devops"

    def test_azure_visualstudio_url(self):
        assert detect_platform("https://org.visualstudio.com/project/_git/repo") == "azure_devops"

    def test_unknown_platform(self):
        assert detect_platform("https://bitbucket.org/user/repo.git") == "unknown"

    def test_ssh_github_parsed_as_unknown(self):
        # SSH URLs have no recognized host via urlparse hostname
        result = detect_platform("git@github.com:org/repo.git")
        # hostname will be None for SSH, so returns unknown
        assert result == "unknown"

    def test_gitlab_returns_unknown(self):
        assert detect_platform("https://gitlab.com/org/repo.git") == "unknown"


# ===========================================================================
# parse_github_repo
# ===========================================================================

class TestParseGithubRepo:
    """Tests for parse_github_repo()."""

    def test_standard_https_url(self):
        owner, repo = parse_github_repo("https://github.com/myorg/my-repo.git")
        assert owner == "myorg"
        assert repo == "my-repo"

    def test_strips_git_suffix(self):
        _, repo = parse_github_repo("https://github.com/org/repo.git")
        assert not repo.endswith(".git")

    def test_url_without_git_suffix(self):
        owner, repo = parse_github_repo("https://github.com/owner/project")
        assert owner == "owner"
        assert repo == "project"

    def test_authenticated_url(self):
        owner, repo = parse_github_repo("https://token@github.com/myorg/repo.git")
        assert owner == "myorg"
        assert repo == "repo"

    def test_url_with_no_repo_path_raises(self):
        with pytest.raises(ValueError, match="Cannot parse GitHub owner/repo"):
            parse_github_repo("https://github.com/only-owner")

    def test_owner_and_repo_are_strings(self):
        owner, repo = parse_github_repo("https://github.com/org/repo.git")
        assert isinstance(owner, str)
        assert isinstance(repo, str)


# ===========================================================================
# parse_azure_repo
# ===========================================================================

class TestParseAzureRepo:
    """Tests for parse_azure_repo()."""

    def test_standard_azure_url(self):
        org, project, repo = parse_azure_repo(
            "https://dev.azure.com/myorg/myproject/_git/myrepo"
        )
        assert org == "myorg"
        assert project == "myproject"
        assert repo == "myrepo"

    def test_strips_git_suffix(self):
        _, _, repo = parse_azure_repo(
            "https://dev.azure.com/myorg/myproject/_git/myrepo.git"
        )
        assert not repo.endswith(".git")

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Azure DevOps"):
            parse_azure_repo("https://dev.azure.com/only-org")

    def test_return_types_are_strings(self):
        org, project, repo = parse_azure_repo(
            "https://dev.azure.com/org/proj/_git/repo"
        )
        assert all(isinstance(v, str) for v in (org, project, repo))


# ===========================================================================
# commit_changes
# ===========================================================================

class TestCommitChanges:
    """Tests for commit_changes()."""

    @patch("pr_publisher.Repo")
    def test_commits_when_changes_exist(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.head.commit.hexsha = "abc123def456" + "0" * 28
        mock_repo_cls.return_value = mock_repo

        sha = commit_changes(tmp_path, "fix: sonar issues")

        mock_repo.git.add.assert_called_once_with("--all")
        mock_repo.index.commit.assert_called_once_with("fix: sonar issues")
        assert sha == mock_repo.head.commit.hexsha

    @patch("pr_publisher.Repo")
    def test_raises_when_no_changes(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo_cls.return_value = mock_repo

        with pytest.raises(ValueError, match="No changes detected"):
            commit_changes(tmp_path, "fix: sonar issues")

    @patch("pr_publisher.Repo")
    def test_checks_untracked_files(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.head.commit.hexsha = "a" * 40
        mock_repo_cls.return_value = mock_repo

        commit_changes(tmp_path, "msg")

        mock_repo.is_dirty.assert_called_once_with(untracked_files=True)

    @patch("pr_publisher.Repo")
    def test_prints_ok_with_sha(self, mock_repo_cls, tmp_path, capsys):
        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.head.commit.hexsha = "deadbeef1234" + "0" * 28
        mock_repo_cls.return_value = mock_repo

        commit_changes(tmp_path, "my message")

        out = capsys.readouterr().out
        assert "[OK]" in out
        assert "deadbeef1234" in out
        assert "my message" in out


# ===========================================================================
# push_fix_branch
# ===========================================================================

class TestPushFixBranch:
    """Tests for push_fix_branch()."""

    @patch("pr_publisher.Repo")
    def test_pushes_branch_refspec(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        push_fix_branch(tmp_path, "sonarfixes/20260227_153042")

        mock_origin.push.assert_called_once_with(
            refspec="sonarfixes/20260227_153042:sonarfixes/20260227_153042",
            set_upstream=True,
        )

    @patch("pr_publisher.Repo")
    def test_injects_pat_into_remote_url(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        push_fix_branch(
            tmp_path,
            "sonarfixes/20260227_153042",
            repo_url="https://github.com/org/repo.git",
            pat="mytoken",
        )

        first_set_url_call = mock_origin.set_url.call_args_list[0][0][0]
        assert "mytoken" in first_set_url_call

    @patch("pr_publisher.Repo")
    def test_restores_unauthenticated_url_after_push(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        repo_url = "https://github.com/org/repo.git"
        push_fix_branch(tmp_path, "sonarfixes/20260227_153042", repo_url=repo_url, pat="tok")

        # Second set_url call should restore the original (unauthenticated) URL
        second_set_url_call = mock_origin.set_url.call_args_list[1][0][0]
        assert second_set_url_call == repo_url

    @patch("pr_publisher.Repo")
    def test_push_failure_re_raises_git_command_error(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_origin.push.side_effect = GitCommandError("push", 128)
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        with pytest.raises(GitCommandError):
            push_fix_branch(tmp_path, "sonarfixes/20260227_153042")

    @patch("pr_publisher.Repo")
    def test_push_failure_prints_error(self, mock_repo_cls, tmp_path, capsys):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_origin.push.side_effect = GitCommandError("push", 128)
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        with pytest.raises(GitCommandError):
            push_fix_branch(tmp_path, "sonarfixes/20260227_153042")

        assert "[ERROR]" in capsys.readouterr().err

    @patch("pr_publisher.Repo")
    def test_no_pat_does_not_call_set_url(self, mock_repo_cls, tmp_path):
        mock_repo = MagicMock()
        mock_origin = MagicMock()
        mock_repo.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo

        push_fix_branch(tmp_path, "sonarfixes/20260227_153042")

        mock_origin.set_url.assert_not_called()


# ===========================================================================
# create_github_pr
# ===========================================================================

class TestCreateGithubPr:
    """Tests for create_github_pr()."""

    def test_successful_pr_creation(self, requests_mock):
        requests_mock.post(
            "https://api.github.com/repos/myorg/myrepo/pulls",
            json={"html_url": "https://github.com/myorg/myrepo/pull/42"},
            status_code=201,
        )

        url = create_github_pr(
            owner="myorg",
            repo_name="myrepo",
            head_branch="sonarfixes/20260227_153042",
            base_branch="main",
            title="Fix: sonar issues",
            body="Auto-fixes applied.",
            pat="ghp_token",
        )

        assert url == "https://github.com/myorg/myrepo/pull/42"

    def test_sends_bearer_auth_header(self, requests_mock):
        adapter = requests_mock.post(
            "https://api.github.com/repos/org/repo/pulls",
            json={"html_url": "https://github.com/org/repo/pull/1"},
            status_code=201,
        )

        create_github_pr("org", "repo", "sf/branch", "main", "title", "body", "mytoken")

        assert adapter.last_request.headers["Authorization"] == "Bearer mytoken"

    def test_sends_correct_payload(self, requests_mock):
        adapter = requests_mock.post(
            "https://api.github.com/repos/org/repo/pulls",
            json={"html_url": "https://github.com/org/repo/pull/1"},
            status_code=201,
        )

        create_github_pr("org", "repo", "head-branch", "base-branch", "My Title", "My Body", "tok")

        payload = adapter.last_request.json()
        assert payload["head"] == "head-branch"
        assert payload["base"] == "base-branch"
        assert payload["title"] == "My Title"
        assert payload["body"] == "My Body"

    def test_http_error_is_raised(self, requests_mock):
        requests_mock.post(
            "https://api.github.com/repos/org/repo/pulls",
            status_code=422,
            json={"message": "Validation failed"},
        )

        with pytest.raises(requests.HTTPError):
            create_github_pr("org", "repo", "head", "base", "title", "body", "tok")

    def test_prints_ok_with_pr_url(self, requests_mock, capsys):
        requests_mock.post(
            "https://api.github.com/repos/org/repo/pulls",
            json={"html_url": "https://github.com/org/repo/pull/7"},
            status_code=201,
        )

        create_github_pr("org", "repo", "head", "base", "title", "body", "tok")

        assert "[OK]" in capsys.readouterr().out


# ===========================================================================
# create_azure_pr
# ===========================================================================

class TestCreateAzurePr:
    """Tests for create_azure_pr()."""

    _API = (
        "https://dev.azure.com/myorg/myproject/_apis/git/repositories"
        "/myrepo/pullrequests?api-version=7.0"
    )

    def test_successful_pr_creation(self, requests_mock):
        requests_mock.post(self._API, json={"pullRequestId": 99}, status_code=201)

        url = create_azure_pr(
            org="myorg",
            project="myproject",
            repo_name="myrepo",
            head_branch="sonarfixes/20260227_153042",
            base_branch="main",
            title="Fix",
            body="Body",
            pat="azpat",
        )

        assert "pullrequest/99" in url
        assert "myorg" in url
        assert "myproject" in url

    def test_sends_basic_auth_header(self, requests_mock):
        import base64
        adapter = requests_mock.post(self._API, json={"pullRequestId": 1}, status_code=201)

        create_azure_pr("myorg", "myproject", "myrepo", "head", "main", "t", "b", "myazpat")

        expected = "Basic " + base64.b64encode(b":myazpat").decode()
        assert adapter.last_request.headers["Authorization"] == expected

    def test_sends_correct_ref_names(self, requests_mock):
        adapter = requests_mock.post(self._API, json={"pullRequestId": 1}, status_code=201)

        create_azure_pr("myorg", "myproject", "myrepo", "sonarfixes/ts", "main", "t", "b", "pat")

        payload = adapter.last_request.json()
        assert payload["sourceRefName"] == "refs/heads/sonarfixes/ts"
        assert payload["targetRefName"] == "refs/heads/main"

    def test_http_error_is_raised(self, requests_mock):
        requests_mock.post(self._API, status_code=400, json={"message": "Bad request"})

        with pytest.raises(requests.HTTPError):
            create_azure_pr("myorg", "myproject", "myrepo", "h", "b", "t", "d", "p")

    def test_pr_url_contains_pull_request_id(self, requests_mock):
        requests_mock.post(self._API, json={"pullRequestId": 123}, status_code=201)

        url = create_azure_pr("myorg", "myproject", "myrepo", "h", "b", "t", "d", "p")

        assert "123" in url


# ===========================================================================
# create_pull_request (router)
# ===========================================================================

class TestCreatePullRequest:
    """Tests for create_pull_request() — the platform-routing function."""

    @patch("pr_publisher.create_github_pr", return_value="https://github.com/o/r/pull/1")
    @patch("pr_publisher.parse_github_repo", return_value=("org", "repo"))
    def test_routes_to_github(self, mock_parse, mock_gh):
        url = create_pull_request(
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            title="t",
            body="b",
            pat="tok",
        )
        mock_gh.assert_called_once()
        assert url == "https://github.com/o/r/pull/1"

    @patch("pr_publisher.create_azure_pr", return_value="https://dev.azure.com/pr/1")
    @patch("pr_publisher.parse_azure_repo", return_value=("org", "proj", "repo"))
    def test_routes_to_azure(self, mock_parse, mock_az):
        url = create_pull_request(
            repo_url="https://dev.azure.com/org/proj/_git/repo",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            title="t",
            body="b",
            pat="tok",
        )
        mock_az.assert_called_once()
        assert url == "https://dev.azure.com/pr/1"

    def test_unknown_platform_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported hosting platform"):
            create_pull_request(
                repo_url="https://bitbucket.org/org/repo.git",
                fix_branch="sonarfixes/ts",
                base_branch="main",
                title="t",
                body="b",
                pat="tok",
            )

    @patch("pr_publisher.create_github_pr", return_value="https://github.com/o/r/pull/2")
    @patch("pr_publisher.parse_github_repo", return_value=("o", "r"))
    def test_passes_correct_branches(self, mock_parse, mock_gh):
        create_pull_request(
            repo_url="https://github.com/o/r.git",
            fix_branch="sonarfixes/20260227_153042",
            base_branch="develop",
            title="title",
            body="body",
            pat="pat",
        )
        # create_github_pr is called with positional args: (owner, repo, head, base, title, body, pat)
        args = mock_gh.call_args[0]
        assert args[2] == "sonarfixes/20260227_153042"  # head_branch
        assert args[3] == "develop"                      # base_branch


# ===========================================================================
# publish_and_create_pr (orchestrator)
# ===========================================================================

class TestPublishAndCreatePr:
    """Tests for publish_and_create_pr()."""

    @patch("pr_publisher.create_pull_request", return_value="https://github.com/o/r/pull/5")
    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", return_value="abc123" + "0" * 34)
    def test_full_pipeline_called_in_order(self, mock_commit, mock_push, mock_pr, tmp_path):
        from unittest.mock import call as _call
        manager = MagicMock()
        manager.attach_mock(mock_commit, "commit")
        manager.attach_mock(mock_push, "push")
        manager.attach_mock(mock_pr, "pr")

        pr_url = publish_and_create_pr(
            clone_dir=tmp_path,
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            commit_message="fix: sonar",
            pat="tok",
        )

        assert pr_url == "https://github.com/o/r/pull/5"
        # Verify ordering: commit → push → pr
        method_names = [c[0] for c in manager.mock_calls]
        assert method_names.index("commit") < method_names.index("push")
        assert method_names.index("push") < method_names.index("pr")

    @patch("pr_publisher.create_pull_request")
    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", return_value="abc" + "0" * 37)
    def test_no_pat_skips_pr_creation(self, _commit, _push, mock_pr, tmp_path):
        result = publish_and_create_pr(
            clone_dir=tmp_path,
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            commit_message="fix",
            pat=None,
        )
        mock_pr.assert_not_called()
        assert result == ""

    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", side_effect=ValueError("No changes detected"))
    def test_no_changes_raises_value_error(self, _commit, _push, tmp_path):
        with pytest.raises(ValueError, match="No changes detected"):
            publish_and_create_pr(
                clone_dir=tmp_path,
                repo_url="https://github.com/org/repo.git",
                fix_branch="sonarfixes/ts",
                base_branch="main",
                commit_message="fix",
                pat="tok",
            )

    @patch("pr_publisher.create_pull_request", return_value="https://github.com/o/r/pull/9")
    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", return_value="abc" + "0" * 37)
    def test_default_pr_title_contains_fix_branch(self, _c, _p, mock_pr, tmp_path):
        publish_and_create_pr(
            clone_dir=tmp_path,
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/20260227_153042",
            base_branch="main",
            commit_message="fix",
            pat="tok",
        )
        # create_pull_request is called with all keyword args
        kwargs = mock_pr.call_args.kwargs
        assert "sonarfixes/20260227_153042" in kwargs["title"]

    @patch("pr_publisher.create_pull_request", return_value="https://github.com/o/r/pull/9")
    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", return_value="abc" + "0" * 37)
    def test_custom_pr_title_and_body_are_forwarded(self, _c, _p, mock_pr, tmp_path):
        publish_and_create_pr(
            clone_dir=tmp_path,
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            commit_message="fix",
            pr_title="Custom Title",
            pr_body="Custom Body",
            pat="tok",
        )
        kwargs = mock_pr.call_args.kwargs
        assert kwargs["title"] == "Custom Title"
        assert kwargs["body"] == "Custom Body"

    @patch("pr_publisher.create_pull_request", return_value="https://github.com/o/r/pull/9")
    @patch("pr_publisher.push_fix_branch")
    @patch("pr_publisher.commit_changes", return_value="abc" + "0" * 37)
    def test_pat_forwarded_to_push_and_pr(self, _commit, mock_push, mock_pr, tmp_path):
        publish_and_create_pr(
            clone_dir=tmp_path,
            repo_url="https://github.com/org/repo.git",
            fix_branch="sonarfixes/ts",
            base_branch="main",
            commit_message="fix",
            pat="secret_pat",
        )
        push_kwargs = mock_push.call_args.kwargs
        assert push_kwargs.get("pat") == "secret_pat"

        pr_kwargs = mock_pr.call_args.kwargs
        assert pr_kwargs["pat"] == "secret_pat"


# ===========================================================================
# parse_args
# ===========================================================================

class TestParseArgs:
    """Tests for pr_publisher.parse_args()."""

    _REQUIRED = [
        "--clone-dir", "./workdir/repo",
        "--repo-url", "https://github.com/org/repo.git",
        "--fix-branch", "sonarfixes/20260227_153042",
        "--base-branch", "main",
        "--commit-message", "fix: sonar auto-fixes",
    ]

    def test_required_args_parsed(self):
        with patch("sys.argv", ["pr_publisher.py"] + self._REQUIRED):
            args = parse_args()
        assert args.clone_dir == "./workdir/repo"
        assert args.repo_url == "https://github.com/org/repo.git"
        assert args.fix_branch == "sonarfixes/20260227_153042"
        assert args.base_branch == "main"
        assert args.commit_message == "fix: sonar auto-fixes"

    def test_pat_defaults_to_none(self):
        with patch("sys.argv", ["pr_publisher.py"] + self._REQUIRED):
            args = parse_args()
        assert args.pat is None

    def test_pr_title_defaults_to_none(self):
        with patch("sys.argv", ["pr_publisher.py"] + self._REQUIRED):
            args = parse_args()
        assert args.pr_title is None

    def test_pr_body_defaults_to_none(self):
        with patch("sys.argv", ["pr_publisher.py"] + self._REQUIRED):
            args = parse_args()
        assert args.pr_body is None

    def test_optional_args_parsed(self):
        with patch("sys.argv", ["pr_publisher.py"] + self._REQUIRED + [
            "--pat", "ghp_secret",
            "--pr-title", "My PR",
            "--pr-body", "My description",
        ]):
            args = parse_args()
        assert args.pat == "ghp_secret"
        assert args.pr_title == "My PR"
        assert args.pr_body == "My description"

    def test_missing_clone_dir_exits(self):
        argv = [a for a in self._REQUIRED if a != "--clone-dir" and a != "./workdir/repo"]
        with patch("sys.argv", ["pr_publisher.py"] + argv):
            with pytest.raises(SystemExit):
                parse_args()

    def test_missing_fix_branch_exits(self):
        argv = [a for a in self._REQUIRED if a not in ("--fix-branch", "sonarfixes/20260227_153042")]
        with patch("sys.argv", ["pr_publisher.py"] + argv):
            with pytest.raises(SystemExit):
                parse_args()

    def test_missing_base_branch_exits(self):
        argv = [a for a in self._REQUIRED if a not in ("--base-branch", "main")]
        with patch("sys.argv", ["pr_publisher.py"] + argv):
            with pytest.raises(SystemExit):
                parse_args()

    def test_missing_commit_message_exits(self):
        argv = [a for a in self._REQUIRED if a not in ("--commit-message", "fix: sonar auto-fixes")]
        with patch("sys.argv", ["pr_publisher.py"] + argv):
            with pytest.raises(SystemExit):
                parse_args()
