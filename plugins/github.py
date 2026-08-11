"""Runtime plugin: thin re-export of the real GitHubPlugin implementation.

The plugin manager (loop.plugin_manager) dynamically loads every *.py file
in this directory (path configured via loop.toml's [plugins].dir) and
looks for a Plugin subclass in it -- that's what makes `github` show up
in `loop plugin list` / `loop plugin validate` alongside `linear`.

The actual implementation lives in loop.plugins.github (an importable
package module) so it can also be imported directly, e.g.:

    from loop.plugins.github import GitHubPlugin

which is how the plugin's method contract is verified independent of the
dynamic plugin-loading path (mirrors plugins/linear.py's shape).
"""
from loop.plugins.github import GitHubPlugin  # noqa: F401
