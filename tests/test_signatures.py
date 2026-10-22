"""The elaborated-signature transcript of a Suggested.lean. The outputs below are Lean 4.35.0-rc3's own
for the file T (recorded 2026-10-21): a `def` keeps only the binders its type or body uses, so `f1`
loses `[Inhabited X]`; any declaration loses a hypothesis it does not mention, so `t3` loses `hp`."""

import json
import shutil
import subprocess

import pytest

from tauceti_roadmap_worker import signatures as sig

T = """section
variable {X : Type} [Inhabited X] {M : Type} [Add M] (p : Nat) (hp : 0 < p) (hX : Nonempty X)
def f1 (x : X) : X := sorry
def f2 (x : X) : X := default
theorem t1 (x : X) : x = x := sorry
theorem t3 : p = p := sorry
def f3 : Nat := p
end
namespace TauCetiRoadmap.Toy
variable {n : Nat}
  (hn : 0 < n)
def twice (k : Nat) : Nat := k + k
theorem _root_.Nat.rooted : True := trivial
theorem uni.{u} (α : Sort u) : True := trivial
instance : Inhabited Nat := ⟨0⟩
end TauCetiRoadmap.Toy
"""
TEXT_OUTPUT = """T.lean:3:4: warning: declaration uses `sorry`
@f1 : {X : Type} → X → X
@f2 : {X : Type} → [Inhabited X] → X → X
@t1 : ∀ {X : Type} [Inhabited X] (x : X), x = x
t3 : ∀ (p : Nat), p = p
f3 : Nat → Nat
TauCetiRoadmap.Toy.twice : Nat → Nat
Nat.rooted : True
@TauCetiRoadmap.Toy.uni : ∀ (α : Sort u_1), True
"""


def json_output(lines_at):
    return "\n".join(json.dumps({"data": data, "pos": {"line": line, "column": 0}, "severity": sev})
                     for line, sev, data in lines_at)


def test_declarations_with_full_names_and_the_binders_in_scope():
    decls = sig.declarations(T)
    assert [d.name for d in decls] == ["f1", "f2", "t1", "t3", "f3", "TauCetiRoadmap.Toy.twice", "Nat.rooted",
                                       "TauCetiRoadmap.Toy.uni"]                  # the anonymous instance is skipped
    assert decls[0].binders == ["{X : Type}", "[Inhabited X]", "{M : Type}", "[Add M]", "(p : Nat)", "(hp : 0 < p)",
                                "(hX : Nonempty X)"]
    assert decls[5].binders == ["{n : Nat}", "(hn : 0 < n)"]                    # a variable command over two lines


def test_a_binder_is_flagged_only_when_its_objects_are_kept_and_it_is_not():
    decls = {d.name: d for d in sig.declarations(T)}
    assert sig.dropped(decls["f1"], "@f1 : {X : Type} → X → X") == ["[Inhabited X]", "(hX : Nonempty X)"]
    assert sig.dropped(decls["f2"], "@f2 : {X : Type} → [Inhabited X] → X → X") == ["(hX : Nonempty X)"]
    assert sig.dropped(decls["t3"], "t3 : ∀ (p : Nat), p = p") == ["(hp : 0 < p)"]
    assert sig.dropped(decls["f3"], "f3 : Nat → Nat") == []                     # p is not kept: nothing to flag
    assert sig.dropped(decls["TauCetiRoadmap.Toy.twice"], "TauCetiRoadmap.Toy.twice : Nat → Nat") == []


def test_transcript_from_json_and_from_text_output():
    decls = sig.declarations(T)
    text, at = sig.augmented(T, decls)
    assert text.splitlines()[-1] == "#check @TauCetiRoadmap.Toy.uni" and len(at) == len(decls)
    checks = {d.name: line for line, d in at.items()}
    out = json_output([(3, "warning", "declaration uses `sorry`"), (checks["f1"], "information", "@f1 : {X : Type} → X → X"),
                       (checks["t3"], "information", "t3 : ∀ (p : Nat), p = p"), (2, "error", "Unknown identifier `hn`")])
    found = sig.transcript(T, out, decls, at)
    assert found.flagged == 2 and found.errors == ["line 2: Unknown identifier `hn`"]
    assert dict((d.name, s) for d, s, _g in found.entries)["t3"] == "t3 : ∀ (p : Nat), p = p"
    plain = sig.transcript(T, TEXT_OUTPUT, decls, at)
    assert all(s for _d, s, _g in plain.entries)                               # every check found by name
    report = sig.report(plain, "Suggested.lean")
    assert "8 declarations; 4 with section binders that look dropped" in report and "Looks dropped: `[Inhabited X]`" in report
    assert sig.json_command("lake env lean {file}") == "lake env lean --json {file}"


@pytest.mark.skipif(not shutil.which("lean") or subprocess.run(["lean", "--version"], capture_output=True).returncode,
                    reason="needs a Lean toolchain")
def test_a_real_elaboration(tmp_path):
    lean = tmp_path / "Suggested.lean"
    lean.write_text(T)
    found = sig.elaborate("lean {file}", lean, tmp_path / "out", tmp_path)
    assert not found.reason, found.reason
    names = {d.name: (s, gone) for d, s, gone in found.entries}
    assert names["f1"][1] == ["[Inhabited X]", "(hX : Nonempty X)"] and names["t3"][1] == ["(hp : 0 < p)"]
    assert names["f3"][1] == [] and names["TauCetiRoadmap.Toy.twice"][0].startswith("TauCetiRoadmap.Toy.twice :")


def test_a_lane_writes_the_transcript_and_lists_it(tmp_path, monkeypatch):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    from tauceti_roadmap_worker.lanes.lake import LakeSetup
    job = new_job("REV-PKG-Mine~2")
    lane = lane_for(job)
    pkg = tmp_path / "board" / "packages" / "Mine"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("# Roadmap: mine\n")
    (pkg / "Suggested.lean").write_text(T)
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "s", board=tmp_path / "board",
                        content=tmp_path, workdir=tmp_path, report=tmp_path / "r.md", branch="b", job_id=job.id,
                        extra={"lake": LakeSetup(tmp_path)})
    ws.scratch.mkdir(parents=True)
    calls = []

    def fake(command, lean, out_dir, cwd):
        calls.append((command, lean, cwd))
        decls = sig.declarations(lean.read_text())
        return sig.transcript(lean.read_text(), TEXT_OUTPUT, decls, sig.augmented(lean.read_text(), decls)[1])
    monkeypatch.setattr(sig, "elaborate", fake)

    class Ctx:
        dry_run = False
        cfg = type("C", (), {"gates": type("G", (), {"elaborate": "lake env lean {file}"})(),
                             "board": type("B", (), {"packages_dir": "packages"})()})()
        state = tmp_path

        def cli(self, *args):
            return "tauceti-roadmap " + " ".join(args)
    lane.prepare_signatures(job, Ctx(), ws)
    assert calls == [("lake env lean {file}", pkg / "Suggested.lean", tmp_path)]
    assert "Looks dropped" in (ws.scratch / "signatures.md").read_text()
    assert "4 of them look to have lost a section binder" in lane.tools_text(Ctx(), ws)


def test_a_file_that_runs_code_is_never_elaborated(tmp_path):
    assert sig.executes_code("#eval IO.println 1\n") == ["#eval", "IO"]
    assert sig.executes_code("run_cmd do pure ()\nmacro \"x\" : term => `(1)\n") == ["macro", "run_cmd"]
    assert sig.executes_code("-- #eval in a comment\n/- run_cmd -/\ntheorem t : True := trivial\n") == []
    lean = tmp_path / "Suggested.lean"
    lean.write_text(T + "\nrun_cmd pure ()\n")
    found = sig.elaborate("definitely-not-a-command {file}", lean, tmp_path / "out", tmp_path)
    assert "runs code at elaboration (`run_cmd`)" in found.reason and not (tmp_path / "out").exists()


def test_a_pass_uses_ready_packages_without_fetching_and_skips_forbidden_files(tmp_path, monkeypatch):
    from tauceti_roadmap_worker.board import new_job
    from tauceti_roadmap_worker.lanes import base, lane_for
    from tauceti_roadmap_worker.lanes.lake import LakeSetup
    job = new_job("RT-Mine~1a", inputs={"board_pr": 12})
    lane = lane_for(job)
    assert lane.transcribes_if_ready and not lane.elaborates(job)
    pkg = tmp_path / "board" / "packages" / "Mine"
    pkg.mkdir(parents=True)
    (pkg / "README.md").write_text("# Roadmap: mine\n")
    (pkg / "Suggested.lean").write_text(T)
    ws = base.Workspace(job_dir=tmp_path / "j", scratch=tmp_path / "j" / "s", board=tmp_path / "board",
                        content=tmp_path, workdir=tmp_path / "board", report=tmp_path / "r.md", branch="b",
                        job_id=job.id)
    ws.scratch.mkdir(parents=True)
    fetches, ready = [], {"value": True}

    def fake_lake(checkout, state, content=None, fetch=True, **kw):
        fetches.append(fetch)
        return LakeSetup(tmp_path) if ready["value"] else LakeSetup(tmp_path, reason="not fetched yet")
    monkeypatch.setattr(base, "prepare_lake", fake_lake)
    monkeypatch.setattr(sig, "elaborate", lambda command, lean, out_dir, cwd: sig.transcript(
        T, TEXT_OUTPUT, sig.declarations(T), sig.augmented(T, sig.declarations(T))[1]))

    class Ctx:
        dry_run, state = False, tmp_path
        cfg = type("C", (), {"gates": type("G", (), {"elaborate": "lake env lean {file}", "forbidden_patterns": []})(),
                             "board": type("B", (), {"packages_dir": "packages"})()})()
    lane.prepare_signatures(job, Ctx(), ws)
    assert fetches == [False] and ws.extra["signatures"][2] == 4               # ready: transcribed, no fetch
    ready["value"], ws.extra = False, {}
    lane.prepare_signatures(job, Ctx(), ws)
    assert fetches == [False, False] and "signatures" not in ws.extra           # not fetched: nothing, still no fetch
    ready["value"], ws.extra = True, {}
    Ctx.cfg.gates.forbidden_patterns = [r"\bf1\b"]
    lane.prepare_signatures(job, Ctx(), ws)
    assert "forbidden patterns (line 3)" in ws.extra["signatures"][3]
