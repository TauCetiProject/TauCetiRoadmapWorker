"""The host-wide throttle on GitHub content writes: a token bucket in one file that every process on
the host shares, so the operator's account stays under GitHub's secondary rate limit."""

from tauceti_roadmap_worker import throttle
from tauceti_roadmap_worker.gh import GH

from .fakes import example_config


class Clock:
    def __init__(self):
        self.now, self.slept = 1_000_000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_a_burst_waits_for_the_minute_window(tmp_path):
    clock = Clock()
    t = throttle.WriteThrottle(tmp_path / "w.json", per_minute=3, per_hour=100, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        assert t.wait() == 0
    assert clock.slept == []
    waited = t.wait()                      # the fourth write waits until the first one is a minute old
    assert waited > 0 and 59 <= clock.slept[0] <= 61
    clock.now += 10
    assert t.wait() == 0                   # one more slot: the second write is now a minute old


def test_the_hour_cap_holds_across_processes(tmp_path):
    clock = Clock()
    a = throttle.WriteThrottle(tmp_path / "w.json", per_minute=0, per_hour=4, clock=clock, sleep=clock.sleep)
    b = throttle.WriteThrottle(tmp_path / "w.json", per_minute=0, per_hour=4, clock=clock, sleep=clock.sleep)
    for t in (a, b, a, b):
        assert t.wait() == 0
    assert a.wait() >= 3500 and sum(clock.slept) >= 3500      # slept in minute-long steps until the first write aged out


def test_zero_rates_disable_the_throttle(tmp_path):
    cfg = example_config()
    cfg.worker.writes_per_minute = cfg.worker.writes_per_hour = 0
    assert throttle.for_config(cfg) is None
    cfg.worker.writes_per_hour = 10
    assert throttle.for_config(cfg) is not None


def test_gh_writes_take_a_slot_but_reads_and_dry_runs_do_not(monkeypatch, tmp_path, capsys):
    clock = Clock()
    t = throttle.WriteThrottle(tmp_path / "w.json", per_minute=1, per_hour=10, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(GH, "throttle", t)
    calls = []
    monkeypatch.setattr(GH, "_run", lambda self, args, stdin=None: calls.append(args) or "{}")
    gh = GH()
    gh.write("POST", "repos/o/r/issues/1/comments", {"body": "x"})
    gh.write("POST", "repos/o/r/issues/1/comments", {"body": "y"})     # second write in the minute: sleeps
    assert len(calls) == 2 and clock.slept and clock.slept[0] >= 59
    before = len(clock.slept)
    gh.api("repos/o/r")                                                  # a read never waits
    GH(dry_run=True).write("POST", "repos/o/r/issues/1/comments", {})   # nor a dry run
    assert len(clock.slept) == before
