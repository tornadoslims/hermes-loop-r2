from unittest.mock import patch

import pytest

from loop.plugins.linear import LinearError, LinearPlugin


def test_init_requires_api_key(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    plugin = LinearPlugin()
    with pytest.raises(LinearError):
        plugin.init({})


def test_init_reads_config_and_env(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "env-key")
    plugin = LinearPlugin()
    plugin.init({"team_key": "REA"})
    assert plugin._api_key == "env-key"
    assert plugin._team_key == "REA"
    plugin.start()
    assert plugin.status() == {"started": True, "team_key": "REA", "has_api_key": True}
    plugin.stop()
    assert plugin.status()["started"] is False


def test_init_config_api_key_overrides_env(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "env-key")
    plugin = LinearPlugin()
    plugin.init({"api_key": "config-key"})
    assert plugin._api_key == "config-key"


@patch("loop.plugins.linear._gql")
def test_whoami(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.return_value = {"viewer": {"id": "1", "name": "Bot", "email": "bot@example.com"}}
    plugin = LinearPlugin()
    plugin.init({})
    result = plugin.whoami()
    assert result["name"] == "Bot"
    mock_gql.assert_called_once()


@patch("loop.plugins.linear._gql")
def test_list_ready_filters_labels(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    # Calls in order: _resolve_team, issues query, then for the one
    # surviving candidate (REA-1) the dependency check's
    # get_comments() -> _resolve_issue() + comments query.
    mock_gql.side_effect = [
        {"teams": {"nodes": [{"id": "t1", "key": "REA", "name": "Reach"}]}},
        {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "REA-1", "title": "A", "url": "u",
                     "state": {"name": "Todo"}, "labels": {"nodes": [{"name": "agent-ready"}]}},
                    {"id": "2", "identifier": "REA-2", "title": "B", "url": "u",
                     "state": {"name": "Todo"}, "labels": {"nodes": [{"name": "agent-ready"}, {"name": "blocked"}]}},
                    {"id": "3", "identifier": "REA-3", "title": "C", "url": "u",
                     "state": {"name": "Todo"}, "labels": {"nodes": []}},
                ]
            }
        },
        {"issue": {"id": "1", "identifier": "REA-1"}},
        {"issue": {"comments": {"nodes": []}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    ready = plugin.list_ready()
    assert [i["identifier"] for i in ready] == ["REA-1"]


@patch("loop.plugins.linear._gql")
def test_list_in_review_filters_by_label(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"teams": {"nodes": [{"id": "t1", "key": "REA", "name": "Reach"}]}},
        {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "REA-1", "title": "A", "url": "u",
                     "state": {"name": "In Review"}, "labels": {"nodes": [{"name": "stage-in-review"}]}},
                    {"id": "2", "identifier": "REA-2", "title": "B", "url": "u",
                     "state": {"name": "Todo"}, "labels": {"nodes": [{"name": "agent-ready"}]}},
                ]
            }
        },
    ]
    plugin = LinearPlugin()
    plugin.init({})
    in_review = plugin.list_in_review()
    assert [i["identifier"] for i in in_review] == ["REA-1"]


@patch("loop.plugins.linear._gql")
def test_get_issue(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.return_value = {"issue": {"id": "1", "identifier": "REA-1", "title": "T"}}
    plugin = LinearPlugin()
    plugin.init({})
    issue = plugin.get_issue("REA-1")
    assert issue["identifier"] == "REA-1"


@patch("loop.plugins.linear._gql")
def test_add_comment(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"issue": {"id": "1", "identifier": "REA-1"}},
        {"commentCreate": {"success": True, "comment": {"id": "c1"}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    result = plugin.add_comment("REA-1", "hello")
    assert result["success"] is True


# ------------------------------------------------------------- REA-90 AC-6

def test_parse_dependencies_basic(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    plugin = LinearPlugin()
    plugin.init({})
    deps = plugin.parse_dependencies(
        "Some text. Depends on REA-12 and more text.",
        ["unrelated comment", "depends on rea-13 too"],
    )
    assert deps == ["REA-12", "REA-13"]


def test_parse_dependencies_dedupes_and_ignores_no_match(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    plugin = LinearPlugin()
    plugin.init({})
    deps = plugin.parse_dependencies("Depends on REA-1. Depends on REA-1 again.", [])
    assert deps == ["REA-1"]
    assert plugin.parse_dependencies("no dependency text here", []) == []


# ------------------------------------------------------------- REA-90 AC-1

@patch("loop.plugins.linear._gql")
def test_list_ready_skips_issue_with_unmet_dependency(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"teams": {"nodes": [{"id": "t1", "key": "REA", "name": "Reach"}]}},
        {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "REA-1", "title": "A", "url": "u",
                     "state": {"name": "Todo"}, "priority": 0, "createdAt": "2024-01-01",
                     "description": "Depends on REA-2",
                     "labels": {"nodes": [{"name": "agent-ready"}]}},
                ]
            }
        },
        # get_comments -> _resolve_issue(REA-1)
        {"issue": {"id": "1", "identifier": "REA-1"}},
        {"issue": {"comments": {"nodes": []}}},
        # _resolve_issue(REA-2) for the dependency check -- still open
        {"issue": {"id": "2", "identifier": "REA-2", "state": {"type": "started"}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    logged = []
    ready = plugin.list_ready(log=logged.append)
    assert ready == []
    assert logged == ["[queue] skipping REA-1 -- waiting on REA-2"]


@patch("loop.plugins.linear._gql")
def test_list_ready_includes_issue_with_met_dependency(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"teams": {"nodes": [{"id": "t1", "key": "REA", "name": "Reach"}]}},
        {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "REA-1", "title": "A", "url": "u",
                     "state": {"name": "Todo"}, "priority": 0, "createdAt": "2024-01-01",
                     "description": "Depends on REA-2",
                     "labels": {"nodes": [{"name": "agent-ready"}]}},
                ]
            }
        },
        {"issue": {"id": "1", "identifier": "REA-1"}},
        {"issue": {"comments": {"nodes": []}}},
        {"issue": {"id": "2", "identifier": "REA-2", "state": {"type": "completed"}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    ready = plugin.list_ready()
    assert [i["identifier"] for i in ready] == ["REA-1"]


# ------------------------------------------------------------- REA-90 AC-3

@patch("loop.plugins.linear._gql")
def test_list_ready_sorts_by_priority_then_created_at(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"teams": {"nodes": [{"id": "t1", "key": "REA", "name": "Reach"}]}},
        {
            "issues": {
                "nodes": [
                    {"id": "1", "identifier": "REA-LOW", "title": "low prio", "url": "u",
                     "state": {"name": "Todo"}, "priority": 3, "createdAt": "2024-01-01",
                     "description": "", "labels": {"nodes": [{"name": "agent-ready"}]}},
                    {"id": "2", "identifier": "REA-HIGH", "title": "high prio", "url": "u",
                     "state": {"name": "Todo"}, "priority": 1, "createdAt": "2024-01-05",
                     "description": "", "labels": {"nodes": [{"name": "agent-ready"}]}},
                    {"id": "3", "identifier": "REA-LABEL", "title": "label prio", "url": "u",
                     "state": {"name": "Todo"}, "priority": 0, "createdAt": "2024-01-02",
                     "description": "", "labels": {"nodes": [{"name": "agent-ready"}, {"name": "priority:2"}]}},
                ]
            }
        },
        # No dependency text on any issue -> parse_dependencies short-circuits
        # before ever calling get_comments/_resolve_issue for the dependency
        # walk (see _unmet_dependencies: get_comments IS still called).
        {"issue": {"id": "1", "identifier": "REA-LOW"}},
        {"issue": {"comments": {"nodes": []}}},
        {"issue": {"id": "2", "identifier": "REA-HIGH"}},
        {"issue": {"comments": {"nodes": []}}},
        {"issue": {"id": "3", "identifier": "REA-LABEL"}},
        {"issue": {"comments": {"nodes": []}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    ready = plugin.list_ready()
    assert [i["identifier"] for i in ready] == ["REA-HIGH", "REA-LABEL", "REA-LOW"]


# ------------------------------------------------------------- REA-90 AC-2

@patch("loop.plugins.linear._gql")
def test_remove_label_drops_only_named_label(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"issue": {"id": "1", "identifier": "REA-1",
                    "labels": {"nodes": [{"id": "l1", "name": "blocked"}, {"id": "l2", "name": "bug"}]}}},
        {"issueUpdate": {"success": True, "issue": {"id": "1", "identifier": "REA-1",
                                                      "labels": {"nodes": [{"name": "bug"}]}}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    result = plugin.remove_label("REA-1", "blocked")
    assert result["labels"]["nodes"] == [{"name": "bug"}]
    call_args = mock_gql.call_args_list[1][0]
    assert call_args[2]["input"]["labelIds"] == ["l2"]


@patch("loop.plugins.linear._gql")
def test_dependencies_met_true_when_all_completed(mock_gql, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    mock_gql.side_effect = [
        {"issue": {"id": "1", "identifier": "REA-A", "description": "Depends on REA-B"}},
        {"issue": {"id": "1", "identifier": "REA-A"}},
        {"issue": {"comments": {"nodes": []}}},
        {"issue": {"id": "2", "identifier": "REA-B", "state": {"type": "canceled"}}},
    ]
    plugin = LinearPlugin()
    plugin.init({})
    assert plugin.dependencies_met("REA-A") is True
