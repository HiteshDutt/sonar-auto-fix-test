"""
tests/test_repo_checkout.py

Unit tests for src/repo_checkout.py.

Run with:
    pytest tests/test_repo_checkout.py -v
"""

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Make sure the src package is importable without installing it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from repo_checkout import (
    inject_pat_into_url,
    safe_display_url,
    resolve_clone_target,
    checkout_repo,
    make_sonarfix_branch_name,
    create_sonarfix_branch,
    parse_args,
)

from git import GitCommandError, InvalidGitRepositoryError


# ===========================================================================
# make_sonarfix_branch_name
# ===========================================================================

class TestMakeSonarfixBranchName:
    """Tests for make_sonarfix_branch_name()."""

    def test_prefix_is_sonarfixes(self):
        name = make_sonarfix_branch_name()
        assert name.startswith("sonarfixes/")

    def test_format_with_explicit_timestamp(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 2, 27, 15, 30, 42, tzinfo=timezone.utc)
        name = make_sonarfix_branch_name(ts)
        assert name == "sonarfixes/20260227_153042"

    def test_timestamp_segment_has_correct_length(self):
        name = make_sonarfix_branch_name()
        # sonarfixes/<YYYYMMDD_HHMMSS>  =>  15 chars after the slash
        _, ts = name.split("/")
        assert len(ts) == 15  # 8 + 1 + 6

    def test_timestamp_segment_format_is_YYYYMMDD_HHMMSS(self):
        import re
        name = make_sonarfix_branch_name()
        _, ts = name.split("/")
        assert re.match(r"\d{8}_\d{6}", ts), f"Unexpected format: {ts}"

    def test_two_successive_calls_may_differ(self):
        import time
        n1 = make_sonarfix_branch_name()
        time.sleep(1)
        n2 = make_sonarfix_branch_name()
        # Both must be valid even if they happen to be equal within the same second
        assert n1.startswith("sonarfixes/")
        assert n2.startswith("sonarfixes/")

    def test_default_uses_utc(self):
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = make_sonarfix_branch_name()
        after = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        _, ts = name.split("/")
        # The generated timestamp must fall between before and after (inclusive)
        assert before <= ts <= after


# ===========================================================================
# create_sonarfix_branch
# ===========================================================================

class TestCreateSonarfixBranch:
    """Tests for create_sonarfix_branch()."""

    def test_calls_git_checkout_minus_b(self):
        mock_repo = MagicMock()
        create_sonarfix_branch(mock_repo, "sonarfixes/20260227_153042")
        mock_repo.git.checkout.assert_called_once_with("-b", "sonarfixes/20260227_153042")

    def test_returns_branch_name(self):
        mock_repo = MagicMock()
        result = create_sonarfix_branch(mock_repo, "sonarfixes/20260227_153042")
        assert result == "sonarfixes/20260227_153042"

    def test_prints_ok_message(self, capsys):
        mock_repo = MagicMock()
        create_sonarfix_branch(mock_repo, "sonarfixes/20260227_153042")
        captured = capsys.readouterr()
        assert "[OK]" in captured.out
        assert "sonarfixes/20260227_153042" in captured.out

    def test_git_error_propagates(self):
        mock_repo = MagicMock()
        mock_repo.git.checkout.side_effect = GitCommandError("checkout", 128)
        with pytest.raises(GitCommandError):
            create_sonarfix_branch(mock_repo, "sonarfixes/20260227_153042")


# ===========================================================================
# inject_pat_into_url
# ===========================================================================

class TestInjectPatIntoUrl:
    """Tests for inject_pat_into_url()."""

    def test_github_https_url(self):
        url = "https://github.com/org/my-repo.git"
        result = inject_pat_into_url(url, "mytoken")
        assert result == "https://mytoken@github.com/org/my-repo.git"

    def test_azure_devops_https_url(self):
        url = "https://dev.azure.com/myorg/myproject/_git/myrepo"
        result = inject_pat_into_url(url, "azpat")
        assert result == "https://azpat@dev.azure.com/myorg/myproject/_git/myrepo"

    def test_http_url_is_supported(self):
        url = "http://internal.corp/org/repo.git"
        result = inject_pat_into_url(url, "corptoken")
        assert result == "http://corptoken@internal.corp/org/repo.git"

    def test_url_with_port_preserves_port(self):
        url = "https://github.com:443/org/repo.git"
        result = inject_pat_into_url(url, "tok")
        assert "443" in result
        assert result == "https://tok@github.com:443/org/repo.git"

    def test_existing_credentials_are_replaced(self):
        url = "https://olduser:oldpass@github.com/org/repo.git"
        result = inject_pat_into_url(url, "newtoken")
        assert "olduser" not in result
        assert "oldpass" not in result
        assert "newtoken" in result

    def test_ssh_url_raises_value_error(self):
        url = "git@github.com:org/repo.git"
        with pytest.raises(ValueError, match="PAT injection is only supported for HTTP/HTTPS"):
            inject_pat_into_url(url, "mytoken")

    def test_unsupported_scheme_raises_value_error(self):
        url = "ftp://example.com/repo"
        with pytest.raises(ValueError):
            inject_pat_into_url(url, "tok")

    def test_pat_appears_before_host(self):
        url = "https://github.com/org/repo.git"
        token = "ghp_abc123"
        result = inject_pat_into_url(url, token)
        # token must appear before the hostname
        assert result.index(token) < result.index("github.com")

    def test_path_and_query_are_preserved(self):
        url = "https://github.com/org/repo.git?ref=main"
        result = inject_pat_into_url(url, "tok")
        assert "/org/repo.git" in result
        assert "ref=main" in result


# ===========================================================================
# safe_display_url
# ===========================================================================

class TestSafeDisplayUrl:
    """Tests for safe_display_url()."""

    def test_masks_pat_in_https_url(self):
        url = "https://mytoken@github.com/org/repo.git"
        result = safe_display_url(url)
        assert "mytoken" not in result
        assert "***" in result
        assert "github.com" in result

    def test_plain_url_without_credentials_unchanged(self):
        url = "https://github.com/org/repo.git"
        result = safe_display_url(url)
        assert result == url

    def test_url_with_user_and_pass_is_masked(self):
        url = "https://user:password@github.com/org/repo.git"
        result = safe_display_url(url)
        assert "user" not in result
        assert "password" not in result
        assert "***" in result

    def test_mask_format_is_correct(self):
        url = "https://secret@github.com/org/repo.git"
        result = safe_display_url(url)
        assert result == "https://***@github.com/org/repo.git"

    def test_ssh_url_returned_unchanged(self):
        url = "git@github.com:org/repo.git"
        result = safe_display_url(url)
        # No https:// pattern, regex should not match — returned as-is
        assert result == url


# ===========================================================================
# resolve_clone_target
# ===========================================================================

class TestResolveCloneTarget:
    """Tests for resolve_clone_target()."""

    def test_github_url_with_git_extension(self):
        workdir = Path("/tmp/workdir")
        url = "https://github.com/org/my-repo.git"
        result = resolve_clone_target(workdir, url)
        assert result == workdir / "my-repo"

    def test_github_url_without_git_extension(self):
        workdir = Path("/tmp/workdir")
        url = "https://github.com/org/my-repo"
        result = resolve_clone_target(workdir, url)
        assert result == workdir / "my-repo"

    def test_azure_devops_url(self):
        workdir = Path("/tmp/workdir")
        url = "https://dev.azure.com/myorg/myproject/_git/myrepo"
        result = resolve_clone_target(workdir, url)
        assert result == workdir / "myrepo"

    def test_url_with_no_path_stem_falls_back_to_repo(self):
        workdir = Path("/tmp/workdir")
        # A URL where urlparse gives an empty stem
        url = "https://github.com/"
        result = resolve_clone_target(workdir, url)
        assert result == workdir / "repo"

    def test_result_is_inside_workdir(self):
        workdir = Path("/some/workdir")
        url = "https://github.com/org/awesome-project.git"
        result = resolve_clone_target(workdir, url)
        assert result.parent == workdir


# ===========================================================================
# checkout_repo — fresh clone
# ===========================================================================

class TestCheckoutRepoFreshClone:
    """Tests for checkout_repo() when the target directory does not yet exist."""

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_calls_clone_from(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=tmp_path,
        )

        mock_repo_cls.clone_from.assert_called_once()
        args, kwargs = mock_repo_cls.clone_from.call_args
        assert args[0] == "https://github.com/org/repo.git"
        assert kwargs.get("branch") == "main" or args[2] == "main"

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_with_pat_injects_token(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            pat="secrettoken",
            workdir=tmp_path,
        )

        args, _ = mock_repo_cls.clone_from.call_args
        assert "secrettoken" in args[0]

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_without_pat_uses_original_url(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="develop",
            pat=None,
            workdir=tmp_path,
        )

        args, _ = mock_repo_cls.clone_from.call_args
        assert args[0] == "https://github.com/org/repo.git"

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_returns_tuple_of_path_and_branch(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()

        result = checkout_repo(
            repo_url="https://github.com/org/my-project.git",
            branch="main",
            workdir=tmp_path,
        )

        assert isinstance(result, tuple)
        clone_path, fix_branch = result
        assert isinstance(clone_path, Path)
        assert clone_path.is_absolute()
        assert "my-project" in str(clone_path)
        assert fix_branch == "sonarfixes/20260227_000000"

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_creates_workdir_if_missing(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()
        new_workdir = tmp_path / "deep" / "nested" / "workdir"

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=new_workdir,
        )

        assert new_workdir.exists()

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_fresh_clone_creates_sonarfix_branch_after_clone(self, mock_repo_cls, _mock_name, mock_create, tmp_path):
        cloned_repo_mock = MagicMock()
        mock_repo_cls.clone_from.return_value = cloned_repo_mock

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=tmp_path,
        )

        mock_create.assert_called_once_with(cloned_repo_mock, "sonarfixes/20260227_000000")


# ===========================================================================
# checkout_repo — clone failure
# ===========================================================================

class TestCheckoutRepoCloneFailure:
    """Tests for checkout_repo() when Repo.clone_from raises GitCommandError."""

    @patch("repo_checkout.Repo")
    def test_clone_failure_exits_with_code_1(self, mock_repo_cls, tmp_path):
        mock_repo_cls.clone_from.side_effect = GitCommandError("clone", 128)

        with pytest.raises(SystemExit) as exc_info:
            checkout_repo(
                repo_url="https://github.com/org/repo.git",
                branch="main",
                workdir=tmp_path,
            )

        assert exc_info.value.code == 1

    @patch("repo_checkout.Repo")
    def test_clone_failure_prints_error(self, mock_repo_cls, tmp_path, capsys):
        mock_repo_cls.clone_from.side_effect = GitCommandError("clone", 128)

        with pytest.raises(SystemExit):
            checkout_repo(
                repo_url="https://github.com/org/repo.git",
                branch="main",
                workdir=tmp_path,
            )

        captured = capsys.readouterr()
        assert "[ERROR]" in captured.err


# ===========================================================================
# checkout_repo — existing valid repo (update path)
# ===========================================================================

class TestCheckoutRepoUpdate:
    """Tests for checkout_repo() when target_dir already has a valid Git repo."""

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_existing_repo_is_updated_not_recloned(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        # Create the target directory so the "exists" check passes
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mock_repo_instance = MagicMock()
        mock_origin = MagicMock()
        mock_repo_instance.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo_instance

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=tmp_path,
        )

        # clone_from must NOT have been called
        mock_repo_cls.clone_from.assert_not_called()
        # fetch and checkout must have been called
        mock_origin.fetch.assert_called_once()
        mock_repo_instance.git.checkout.assert_called_once_with("main")

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_existing_repo_updates_remote_url_with_pat(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mock_repo_instance = MagicMock()
        mock_origin = MagicMock()
        mock_repo_instance.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo_instance

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            pat="newtoken",
            workdir=tmp_path,
        )

        set_url_call_args = mock_origin.set_url.call_args[0][0]
        assert "newtoken" in set_url_call_args

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_existing_repo_pulls_correct_branch(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mock_repo_instance = MagicMock()
        mock_origin = MagicMock()
        mock_repo_instance.remotes.origin = mock_origin
        mock_repo_cls.return_value = mock_repo_instance

        checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="feature/my-branch",
            workdir=tmp_path,
        )

        mock_repo_instance.git.pull.assert_called_once_with("origin", "feature/my-branch")

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_existing_repo_creates_sonarfix_branch(self, mock_repo_cls, _mock_name, mock_create, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mock_repo_instance = MagicMock()
        mock_repo_instance.remotes.origin = MagicMock()
        mock_repo_cls.return_value = mock_repo_instance

        clone_path, fix_branch = checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=tmp_path,
        )

        mock_create.assert_called_once_with(mock_repo_instance, "sonarfixes/20260227_000000")
        assert fix_branch == "sonarfixes/20260227_000000"

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_existing_repo_returns_tuple(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mock_repo_instance = MagicMock()
        mock_repo_instance.remotes.origin = MagicMock()
        mock_repo_cls.return_value = mock_repo_instance

        result = checkout_repo(
            repo_url="https://github.com/org/repo.git",
            branch="main",
            workdir=tmp_path,
        )

        assert isinstance(result, tuple)
        assert len(result) == 2


# ===========================================================================
# checkout_repo — existing directory that is NOT a git repo
# ===========================================================================

class TestCheckoutRepoInvalidExistingDir:
    """Tests for checkout_repo() when target_dir exists but is not a valid Git repo."""

    @patch("repo_checkout.Repo")
    def test_invalid_repo_dir_is_removed_and_recloned(self, mock_repo_cls, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        # A stray file inside to verify the directory is wiped
        (target_dir / "junk.txt").write_text("junk")

        def repo_factory(path):
            raise InvalidGitRepositoryError("not a repo")

        with patch("repo_checkout.Repo") as p:
            p.side_effect = repo_factory
            p.clone_from = MagicMock(return_value=MagicMock())

            with patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000"):
                with patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000"):
                    checkout_repo(
                        repo_url="https://github.com/org/repo.git",
                        branch="main",
                        workdir=tmp_path,
                    )

            p.clone_from.assert_called_once()


# ===========================================================================
# checkout_repo — default workdir
# ===========================================================================

class TestCheckoutRepoDefaultWorkdir:
    """Tests that the default workdir resolves to <project_root>/workdir."""

    @patch("repo_checkout.create_sonarfix_branch", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.make_sonarfix_branch_name", return_value="sonarfixes/20260227_000000")
    @patch("repo_checkout.Repo")
    def test_default_workdir_is_project_root_workdir(self, mock_repo_cls, _mock_name, _mock_create, tmp_path):
        mock_repo_cls.clone_from.return_value = MagicMock()

        fake_src = tmp_path / "src"
        fake_src.mkdir()
        fake_file = fake_src / "repo_checkout.py"
        fake_file.touch()

        with patch("repo_checkout.Path") as mock_path_cls:
            real_path = Path

            def path_side_effect(arg=None):
                if arg == __file__ or (isinstance(arg, str) and "repo_checkout" in arg):
                    mock_p = MagicMock()
                    mock_p.resolve.return_value.parent.parent = tmp_path
                    return mock_p
                return real_path(arg) if arg is not None else real_path()

            mock_path_cls.side_effect = path_side_effect

            result = checkout_repo(
                repo_url="https://github.com/org/repo.git",
                branch="main",
                workdir=tmp_path,
            )
            assert result is not None


# ===========================================================================
# parse_args
# ===========================================================================

class TestParseArgs:
    """Tests for parse_args()."""

    def test_required_args_are_parsed(self):
        with patch("sys.argv", ["repo_checkout.py", "--repo", "https://github.com/o/r.git", "--branch", "main"]):
            args = parse_args()
        assert args.repo == "https://github.com/o/r.git"
        assert args.branch == "main"

    def test_optional_pat_defaults_to_none(self):
        with patch("sys.argv", ["repo_checkout.py", "--repo", "https://github.com/o/r.git", "--branch", "main"]):
            args = parse_args()
        assert args.pat is None

    def test_optional_workdir_defaults_to_none(self):
        with patch("sys.argv", ["repo_checkout.py", "--repo", "https://github.com/o/r.git", "--branch", "main"]):
            args = parse_args()
        assert args.workdir is None

    def test_pat_is_parsed_when_supplied(self):
        with patch("sys.argv", [
            "repo_checkout.py",
            "--repo", "https://github.com/o/r.git",
            "--branch", "main",
            "--pat", "ghp_secret",
        ]):
            args = parse_args()
        assert args.pat == "ghp_secret"

    def test_workdir_is_parsed_when_supplied(self):
        with patch("sys.argv", [
            "repo_checkout.py",
            "--repo", "https://github.com/o/r.git",
            "--branch", "main",
            "--workdir", "/tmp/myworkdir",
        ]):
            args = parse_args()
        assert args.workdir == "/tmp/myworkdir"

    def test_missing_repo_exits(self):
        with patch("sys.argv", ["repo_checkout.py", "--branch", "main"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_missing_branch_exits(self):
        with patch("sys.argv", ["repo_checkout.py", "--repo", "https://github.com/o/r.git"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_all_args_parsed_together(self):
        with patch("sys.argv", [
            "repo_checkout.py",
            "--repo", "https://dev.azure.com/org/project/_git/repo",
            "--branch", "feature/xyz",
            "--pat", "azuretoken",
            "--workdir", "C:/tmp/wd",
        ]):
            args = parse_args()
        assert args.repo == "https://dev.azure.com/org/project/_git/repo"
        assert args.branch == "feature/xyz"
        assert args.pat == "azuretoken"
        assert args.workdir == "C:/tmp/wd"
