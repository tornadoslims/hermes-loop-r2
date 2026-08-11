from unittest.mock import patch

import pytest

from loop.plugins.github import GitHubError, GitHubPlugin


def test_init_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    plugin = GitHubPlugin()
    with pytest.raises(GitHubError):
        plugin.init({"repo": "owner/repo"})


def test_init_requires_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    with pytest.raises(GitHubError):
        plugin.init({})


def test_init_reads_config_and_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    assert plugin._token == "env-tok"
    assert plugin._repo == "owner/repo"


def test_init_config_token_overrides_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo", "token": "config-tok"})
    assert plugin._token == "config-tok"


def test_status_unauthenticated_when_token_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    plugin = GitHubPlugin()
    with pytest.raises(GitHubError):
        plugin.init({"repo": "owner/repo"})
    status = plugin.status()
    assert status == {"authenticated": False, "error": "missing GITHUB_TOKEN"}


@patch("loop.plugins.github._request")
def test_start_authenticates_and_status_shape(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    mock_request.return_value = (
        {"login": "tornadoslims"},
        {"X-RateLimit-Remaining": "4995"},
    )
    plugin = GitHubPlugin()
    plugin.init({"repo": "tornadoslims/hermes-loop-r2"})
    plugin.start()
    assert plugin.status() == {
        "name": "github",
        "authenticated": True,
        "username": "tornadoslims",
        "repo": "tornadoslims/hermes-loop-r2",
        "rate_limit_remaining": 4995,
    }
    mock_request.assert_called_once_with("tok", "GET", "/user")


@patch("loop.plugins.github._request")
def test_start_with_invalid_token_stays_unauthenticated(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "bad-tok")
    mock_request.side_effect = GitHubError("HTTP 401: bad credentials")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin.start()  # must not raise
    status = plugin.status()
    assert status["authenticated"] is False
    assert "401" in status["error"]


@patch("loop.plugins.github._request")
def test_method_call_before_start_raises_clear_error(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    # start() never called -- _authenticated is False
    with pytest.raises(GitHubError):
        plugin.list_ready()
    mock_request.assert_not_called()


@patch("loop.plugins.github._request")
def test_list_ready_filters_unassigned_issues_only(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True  # bypass start() network call for this unit test
    plugin._username = "bot"
    mock_request.return_value = (
        [
            {
                "number": 1, "title": "A", "body": "desc", "html_url": "u1",
                "labels": [{"name": "agent-ready"}], "assignee": None,
            },
            {
                "number": 2, "title": "B", "body": "desc", "html_url": "u2",
                "labels": [{"name": "agent-ready"}], "assignee": {"login": "someone"},
            },
            {
                "number": 3, "title": "PR", "body": "", "html_url": "u3",
                "labels": [{"name": "agent-ready"}], "assignee": None,
                "pull_request": {"url": "x"},
            },
        ],
        {},
    )
    ready = plugin.list_ready()
    assert [i["id"] for i in ready] == [1]
    mock_request.assert_called_once()
    args, kwargs = mock_request.call_args
    assert args[1] == "GET"
    assert args[2] == "/repos/owner/repo/issues"


@patch("loop.plugins.github._request")
def test_list_in_review_filters_by_label(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = (
        [
            {
                "number": 10, "title": "PR A", "body": "closes #1",
                "labels": [{"name": "in-review"}],
                "head": {"ref": "branch-a", "sha": "sha1"},
            },
            {
                "number": 11, "title": "PR B", "body": "",
                "labels": [{"name": "other"}],
                "head": {"ref": "branch-b", "sha": "sha2"},
            },
        ],
        {},
    )
    in_review = plugin.list_in_review()
    assert [pr["pr_number"] for pr in in_review] == [10]
    assert in_review[0]["issue_id"] == "1"


@patch("loop.plugins.github._request")
def test_claim_issue_assigns_and_relabels(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = (None, {})
    result = plugin.claim_issue("5")
    assert result is True
    calls = [c.args[1:3] for c in mock_request.call_args_list]
    assert ("PATCH", "/repos/owner/repo/issues/5") in calls
    assert ("POST", "/repos/owner/repo/issues/5/labels") in calls
    assert ("DELETE", "/repos/owner/repo/issues/5/labels/agent-ready") in calls


@patch("loop.plugins.github._request")
def test_create_pr_returns_pr_number_and_url(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = ({"number": 42, "html_url": "https://x/42"}, {})
    result = plugin.create_pr("title", "head-branch", "main", "body")
    assert result == {"pr_number": 42, "url": "https://x/42"}


@patch("loop.plugins.github._request")
def test_merge_pr_returns_false_on_error(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.side_effect = GitHubError("HTTP 405: not mergeable")
    assert plugin.merge_pr("42") is False


@patch("loop.plugins.github._request")
def test_merge_pr_returns_true_on_success(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = ({"merged": True}, {})
    assert plugin.merge_pr("42") is True


@patch("loop.plugins.github._request")
def test_find_pr_returns_none_when_no_matching_pr(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = ([], {})
    assert plugin.find_pr("some-branch") is None
    args, kwargs = mock_request.call_args
    assert args[1] == "GET"
    assert args[2] == "/repos/owner/repo/pulls"
    assert kwargs["params"]["head"] == "owner:some-branch"


@patch("loop.plugins.github._request")
def test_find_pr_returns_matching_pr(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = (
        [{"number": 7, "html_url": "https://x/7", "state": "open",
          "head": {"ref": "some-branch"}}],
        {},
    )
    pr = plugin.find_pr("some-branch")
    assert pr == {"pr_number": 7, "url": "https://x/7", "state": "open", "head_branch": "some-branch"}


@patch("loop.plugins.github._request")
def test_find_pr_defaults_to_all_states(mock_request, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    plugin = GitHubPlugin()
    plugin.init({"repo": "owner/repo"})
    plugin._authenticated = True
    plugin._username = "bot"
    mock_request.return_value = ([], {})
    plugin.find_pr("some-branch")
    args, kwargs = mock_request.call_args
    assert kwargs["params"]["state"] == "all"
