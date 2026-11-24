"""Agent loops: database connection handling of the stuck-resource scan."""


class Conn:
    def __init__(self):
        self.closed = False
        self.broken = False

    def close(self):
        self.closed = True


class Custom:
    def __init__(self):
        self.calls = 0

    def list_namespaced_custom_object(self, **kw):
        self.calls += 1
        return {"items": []}


def _run_scan(event_watcher, monkeypatch, cycles, between=None):
    opened, custom, slept = [], Custom(), {"n": 0}

    def factory():
        opened.append(Conn())
        return opened[-1]

    def sleep(_s):
        slept["n"] += 1
        if between:
            between(slept["n"], opened)
        if slept["n"] >= cycles:
            event_watcher.stop_event.set()

    monkeypatch.setattr(event_watcher.client, "CustomObjectsApi", lambda: custom, raising=False)
    monkeypatch.setattr(event_watcher, "STUCK_RESOURCE_POLL_INTERVAL", 1)
    monkeypatch.setattr(event_watcher.time, "sleep", sleep)
    event_watcher.stop_event.clear()
    event_watcher.watch_stuck_resources(factory)
    return opened, custom


def test_stuck_scan_reuses_one_connection_and_closes_it(event_watcher, monkeypatch):
    opened, custom = _run_scan(event_watcher, monkeypatch, cycles=3)
    assert len(opened) == 1 and opened[0].closed
    assert custom.calls == 3 * 4                     # 3 endpoint kinds + AITeams per scan


def test_stuck_scan_replaces_a_broken_connection(event_watcher, monkeypatch):
    def break_first(n, opened):
        if n == 1:
            opened[0].broken = True
    opened, _ = _run_scan(event_watcher, monkeypatch, cycles=3, between=break_first)
    assert len(opened) == 2 and all(c.closed for c in opened)
