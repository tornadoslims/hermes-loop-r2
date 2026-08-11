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
    # First call: _resolve_team; second call: issues query
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
    ]
    plugin = LinearPlugin()
    plugin.init({})
    ready = plugin.list_ready()
    assert [i["identifier"] for i in ready] == ["REA-1"]


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
