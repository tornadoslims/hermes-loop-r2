"""Runtime plugin: thin re-export of the real LinearPlugin implementation.

The plugin manager (loop.plugin_manager) dynamically loads every *.py file
in this directory (path configured via loop.toml's [plugins].dir) and
looks for a Plugin subclass in it -- that's what makes `linear` show up
in `loop plugin list` / `loop plugin validate` per AC-3/AC-4.

The actual implementation lives in loop.plugins.linear (an importable
package module) so it can also be imported directly, e.g.:

    from loop.plugins.linear import LinearPlugin

which is how AC-4's "port the core Linear operations" is verified
independent of the dynamic plugin-loading path.
"""
from loop.plugins.linear import LinearPlugin  # noqa: F401
