import pytest

from tauceti_roadmap_worker import fetcher


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches the network: the real transport refuses, and tests pass a FakeTransport."""
    def refuse(self, url, headers):
        raise AssertionError(f"a test tried to fetch {url}")
    monkeypatch.setattr(fetcher.UrllibTransport, "request", refuse)


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """The host's free space never decides a test (the tests' state directories live in a tmpfs);
    the tests of the disk guard set it themselves."""
    from tauceti_roadmap_worker.lanes import base
    monkeypatch.setattr(base, "free_gb", lambda path: 1000.0)


@pytest.fixture(autouse=True)
def no_lake_cache_fetch(monkeypatch):
    """No test runs `lake exe cache get` for real (it downloads gigabytes): the command becomes `false`."""
    from tauceti_roadmap_worker.lanes import pr
    monkeypatch.setattr(pr, "CACHE_GET", ("false",))
