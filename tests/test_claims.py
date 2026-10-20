from tauceti_roadmap_worker.claims import (author_key, held_by_other, job_key, lease_live, lease_payload, ref_safe,
                                           separation_problem)


def test_keys_are_valid_ref_names():
    assert ref_safe("REV-PKG-Foo~2") == "REV-PKG-Foo_r2"
    assert job_key("P", "RT-Foo~1a") == "roadmap/P/job/RT-Foo_r1a"
    assert author_key("P", "FIX-DESIGN-Foo~3") == "roadmap/P/author/FIX-DESIGN-Foo_r3"
    for key in (job_key("P", "RT-Foo~1a"), author_key("P", "FIX-DESIGN-Foo~3")):
        assert "~" not in key and ".." not in key and " " not in key


def test_lease_lifecycle():
    lease = lease_payload("roadmap/P/job/X", "alice/w1", "alice", "P", ttl=100, now=1000, host="h", pid=1)
    assert lease["schema"] == "tauceti-claim/v1" and lease["expires_at"] == 1100
    assert lease_live(lease, 1099) and not lease_live(lease, 1100)
    assert held_by_other(lease, "bob/w1", 1050)
    assert not held_by_other(lease, "alice/w1", 1050)      # our own lease is renewed, not refused
    assert not held_by_other(lease, "bob/w1", 2000)        # an expired lease can be taken over
    assert not lease_live(None, 0) and not lease_live({"expires_at": "junk"}, 0)


def test_separation_problem():
    recorded = {"PKG-Foo": "alice", "FIX-PKG-Foo~2": "bob"}
    assert separation_problem(["PKG-Foo", "FIX-PKG-Foo~2"], "Alice", recorded) == "operator Alice authored PKG-Foo"
    assert separation_problem(["PKG-Foo"], "carol", recorded) == ""
    assert separation_problem(["PKG-Foo"], "", {"PKG-Foo": ""}) == ""
