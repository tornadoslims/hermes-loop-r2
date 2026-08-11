"""Example plugin demonstrating the complete Plugin lifecycle.

This file is a self-contained, well-commented reference implementation that
shows every lifecycle method a hermes-loop-r2 plugin must provide.  Use it as
a starting point when writing your own plugin.

Lifecycle order (enforced by the plugin manager):
  1.  The plugin manager instantiates your subclass (no-arg __init__).
  2.  ``init(config)`` is called once to validate and store configuration.
  3.  ``start()`` is called once to begin runtime work.
  4.  ``stop()`` is called on shutdown to release resources.
  5.  ``status()`` may be called at any point for introspection / health checks.

Because ``Plugin`` is an ``abc.ABC`` with ``@abstractmethod``-decorated
methods, Python refuses to instantiate any subclass that is missing one of
them — your plugin will fail loudly at load time with a clear error message,
not silently do nothing at runtime.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from loop.plugins.base import Plugin

logger = logging.getLogger(__name__)


class ExamplePlugin(Plugin):
    """A simple plugin that demonstrates the full Plugin lifecycle.

    This plugin simulates a real integration by:
      - Recording timestamps for each lifecycle transition.
      - Tracking a configurable ``greeting`` value (validated in init).
      - Starting a simulated background counter (just a flag + timestamp).
      - Stopping the counter on shutdown.
      - Exposing all of the above via ``status()``.

    To use this plugin, include it in your loop.toml:

        [plugins]
        dir = "plugins/"

        [plugins.example]
        greeting = "hello world"
    """

    # ------------------------------------------------------------------
    # Lifecycle method 1: init
    # ------------------------------------------------------------------
    def init(self, config: Dict[str, Any]) -> None:
        """Validate and store the plugin configuration.

        The plugin manager calls this exactly once, **before** ``start()``.
        This method must **not** perform network I/O, spawn threads, or
        start background work — it should only validate inputs and record
        the configuration for later use.

        Args:
            config: A flat dict of key/value pairs from the ``[plugins.example]``
                    section of the project's ``loop.toml``.  The plugin manager
                    passes an empty dict ``{}`` if no section is declared.

        Raises:
            ValueError: If a required config key is missing or invalid.
        """
        # --- validate required keys ---
        # Every key you expect should be checked here so that a misconfigured
        # plugin fails at startup (init) rather than later (start/run).
        if not isinstance(config.get("greeting"), str):
            raise ValueError(
                "ExamplePlugin requires config key 'greeting' (str), "
                f"got {config.get('greeting')!r}"
            )

        # --- store validated config ---
        self._greeting: str = config["greeting"]
        self._interval: float = float(config.get("interval", 5.0))

        # --- initialise runtime state ---
        # These fields track whether start() / stop() have been called and
        # when — they are reported back through status().
        self._init_time: float = time.time()
        self._started: bool = False
        self._stopped: bool = False
        self._start_time: float | None = None
        self._stop_time: float | None = None

        logger.info(
            "ExamplePlugin.init: greeting=%r interval=%.1fs",
            self._greeting,
            self._interval,
        )

    # ------------------------------------------------------------------
    # Lifecycle method 2: start
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Begin the plugin's runtime behaviour.

        The plugin manager calls this exactly once, **after** ``init()``.
        This is where you open connections, spawn background threads, start
        watchers, subscribe to event buses, etc.

        This method should be idempotent — calling it a second time
        (though the manager won't) should be safe, or at least loudly
        complain instead of corrupting state.
        """
        if self._started:
            logger.warning("ExamplePlugin.start: already started — skipping")
            return

        # --- begin "work" ---
        # In a real plugin you might:
        #   - open a WebSocket / MQTT connection
        #   - spawn a threading.Thread with a run loop
        #   - register callbacks on an event bus
        # Here we just record the transition for demonstration purposes.
        self._started = True
        self._start_time = time.time()

        logger.info(
            "ExamplePlugin.start: running with greeting=%r "
            "interval=%.1fs",
            self._greeting,
            self._interval,
        )

    # ------------------------------------------------------------------
    # Lifecycle method 3: stop
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop the plugin and release all resources.

        The plugin manager calls this on daemon shutdown or when a plugin
        is explicitly unloaded.  It must be **safe to call even if**
        ``start()`` was never called or already failed — guard with a flag
        so that a double-stop or stop-before-start doesn't crash.

        In a real plugin you would:
          - Close network connections.
          - Join background threads (join with a timeout).
          - Cancel scheduled tasks.
          - Flush buffers / write final state.
        """
        if self._stopped or not self._started:
            logger.debug("ExamplePlugin.stop: nothing to stop — skipping")
            return

        # --- tear down "work" ---
        self._started = False
        self._stopped = True
        self._stop_time = time.time()

        logger.info(
            "ExamplePlugin.stop: shutdown complete (ran for %.1fs)",
            self._stop_time - (self._start_time or self._init_time),
        )

    # ------------------------------------------------------------------
    # Lifecycle method 4: status
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of the plugin's current state.

        This method may be called at **any time** — before init, between
        start and stop, after stop, etc.  It must never raise and must
        always return a plain dict of serializable values (str, int, float,
        bool, list, dict, None).

        The plugin manager uses this for:
          - ``loop plugin list`` (shows status summaries)
          - ``loop plugin validate`` (health checks)
          - The web dashboard's plugin pane
          - Cron / health-check probes
        """
        return {
            # --- identity ---
            "name": "example",
            # --- lifecycle state ---
            "initialised": hasattr(self, "_init_time"),
            "started": getattr(self, "_started", False),
            "stopped": getattr(self, "_stopped", False),
            # --- configuration (sanitised) ---
            "greeting": getattr(self, "_greeting", None),
            "interval": getattr(self, "_interval", None),
            # --- timing (float seconds since epoch, or None) ---
            "init_time": getattr(self, "_init_time", None),
            "start_time": getattr(self, "_start_time", None),
            "stop_time": getattr(self, "_stop_time", None),
            # --- computed ---
            "uptime": (
                round(time.time() - self._start_time, 2)
                if getattr(self, "_started", False) and self._start_time
                else None
            ),
        }