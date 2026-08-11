import pytest

from loop.plugins.base import Plugin


def test_incomplete_subclass_rejected_at_instantiation():
    class MissingStop(Plugin):
        def init(self, config):
            pass

        def start(self):
            pass

        def status(self):
            return {}

    with pytest.raises(TypeError):
        MissingStop()


def test_missing_all_methods_rejected():
    class Empty(Plugin):
        pass

    with pytest.raises(TypeError):
        Empty()


def test_complete_subclass_accepted():
    class Complete(Plugin):
        def init(self, config):
            self.config = config

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def status(self):
            return {"started": getattr(self, "started", False)}

    p = Complete()
    p.init({"a": 1})
    assert p.config == {"a": 1}
    p.start()
    assert p.status() == {"started": True}
    p.stop()
    assert p.status() == {"started": False}


def test_plugin_is_abstract_base_class():
    with pytest.raises(TypeError):
        Plugin()
