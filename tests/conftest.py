import pytest

from tauceti_roadmap_worker import fetcher


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches the network: the real transport refuses, and tests pass a FakeTransport."""
    def refuse(self, url, headers):
        raise AssertionError(f"a test tried to fetch {url}")
    monkeypatch.setattr(fetcher.UrllibTransport, "request", refuse)
