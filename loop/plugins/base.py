"""Plugin base interface for hermes-loop-r2.

Every plugin subclasses :class:`Plugin` and implements its four lifecycle
methods. The plugin manager instantiates the subclass and calls
``init(config)`` then ``start()`` in order; ``stop()`` is called on
shutdown and ``status()`` is used for introspection (``loop plugin list``,
health checks, etc.).

Because ``Plugin`` is an ``abc.ABC`` with ``@abstractmethod``-decorated
methods, Python itself refuses to instantiate any subclass that is
missing one of them -- the error happens at the ``Plugin(...)`` call site
with a message naming the class and the missing method(s), e.g.:

    TypeError: Can't instantiate abstract class Broken without an
    implementation for abstract method 'stop'

The plugin manager catches that TypeError and re-raises a
PluginInterfaceError that also names *which plugin file* it came from,
so a bad plugin fails loudly and specifically at daemon startup instead
of silently doing nothing at call time.
"""
from __future__ import annotations

import abc
from typing import Any, Dict


class Plugin(abc.ABC):
    """Abstract base class every hermes-loop-r2 plugin must subclass."""

    @abc.abstractmethod
    def init(self, config: Dict[str, Any]) -> None:
        """Configure the plugin. Called once, before start(). Must not
        perform network I/O or spawn background work -- just validate
        and store config."""
        raise NotImplementedError

    @abc.abstractmethod
    def start(self) -> None:
        """Start the plugin's runtime behavior (connections, watchers,
        background threads, etc.). Called once, after init()."""
        raise NotImplementedError

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop the plugin's runtime behavior and release any resources
        acquired in start(). Must be safe to call even if start() was
        never called or already failed."""
        raise NotImplementedError

    @abc.abstractmethod
    def status(self) -> Dict[str, Any]:
        """Return a JSON-serializable dict describing the plugin's
        current state, e.g. {"name": "linear", "status": "loaded"}."""
        raise NotImplementedError

    def validate(self) -> bool:
        """Optional self-check. Plugins may override this to verify
        connectivity, credentials, or other runtime prerequisites.
        Called by `loop plugin validate <name>`; the CLI exits non-zero
        when this returns False."""
        return True
