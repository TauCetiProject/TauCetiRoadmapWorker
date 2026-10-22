"""The board's merge order repaired where a roadmap is listed above one of its suppliers: the
backfill placed imports by READMEs that named their suppliers under names the dependency check did
not know, and a later fix repointed them (2026-10-22: PeriodsAndSpecialValues at #151, its suppliers
SelmerIwasawaCohomologyPartII and GanGrossPrasadUnitaryGroups at #173 and #185). `board intake`
moves every such roadmap once (idempotent), reading the board's READMEs by git."""

import base64
import subprocess

from tauceti_roadmap_worker import depgraph, gitops, intake
from tauceti_roadmap_worker.board import encode_submission

from .fakes import bot_context

ORDER = "merge-order/ORDER.txt"
LIVE = ("# Merge order: suppliers before consumers, one roadmap per line.\nSchemeAndStackFoundations\n"
        "PeriodsAndSpecialValues\nMotivesAndAlgebraicCycles\nSelmerIwasawaCohomologyPartII\nShimuraData\n"
        "GanGrossPrasadUnitaryGroups\n")
PS = ("# Periods and special values\n\n## Layer PS.6\n\n*Needs:* SelmerIwasawaCohomologyPartII L4.24; "
      "GanGrossPrasadUnitaryGroups GGP.8; MotivesAndAlgebraicCycles MC.2.\n")


class OrderGH:
    """The board's merge-order file through the contents API, and its open pull requests."""

    def __init__(self, order, pulls=()):
        self.files, self.pulls, self.writes = {ORDER: order}, list(pulls), []

    def api(self, path, paginate=False):
        if "/pulls?state=open" in path:
            return list(self.pulls)
        if path.endswith(f"/contents/{ORDER}"):
            return {"sha": f"blob{len(self.writes)}", "content": base64.b64encode(self.files[ORDER].encode()).decode()}
        raise AssertionError(path)

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        self.files[ORDER] = base64.b64decode(payload["content"]).decode()


def bot(tmp_path, gh):
    ctx = bot_context(gh=gh)
    ctx.state = tmp_path / "state"
    return ctx


def test_the_intake_moves_a_roadmap_listed_above_its_suppliers_once(tmp_path, monkeypatch):
    gh = OrderGH(LIVE, pulls=[{"number": 591}])
    seen = []
    monkeypatch.setattr(intake, "board_readmes", lambda ctx, pulls: seen.append(pulls) or {
        "PeriodsAndSpecialValues": PS, "ShimuraData": "*Needs:* MotivesAndAlgebraicCycles MC.1.\n"})
    ctx = bot(tmp_path, gh)
    moved = ("merge order: PeriodsAndSpecialValues moved in the merge order from #2 to #6, right after its last "
             "supplier GanGrossPrasadUnitaryGroups (MotivesAndAlgebraicCycles, SelmerIwasawaCohomologyPartII also "
             "came after it)")
    assert intake.replace_upward_roadmaps(ctx, yes=False) == [moved + " (dry run: not written)"]
    assert gh.writes == [] and seen == [[{"number": 591}]]
    assert intake.replace_upward_roadmaps(ctx, yes=True) == [moved]
    assert depgraph.read_order(gh.files[ORDER])[-2:] == ["GanGrossPrasadUnitaryGroups", "PeriodsAndSpecialValues"]
    [(method, path, payload)] = gh.writes
    assert (method, path) == ("PUT", f"repos/TauCetiProject/TauCetiRoadmapJobs/contents/{ORDER}")
    assert payload["message"] == "Merge order: move PeriodsAndSpecialValues after its last supplier"
    assert payload["sha"] == "blob0" and gh.files[ORDER].startswith("# Merge order: suppliers before consumers")
    # Idempotent: the next run reads, writes nothing and says nothing.
    assert intake.replace_upward_roadmaps(ctx, yes=True) == [] and len(gh.writes) == 1


def test_a_move_that_would_put_a_consumer_first_is_logged_and_not_made(tmp_path, monkeypatch):
    gh = OrderGH(LIVE)
    monkeypatch.setattr(intake, "board_readmes", lambda ctx, pulls: {
        "PeriodsAndSpecialValues": PS, "ShimuraData": "*Needs:* PeriodsAndSpecialValues PS.1.\n"})
    lines = intake.replace_upward_roadmaps(bot(tmp_path, gh), yes=True)
    assert lines == [("merge order: PeriodsAndSpecialValues not moved in the merge order: PeriodsAndSpecialValues "
                      "consumes GanGrossPrasadUnitaryGroups (#6) and is consumed by ShimuraData (#5), which comes "
                      "first: moving PeriodsAndSpecialValues after GanGrossPrasadUnitaryGroups would put a supplier "
                      "after a consumer, a cycle through PeriodsAndSpecialValues")]
    assert gh.writes == []


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def test_the_board_readmes_come_from_main_and_from_the_heads_of_the_open_pull_requests(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    files = {"packages/Alpha/README.md": "Alpha on main\n", "packages/Alpha/reports/R.md": "a report\n",
             "packages/Beta/design/README.md": "Beta's design\n", "packages/Gamma/README.md": "Gamma on main\n",
             "packages/Gamma/design/README.md": "Gamma's design\n", "merge-order/ORDER.txt": "Alpha\n"}
    for rel, text in files.items():
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
    git(origin, "init", "-q", "-b", "main")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "main")
    # Pull request #7 carries Gamma (a package under review); its head also has an older Alpha README.
    git(origin, "checkout", "-q", "-b", "w1/PKG-Gamma")
    (origin / "packages/Gamma/README.md").write_text("Gamma at the head of #7\n")
    (origin / "packages/Alpha/README.md").write_text("Alpha as #7's base had it\n")
    (origin / "packages/Delta/README.md").parent.mkdir(parents=True)
    (origin / "packages/Delta/README.md").write_text("Delta, new in #7 but not its roadmap\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "PKG-Gamma")
    git(origin, "update-ref", "refs/pull/7/head", "HEAD")
    git(origin, "update-ref", "refs/pull/9/head", "HEAD")
    git(origin, "checkout", "-q", "main")
    monkeypatch.setattr(gitops, "url", lambda repo: (origin.as_uri()))
    review = [{"name": "needs-review"}]
    pulls = [{"number": 7, "body": encode_submission("PKG-Gamma", "w1", "alice", "codex"), "labels": review},
             {"number": 8, "body": "no marker", "labels": review},
             {"number": 9, "body": encode_submission("PKG-Alpha", "w1", "alice", "codex")}]   # not taken in
    ctx = bot(tmp_path, OrderGH(""))
    expected = {"Alpha": "Alpha on main\n", "Beta": "Beta's design\n", "Gamma": "Gamma at the head of #7\n"}
    assert intake.board_readmes(ctx, pulls) == expected
    assert intake.board_readmes(ctx, pulls) == expected            # a second run fetches into the same checkout
