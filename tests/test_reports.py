"""Roadmap reports: the marker, the harvest from the library on GitHub and Forgejo, the REPORT jobs and
the register `board sync` keeps (reports.py), and the safety of a job kind no lane takes."""

import base64
import copy
import datetime as dt
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tauceti_roadmap_worker import board, cli, intake, reports, watch, worker
from tauceti_roadmap_worker.board import Board, Job, new_job
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import Context

from .fakes import FakeBoard, FakeClaims, FakeIssuesGH, FakeTransport, example_config, job_issue

SHA = "a" * 40
LIBRARY = "https://github.com/TauCetiProject/TauCeti"
FORGEJO = "https://git.taucetiproject.org"
README = "TauCetiRoadmap/QuadraticFormInvariants/README.md"
ANCHOR = "Two forms of rank one are equivalent iff their diagonal chains are"


def marker(**over) -> str:
    data = {"roadmap": "QuadraticFormInvariants", "file": "README.md", "roadmap_sha": SHA, "anchor": ANCHOR,
            "kind": "false-statement", "effect": "worked-around",
            "refutes": ["TauCeti.exists_rankOne_equivalent_not_diagonalChain"],
            "replaces": ["TauCeti.diagonalChain_iff_equivalent_of_two_le"]} | over
    return reports.MARKER + json.dumps({k: v for k, v in data.items() if v is not None}) + "-->"


BODY = f"""This PR proves the Witt chain equivalence in rank at least two.

### Roadmap report

The roadmap states the chain equivalence for every rank; it is false in rank one (see
`TauCeti.exists_rankOne_equivalent_not_diagonalChain`). @someone please look. <!-- a comment -->

### Test plan

- lake build

{marker()}
"""


# --- the marker ------------------------------------------------------------------------------------------

def test_a_valid_marker_is_read_and_its_unknown_fields_ignored():
    found, problems = reports.parse("text\n" + marker(future="ignored", anchor="  Two  forms\nof rank one ") + "\nmore")
    assert problems == [] and len(found) == 1
    r = found[0]
    assert (r.roadmap, r.file, r.roadmap_sha, r.kind, r.effect) == ("QuadraticFormInvariants", "README.md", SHA,
                                                                   "false-statement", "worked-around")
    assert r.anchor == "Two forms of rank one" and r.identity[2] == "two forms of rank one"
    assert r.refutes == ("TauCeti.exists_rankOne_equivalent_not_diagonalChain",)


def test_braces_and_quotes_inside_an_anchor_are_read():
    anchor = "theorem foo {R : Type*} [CommRing R] (x : R) : f {x} = \"}\""
    found, problems = reports.parse(marker(file="Suggested.lean", anchor=anchor))
    assert problems == [] and found[0].anchor == anchor


@pytest.mark.parametrize("over, why", [
    ({"roadmap": None}, "`roadmap` is missing"),
    ({"roadmap": "Quadratic-Forms"}, "`roadmap` is not a roadmap folder"),
    ({"roadmap": "1Forms"}, "`roadmap` is not a roadmap folder"),
    ({"file": "STATUS.md"}, "`file` is not one of"),
    ({"roadmap_sha": "abc123"}, "`roadmap_sha` is not 40 lowercase hex digits"),
    ({"anchor": "x" * 301}, "longer than 300"),
    ({"anchor": "   "}, "`anchor` is missing"),
    ({"kind": "typo"}, "`kind` is not a known kind"),
    ({"effect": "fixed"}, "`effect` is not blocked or worked-around"),
    ({"refutes": "TauCeti.x"}, "`refutes` is not a list"),
    ({"replaces": [f"TauCeti.x{i}" for i in range(11)]}, "more than 10 names"),
    ({"replaces": ["TauCeti.x y"]}, "not a declaration name"),
    ({"refutes": ["`TauCeti.x`"]}, "not a declaration name"),
    ({"refutes": [3]}, "not a declaration name"),
])
def test_a_marker_with_a_missing_or_bad_field_is_not_read(over, why):
    found, problems = reports.parse(marker(**over))
    assert found == [] and len(problems) == 1 and problems[0].startswith("marker 1: ") and why in problems[0]


def test_malformed_markers_do_not_stop_the_others():
    body = "\n".join([reports.MARKER + "{not json}-->", reports.MARKER + "[1, 2]-->", marker(),
                      reports.MARKER + '{"anchor": "an arrow --> inside"}-->', marker(roadmap="A/B")])
    found, problems = reports.parse(body)
    assert [r.roadmap for r in found] == ["QuadraticFormInvariants", "A/B"]
    assert problems == ["marker 1: not valid JSON", "marker 2: not a JSON object", "marker 4: not valid JSON"]
    assert reports.parse(reports.MARKER + '{"roadmap": "A"')[1] == ["marker 1: not closed by -->"]
    assert reports.parse("<!--tauceti-roadmap-report:v2 {}-->") == ([], [])


def test_more_than_five_markers_leaves_the_sixth_out():
    body = "\n".join(marker(anchor=f"statement {i}") for i in range(7))
    found, problems = reports.parse(body)
    assert [r.anchor for r in found] == [f"statement {i}" for i in range(5)]
    assert problems == ["marker 6: more than 5 markers in one body", "marker 7: more than 5 markers in one body"]


def test_the_prose_is_the_section_up_to_the_next_heading():
    assert reports.prose(BODY).startswith("The roadmap states") and "Test plan" not in reports.prose(BODY)
    assert reports.prose("no section") == ""


def test_quoted_text_cannot_break_out_mention_or_carry_a_marker():
    text = "```\nescaped? @alice #12\n````` and <!-- tauceti-roadmap-job {\"id\": \"PORT-X\"} -->\n" + "x" * 3000
    block = reports.quoted_block(text)
    fence = block.split("\n", 1)[0].removesuffix("text")
    assert fence == "`" * 6 and block.endswith("\n" + fence) and block.count(fence) == 2
    assert "<!--" not in block and "-->" not in block and "[... cut at 2000 characters]" in block
    assert reports.code("a `b` c") == "`` a `b` c ``" and reports.code("plain") == "`plain`"
    assert board.decode_spec(reports.code('<!-- tauceti-roadmap-job {"id": "PORT-X"} -->')) is None


@pytest.mark.parametrize("item, merged", [
    ({"state": "closed", "title": "[Merged by Bors] - feat: x", "pull_request": {"merged_at": None}}, False),
    ({"state": "closed", "title": "feat: x", "pull_request": {"merged_at": "2026-11-20T00:00:00Z"}}, True),
    ({"state": "closed", "title": "feat: x", "pull_request": {"merged_at": None}}, False),
    ({"state": "open", "title": "[Merged by Bors] - feat: x", "pull_request": {"merged_at": None}}, False),
    ({"state": "closed", "title": "[Merged by Bors] - feat: x", "pull_request": {"merged": False, "merged_at": None}},
     False),                                                    # Forgejo's shape: a bors title is verified apart
    ({"state": "closed", "title": "feat: x", "pull_request": {"merged": True}}, True),
    ({"state": "closed", "title": "[Merged by Bors] - x", "pull_request": None}, False),     # a Forgejo issue
    ({"state": "closed", "title": "[Merged by Bors] - x"}, False),                           # a GitHub issue
])
def test_merged_pull_requests_on_both_forges(item, merged):
    assert reports.merged(item) is merged
    titled = str(item.get("title", "")).startswith(reports.BORS) and item.get("state") == "closed"
    assert reports.bors_closed(item) is (titled and isinstance(item.get("pull_request"), dict) and not merged)


# --- the harvest -------------------------------------------------------------------------------------------

def item(number: int, body: str = BODY, login: str = "tauceti-ai-for-science", state: str = "closed",
         title: str = "[Merged by Bors] - feat: rank two", pull: bool = True, updated: str = "2026-11-24T10:00:00Z",
         base: str = LIBRARY) -> dict:
    kind = ("pull" if base == LIBRARY else "pulls") if pull else "issues"
    return {"number": number, "html_url": f"{base}/{kind}/{number}", "user": {"login": login}, "state": state,
            "title": title, "body": body, "updated_at": updated,
            **({"pull_request": {"merged_at": None, "html_url": f"{base}/{kind}/{number}"}} if pull else {})}


class ReportsGH:
    """GitHub for the reports step: the issue search of the library, the board's register by the contents
    API, the roadmap repository's files by GraphQL blobs (at `main` and at full commits)."""

    def __init__(self, search=(), main=None, at=None):
        self.search = list(search)
        self.main = dict(main if main is not None else {README: "intro\n- Two forms of rank one are equivalent\n"
                                                        "  iff their diagonal   chains are equal.\n"})
        self.at = dict(at or {})
        self.files: dict[str, str] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self.queries: list[str] = []
        self.closers: dict[int, str] = {}                   # who closed a pull request (default: the merge queue)
        self.closer_reads: list[int] = []
        self.dry_run, self.fence = False, None

    def api(self, path, paginate=False):
        if path.startswith("search/issues?"):
            q = parse_qs(urlsplit(path).query)
            self.queries.append(q["q"][0])
            page = int(q["page"][0])
            return {"total_count": len(self.search), "items": copy.deepcopy(self.search[(page - 1) * 100:page * 100])}
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in self.files:
                raise GHError(f"GET {path}: gh: Not Found (HTTP 404)")
            return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": "s-" + name}
        m = re.fullmatch(r"repos/TauCetiProject/TauCeti/issues/(\d+)", path)
        if m:
            self.closer_reads.append(int(m.group(1)))
            who = self.closers.get(int(m.group(1)), "tauceti-queue[bot]")
            if who is None:
                raise GHError(f"GET {path}: HTTP 502")
            listed = next((i for i in self.search if i["number"] == int(m.group(1))), {})
            return {**copy.deepcopy(listed), "number": int(m.group(1)), "closed_by": {"login": who}}
        raise GHError(f"GET {path}: not faked")

    def graphql(self, query, **variables):
        out = {}
        for alias, expr in re.findall(r'(b\d+): object\(expression: ("(?:[^"\\]|\\.)*")\)', query):
            ref, _, path = json.loads(expr).partition(":")
            text = self.main.get(path) if ref == "main" else self.at.get((ref, path))
            out[alias] = None if text is None else {"text": text, "isTruncated": False}
        return {"repository": out}

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload or {}))
        if method == "PUT" and "/contents/" in path:
            self.files[path.split("/contents/", 1)[1]] = base64.b64decode(payload["content"]).decode()
        return {}

    def register(self) -> dict:
        return json.loads(self.files[reports.REGISTER])


def context(gh, jobs=(), dry_run=False, transport=None, **over):
    cfg = example_config()
    cfg.reports.enabled = True
    for key, value in over.items():
        setattr(cfg.reports, key, value)
    fb = FakeBoard(jobs=list(jobs))
    return SimpleNamespace(cfg=cfg, gh=gh, board=fb, operator="maintainer", dry_run=dry_run, transport=transport,
                           state=cfg.state_dir())


NOW = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.UTC)


def test_a_merged_bors_pull_request_files_one_job_and_the_register_records_it():
    gh = ReportsGH([item(6903)])
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    [(job, state, preamble)] = ctx.board.created
    assert job.id == "REPORT-QuadraticFormInvariants~1" and state == "available"
    assert job.inputs["anchor"] == ANCHOR and job.inputs["sources"] == [f"{LIBRARY}/pull/6903"]
    assert f"reports: {LIBRARY}/pull/6903: filed REPORT-QuadraticFormInvariants~1 (#501) (QuadraticFormInvariants " \
           "README.md, false-statement, state:available)" in out
    # The job's text is the board's own; the prose is quoted data with no live mention or comment.
    assert "`TauCeti.exists_rankOne_equivalent_not_diagonalChain`" in preamble and "a merged pull request" in preamble
    assert "```text\nThe roadmap states" in preamble and "<!--" not in preamble and "Test plan" not in preamble
    entry = gh.register()["roadmaps"]["QuadraticFormInvariants"][0]
    assert entry == {"job": "REPORT-QuadraticFormInvariants~1", "file": "README.md", "anchor": ANCHOR,
                     "kind": "false-statement", "effect": "worked-around", "state": "open",
                     "sources": [f"{LIBRARY}/pull/6903"],
                     "refutes": ["TauCeti.exists_rankOne_equivalent_not_diagonalChain"],
                     "replaces": ["TauCeti.diagonalChain_iff_equivalent_of_two_le"],
                     "evidence": {f"{LIBRARY}/pull/6903": {
                         "kind": "false-statement", "effect": "worked-around",
                         "refutes": ["TauCeti.exists_rankOne_equivalent_not_diagonalChain"],
                         "replaces": ["TauCeti.diagonalChain_iff_equivalent_of_two_le"]}}}
    assert gh.register()["version"] == 1 and gh.register()["scanned"] == {"github": "2026-11-24T10:00:00Z"}
    assert gh.queries == ['repo:TauCetiProject/TauCeti is:pr "tauceti-roadmap-report:v1" in:body']
    # The next run searches from the mark, finds the same source recorded, and writes nothing.
    ctx.board.jobs = [Job(job.id, "report", job.name, 1, inputs=job.inputs, number=501, state="available")]
    writes = len(gh.writes)
    out = reports.sync_reports(ctx, now=NOW)
    assert gh.queries[-1].endswith(" updated:>=2026-11-24T09:55:00Z")
    assert len(ctx.board.created) == 1 and len(gh.writes) == writes
    assert out == ["reports: 1 source(s) read, 1 report(s) recorded already, 0 filed, 0 fixed"]


def test_the_feature_is_off_by_default():
    gh = ReportsGH([item(1)])
    ctx = context(gh, enabled=False)
    assert example_config().reports.enabled is False
    assert reports.sync_reports(ctx) == [] and gh.queries == [] and gh.writes == []


def test_open_pull_requests_count_only_from_listed_reporters_and_issues_never():
    gh = ReportsGH([item(1, state="open", title="feat: a", login="stranger"),
                    item(2, state="open", title="feat: b", login="Worker-Bot", body=marker(anchor="diagonal chains")),
                    item(3, pull=False, state="open", title="Roadmap report", login="worker-bot",
                         body=marker(anchor="rank one are equivalent")),
                    item(4, state="closed", title="feat: abandoned", login="worker-bot", body=marker(anchor="intro")),
                    item(5, state="open", title="feat: c", login="worker-bot", body=marker(anchor="rank one"))])
    ctx = context(gh, reporters=["worker-bot"])
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~1",
                                                          "REPORT-QuadraticFormInvariants~2"]
    assert f"reports: {LIBRARY}/pull/1: an open pull request of @stranger, who is not a listed reporter; not " \
           "harvested" in out
    assert f"reports: {LIBRARY}/pull/4: a pull request closed unmerged; not harvested" in out
    # An issue's marker is never read, even from a listed reporter: reports travel only in pull requests.
    assert not any("/issues/3" in line for line in out)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/2"], [f"{LIBRARY}/pull/5"]]


def test_the_same_report_from_two_sources_is_one_job_with_both_sources():
    other = marker(anchor="two forms of RANK one  are equivalent iff their diagonal chains are")
    gh = ReportsGH([item(1), item(2, body=other, updated="2026-11-24T10:05:00Z")])
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert len(ctx.board.created) == 1
    entry = gh.register()["roadmaps"]["QuadraticFormInvariants"][0]
    assert entry["sources"] == [f"{LIBRARY}/pull/1", f"{LIBRARY}/pull/2"]
    assert any("pull/2: QuadraticFormInvariants README.md: already reported" in line for line in out)
    # A later source of an open report: its job says so in one comment.
    ctx.board.jobs = [Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=501,
                          state="available", inputs=ctx.board.created[0][0].inputs)]
    gh.search = [item(3, body=other, updated="2026-11-24T11:00:00Z")]
    reports.sync_reports(ctx, now=NOW)
    assert ctx.board.posted == [(501, f"Reported again in {LIBRARY}/pull/{n} (a merged pull request).") for n in (2, 3)]
    assert gh.register()["roadmaps"]["QuadraticFormInvariants"][0]["sources"][-1] == f"{LIBRARY}/pull/3"


def test_an_anchor_gone_from_main_is_recorded_fixed_and_one_never_there_is_not_a_report():
    path = README
    gh = ReportsGH([item(1, body=marker(anchor="an old statement")), item(2, body=marker(anchor="never written"))],
                   main={path: "a rewritten roadmap\n"}, at={(SHA, path): "it had an old\nstatement once"})
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == []
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["state"] == "fixed" and entry["job"] == "" and entry["anchor"] == "an old statement"
    assert f"reports: {LIBRARY}/pull/2: QuadraticFormInvariants README.md: the anchor is not in the file at " \
           f"{SHA[:7]} either; not a report" in out


def test_a_report_against_a_completed_roadmap_is_parked_for_a_human():
    gh = ReportsGH([item(1)], main={"Completed/QuadraticFormInvariants/README.md": ANCHOR + "."})
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    [(job, state, preamble)] = ctx.board.created
    assert state == "blocked" and job.inputs["place"] == "completed"
    assert "`Completed/QuadraticFormInvariants/README.md`" in preamble and "needs a human" in preamble


def test_caps_per_run_and_per_author_leave_the_rest_for_later_runs():
    gh = ReportsGH([item(1, body=marker(anchor="intro"), updated="2026-11-24T10:01:00Z"),
                    item(2, body=marker(anchor="Two forms"), updated="2026-11-24T10:02:00Z"),
                    item(3, login="other", body=marker(anchor="diagonal chains"), updated="2026-11-24T10:03:00Z"),
                    item(4, login="third", body=marker(anchor="rank one"), updated="2026-11-24T10:04:00Z")])
    ctx = context(gh, per_run=2, per_author=1)
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/1"], [f"{LIBRARY}/pull/3"]]
    assert f"reports: {LIBRARY}/pull/2: QuadraticFormInvariants README.md: left for a later run ([reports] " \
           "per_author)" in out
    assert f"reports: {LIBRARY}/pull/4: QuadraticFormInvariants README.md: left for a later run ([reports] " \
           "per_run)" in out
    # The mark moves on; the reports left over are queued and read again by their number.
    assert gh.register()["scanned"]["github"] == "2026-11-24T10:04:00Z"
    assert [q["url"] for q in gh.register()["retry"]] == [f"{LIBRARY}/pull/2", f"{LIBRARY}/pull/4"]
    assert out[-2].endswith("2 filed, 0 fixed, 2 left for a later run")


def test_a_dry_run_decides_everything_and_writes_nothing():
    gh = ReportsGH([item(1)])
    ctx = context(gh, dry_run=True)
    out = reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == [] and gh.writes == [] and reports.REGISTER not in gh.files
    assert f"reports: {LIBRARY}/pull/1: would file REPORT-QuadraticFormInvariants~1 (QuadraticFormInvariants " \
           "README.md, false-statement, state:available) (dry run)" in out
    assert out[-1] == f"reports: {reports.REGISTER} not written (dry run)"


def test_an_open_report_whose_anchor_left_main_is_fixed_and_its_job_done():
    gh = ReportsGH([], main={README: "corrected text"})
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"}, "roadmaps": {
        "QuadraticFormInvariants": [reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1",
                                                      "open", f"{LIBRARY}/pull/1")]}})
    job = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=77, state="available",
              inputs={**json.loads(json.dumps(reports.new_entry(reports.parse(marker())[0][0], "", "", ""))),
                      "roadmap": "QuadraticFormInvariants", "roadmap_sha": SHA})
    ctx = context(gh, jobs=[job])
    out = reports.sync_reports(ctx, now=NOW)
    assert ctx.board.states == [("REPORT-QuadraticFormInvariants~1", "done")]
    assert ("PATCH", "repos/TauCetiProject/TauCetiRoadmapJobs/issues/77", {"state": "closed"}) in gh.writes
    assert gh.register()["roadmaps"]["QuadraticFormInvariants"][0]["state"] == "fixed"
    assert "reports: REPORT-QuadraticFormInvariants~1: the anchor is gone from QuadraticFormInvariants/README.md on " \
           "main; fixed, job done" in out
    # A report fixed once, whose text comes back to main, is filed again as the next round.
    gh.main[README] = ANCHOR
    ctx.board.jobs[0].state, ctx.board.jobs[0].closed = "done", True
    gh.search = [item(9, updated="2026-11-24T11:00:00Z")]
    reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~2"]
    entry = gh.register()["roadmaps"]["QuadraticFormInvariants"][0]
    assert entry["state"] == "open" and entry["job"] == "REPORT-QuadraticFormInvariants~2"
    assert entry["sources"] == [f"{LIBRARY}/pull/1", f"{LIBRARY}/pull/9"]


def test_main_that_cannot_be_read_decides_nothing():
    class Down(ReportsGH):
        def graphql(self, query, **variables):
            raise GHError("graphql: HTTP 502")

        def raw_file(self, repo, path, ref=""):
            raise GHError("HTTP 502")
    gh = Down([item(1, updated="2026-11-24T10:00:00Z")])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T08:00:00Z"},
                                             "roadmaps": {}})
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == [] and gh.register()["roadmaps"] == {}     # only the verified merge is recorded
    assert gh.register()["retry"] == [{"url": f"{LIBRARY}/pull/1", "forge": "github"}]     # queued for later
    assert f"reports: {LIBRARY}/pull/1: QuadraticFormInvariants README.md: main could not be read; left for a later " \
           "run" in out


def test_a_failed_register_write_is_recovered_from_the_job_on_the_board():
    report = reports.parse(marker())[0][0]
    job = new_job("REPORT-QuadraticFormInvariants~1", inputs={
        "roadmap": report.roadmap, "file": report.file, "roadmap_sha": report.roadmap_sha, "anchor": report.anchor,
        "kind": report.kind, "effect": report.effect, "refutes": list(report.refutes), "replaces": [],
        "sources": [f"{LIBRARY}/pull/1"]})
    job.number, job.state = 12, "available"
    gh = ReportsGH([item(1)])
    ctx = context(gh, jobs=[job])
    reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == []
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["job"] == job.id and entry["sources"] == [f"{LIBRARY}/pull/1"] and entry["state"] == "open"


def test_forgejo_pull_requests_are_harvested_by_their_bors_title():
    transport = FakeTransport()
    base = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues?type=pulls&state=all&limit=50"
    forgejo_pr = item(3668, base=f"{FORGEJO}/TauCetiProject/TauCeti", updated="2026-11-23T11:40:44Z")
    forgejo_pr["pull_request"] = {"merged": False, "merged_at": None, "draft": False,
                                  "html_url": forgejo_pr["html_url"]}
    open_one = item(3700, base=f"{FORGEJO}/TauCetiProject/TauCeti", state="open", title="feat: y", login="kim",
                    body=marker(anchor="diagonal chains"), updated="2026-11-23T11:00:00Z")
    open_one["pull_request"] = {"merged": False, "merged_at": None}
    transport.add(f"{base}&page=1", json.dumps([forgejo_pr, open_one]).encode())
    gh = ReportsGH([])
    timeline = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues/3668/timeline?limit=50&page=1"
    transport.add(timeline, json.dumps([{"type": "change_title", "user": {"login": "queue"}},
                                        {"type": "close", "user": {"login": "queue"}}]).encode())
    ctx = context(gh, transport=transport, forgejo=FORGEJO, forgejo_reporters=["Kim"], forgejo_merge_bots=["queue"])
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [
        [f"{FORGEJO}/TauCetiProject/TauCeti/pulls/3700"], [f"{FORGEJO}/TauCetiProject/TauCeti/pulls/3668"]]
    assert gh.register()["scanned"]["forgejo"] == "2026-11-23T11:40:44Z" and "2 filed" in out[-2]
    # The next listing asks for what changed since the mark, less the margin.
    transport.add(f"{base}&page=1&since=2026-11-23T11%3A35%3A44Z", b"[]")
    reports.sync_reports(ctx, now=NOW)
    assert transport.requests[-1].endswith("&since=2026-11-23T11%3A35%3A44Z")


def test_a_forge_that_does_not_answer_keeps_its_mark_and_the_other_goes_on():
    transport = FakeTransport()           # every URL a 404
    gh = ReportsGH([item(1)])
    ctx = context(gh, transport=transport, forgejo=FORGEJO)
    out = reports.sync_reports(ctx, now=NOW)
    assert any(line.startswith(f"reports: the Forgejo listing of {FORGEJO} failed") for line in out)
    assert len(ctx.board.created) == 1 and "forgejo" not in gh.register()["scanned"]


def test_the_configuration_is_checked():
    from tauceti_roadmap_worker import config
    text = (config.paths.HERE / "tauceti-roadmap.toml.example").read_text(encoding="utf-8")
    def with_(old: str, new: str) -> str:
        assert text.count(old) == 1
        return text.replace(old, new)
    with pytest.raises(config.ConfigError, match="reports.reporters"):
        config.parse(with_("\nreporters = []\n", '\nreporters = ["team:Org"]\n'))
    with pytest.raises(config.ConfigError, match="reports.forgejo must"):
        config.parse(with_('\nforgejo = ""', '\nforgejo = "http://git.example.org"'))
    cfg = config.parse(with_("\nreporters = []\n", '\nreporters = ["team:TauCetiProject/workers", "bot-1"]\n')
                       .replace("enabled = false\n# The library on GitHub", "enabled = true\n# The library on GitHub"))
    assert cfg.reports.enabled and cfg.reports.per_run == 5 and cfg.reports.per_author == 2


# --- a kind no lane takes ----------------------------------------------------------------------------------

def test_report_job_ids_titles_and_issue_text():
    job = new_job("REPORT-QuadraticFormInvariants~1", inputs={"roadmap": "QuadraticFormInvariants"})
    assert job.kind == "report" and board.make_id("report", "Foo") == "REPORT-Foo~1"
    assert board.title_for(job) == "[Report] QuadraticFormInvariants (round 1)"
    assert board.stage_of(job.id) == "REPORT" and "report" in board.NO_LANE
    body = board.issue_body(job, "o/board", "packages", preamble="text")
    assert "No worker lane takes `report` jobs yet" in body and "work --only" not in body
    assert board.decode_spec(body).id == job.id
    assert reports.job_name("RepresentationTheory/compactGroups") == "CompactGroups"


def test_a_worker_never_takes_a_report_job(tmp_path, monkeypatch):
    monkeypatch.setenv("TAUCETI_PROJECT_STATE", str(tmp_path / "state"))
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "REPORT-Foo~1", "available", inputs={"roadmap": "Foo"})])
    ns = FakeClaims()
    ctx = Context(cfg=cfg, gh=gh, board=Board(cfg, gh), claims=ns, worker_id="w1", operator="alice", agent="codex",
                  model="m")
    monkeypatch.setattr(worker, "lane_for", lambda job: pytest.fail(f"a lane was asked for {job.id}"))
    assert worker.work_once(ctx, None, log=lambda line: None).status == "idle"
    assert worker.work_once(ctx, None, job_id="REPORT-Foo~1", log=lambda line: None).status == "idle"
    assert ns.acquired == [] and gh.writes == [] and gh.states(1) == ["available"]


def test_work_only_and_overflow_refuse_the_report_kind(monkeypatch):
    monkeypatch.setattr(cli.GH, "login", lambda self: "alice")
    with pytest.raises(SystemExit, match="no lane takes report jobs"):
        cli.main(["--conf", str(cli.paths.HERE / "tauceti-roadmap.toml.example"), "--dry-run", "work", "--worker-id",
                  "w", "--only", "report"])
    with pytest.raises(SystemExit, match="unknown kind 'report'"):
        cli._overflow("report=30")


def test_status_lists_report_jobs_and_resume_ignores_them():
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "REPORT-Foo~1", "available"), job_issue(2, "PKG-Foo", "done")])
    issues = gh.api("repos/o/board/issues?labels=roadmap-job&state=open")
    text = watch.status(Board(cfg, gh), [i | {"updated_at": "2026-11-24T10:00:00+00:00"} for i in issues], {}, "P",
                        at=NOW)
    assert "available (1)" in text and "REPORT-Foo~1" in text
    jobs = Board(cfg, gh).all_jobs()
    # A REPORT job of the roadmap neither holds its resumption nor is taken for its last job.
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh))
    assert "REPORT" not in intake.resume_roadmap(ctx, "Foo", jobs, yes=False)


def test_the_intake_takes_no_submission_for_a_report_job(monkeypatch):
    cfg = example_config()
    gh = FakeIssuesGH([job_issue(1, "REPORT-Foo~1", "available")])
    ctx = SimpleNamespace(cfg=cfg, gh=gh, board=Board(cfg, gh))
    for name, value in (("admission_problems", lambda *a, **k: []), ("submission_files", lambda *a, **k: ([], [])),
                        ("deliverable_problems", lambda *a, **k: []), ("_checks", lambda *a, **k: ([], False))):
        monkeypatch.setattr(intake, name, value)
    pr = {"number": 5, "state": "open", "draft": False, "labels": [], "user": {"login": "alice"},
          "head": {"sha": "b" * 40, "repo": {"full_name": "alice/board"}},
          "body": "Refs #1\n\n" + board.encode_submission("REPORT-Foo~1", "w1", "alice", "codex")}
    out = intake.intake(ctx, 5, yes=True, pr=pr)
    assert out == "#5: not merged: REPORT-Foo~1 is a report job, which no lane takes and no submission answers"


def test_a_search_with_more_than_its_pages_narrows_its_window():
    stamps = [f"2026-11-2{1 + k // 200}T{(k // 60) % 24:02d}:{k % 60:02d}:00Z" for k in range(350)]
    gh = ReportsGH([item(k, body="no marker here", updated=stamps[k]) for k in range(350)])
    transport = FakeTransport()
    base = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues?type=pulls&state=all&limit=50"
    page = [item(k, body="no marker", base=f"{FORGEJO}/TauCetiProject/TauCeti") for k in range(50)]
    for n in (1, 2, 3):
        transport.add(f"{base}&page={n}&since=2026-10-31T23%3A55%3A00Z", json.dumps(page).encode())
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "roadmaps": {},
                                             "scanned": {"forgejo": "2026-11-01T00:00:00Z"}})
    ctx = context(gh, transport=transport, forgejo=FORGEJO)
    out = reports.sync_reports(ctx, now=NOW)
    assert len(gh.queries) == 3 and len(transport.requests) == 3
    assert ("reports: more github results between the start and 2026-11-24T11:55:00Z than 3 pages; the next run reads "
            "them to 2013-06-13T05:57:30Z") in out
    assert ("reports: more forgejo results between 2026-10-31T23:55:00Z and 2026-11-24T11:55:00Z than 3 pages; the "
            "next run reads them to 2026-11-12T17:55:00Z") in out
    scanned = gh.register()["scanned"]
    assert scanned == {"forgejo": "2026-11-01T00:00:00Z", "forgejo-until": "2026-11-12T17:55:00Z",
                       "github-until": "2013-06-13T05:57:30Z"}
    # The next Forgejo listing asks for the earlier half only.
    transport.add(f"{base}&page=1&since=2026-10-31T23%3A55%3A00Z&before=2026-11-12T17%3A55%3A00Z", b"[]")
    reports.sync_reports(ctx, now=NOW)
    scanned = gh.register()["scanned"]
    assert scanned["forgejo"] == "2026-11-12T17:55:00Z" and scanned["forgejo-exact"] is True
    assert "forgejo-until" not in scanned


def test_a_window_of_one_second_is_passed_with_a_warning():
    marks = {"github": "2026-11-24T10:00:00Z", "github-exact": True, "github-until": "2026-11-24T10:00:01Z"}
    line = reports.next_marks(marks, "github", [], True, "2026-11-24T12:00:00Z")
    assert line == ("reports: more github results updated within one second (2026-11-24T10:00:01Z) than 3 pages; "
                    "some may be missed")
    assert marks == {"github": "2026-11-24T10:00:01Z", "github-exact": True}


def test_the_origin_upkeep_leaves_report_jobs_alone():
    target = new_job("TARGET-Foo", inputs={"origin": "target:3"})
    target.number, target.state = 3, "done"
    report = new_job("REPORT-Foo~1", inputs={"roadmap": "Foo"})
    report.number, report.state = 9, "available"
    design = new_job("DESIGN-Foo")
    design.number, design.state = 4, "available"
    fb = FakeBoard(jobs=[target, report, design])
    assert intake.sync_origins(SimpleNamespace(cfg=example_config(), board=fb)) == ["DESIGN-Foo: origin target:3"]
    assert [jid for jid, _spec in fb.specs] == ["DESIGN-Foo"]


# --- review findings: evidence revisions, recovery, closing, dense windows -----------------------------------

def test_a_corrected_source_replaces_its_evidence_and_its_open_job_says_so():
    first = item(1, body=marker(refutes=["TauCeti.wrong_counterexample"]), updated="2026-11-24T10:00:00Z")
    gh = ReportsGH([first])
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    [(job, _s, _p)] = ctx.board.created
    job.number, job.state = 501, "available"
    ctx.board.jobs = [job]
    revised = item(1, body=marker(refutes=["TauCeti.right_counterexample"], kind="missing-hypothesis"),
                   updated="2026-11-24T11:00:00Z")
    gh.search = [revised]
    out = reports.sync_reports(ctx, now=NOW)
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["refutes"] == ["TauCeti.right_counterexample"] and entry["kind"] == "missing-hypothesis"
    assert entry["evidence"][f"{LIBRARY}/pull/1"]["refutes"] == ["TauCeti.right_counterexample"]
    assert job.inputs["refutes"] == ["TauCeti.right_counterexample"] and job.inputs["kind"] == "missing-hypothesis"
    assert ctx.board.specs[-1][0] == job.id
    assert ctx.board.posted[-1][0] == 501 and "Revised in https://github.com/TauCetiProject/TauCeti/pull/1" in \
        ctx.board.posted[-1][1]
    assert any("evidence revised" in line for line in out)
    # Another source keeps its own evidence: the names are the union of what each source says now.
    gh.search = [item(2, body=marker(replaces=["TauCeti.other"]), updated="2026-11-24T11:30:00Z")]
    reports.sync_reports(ctx, now=NOW)
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["refutes"] == ["TauCeti.right_counterexample", "TauCeti.exists_rankOne_equivalent_not_diagonalChain"]


def test_new_evidence_for_a_rejected_report_is_posted_on_its_closed_job():
    gh = ReportsGH([item(2, updated="2026-11-24T11:00:00Z")])
    entry = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "rejected",
                              f"{LIBRARY}/pull/1")
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    job = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="done",
              closed=True)
    ctx = context(gh, jobs=[job])
    reports.sync_reports(ctx, now=NOW)
    [(number, text)] = ctx.board.posted
    assert number == 40 and "rejected" in text and "reopen" in text


def test_a_recurrence_filed_before_a_failed_register_write_is_not_filed_again():
    gh = ReportsGH([item(9, updated="2026-11-24T11:00:00Z")])
    entry = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    old = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="done",
              closed=True)
    ctx = context(gh, jobs=[old])
    write = gh.write
    gh.write = lambda m, p, payload=None: (_ for _ in ()).throw(GHError("PUT: HTTP 502"))
    with pytest.raises(GHError):
        reports.sync_reports(ctx, now=NOW)             # round 2 is filed, the register write fails
    [(round2, _s, _p)] = ctx.board.created
    round2.number, round2.state = 41, "available"
    ctx.board.jobs.append(round2)
    gh.write = write
    reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~2"]
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["job"] == "REPORT-QuadraticFormInvariants~2" and entry["state"] == "open"
    assert entry["sources"] == [f"{LIBRARY}/pull/1", f"{LIBRARY}/pull/9"]


def test_a_failed_close_is_retried_while_the_job_is_done_but_open():
    gh = ReportsGH([], main={README: "corrected"})
    entry = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "open",
                              f"{LIBRARY}/pull/1")
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    job = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="available")
    ctx = context(gh, jobs=[job])
    write, fails = gh.write, [1]

    def flaky(method, path, payload=None):
        if method == "PATCH" and fails:
            fails.pop()
            raise GHError("PATCH: HTTP 502")
        return write(method, path, payload)
    gh.write = flaky
    with pytest.raises(GHError):
        reports.sync_reports(ctx, now=NOW)
    assert job.state == "done" and not job.closed       # the label went on, the close failed
    reports.sync_reports(ctx, now=NOW)
    closes = [w for w in gh.writes if w[0] == "PATCH"]
    assert closes == [("PATCH", "repos/TauCetiProject/TauCetiRoadmapJobs/issues/40", {"state": "closed"})]
    assert gh.register()["roadmaps"]["QuadraticFormInvariants"][0]["state"] == "fixed"


class WindowGH(ReportsGH):
    """GitHub's issue search as it filters and orders: `updated:>=A` or `updated:A..B`, oldest first."""

    def api(self, path, paginate=False):
        if not path.startswith("search/issues?"):
            return super().api(path, paginate)
        q = parse_qs(urlsplit(path).query)
        self.queries.append(q["q"][0])
        page = int(q["page"][0])
        lo, hi = "", "9999"
        m = re.search(r"updated:(>=)?(\S+?)(?:\.\.(\S+))?(?:\s|$)", q["q"][0])
        if m:
            lo, hi = m.group(2), m.group(3) or "9999"
        hits = sorted((i for i in self.search if lo <= i["updated_at"] <= hi), key=lambda i: i["updated_at"])
        return {"total_count": len(hits), "items": copy.deepcopy(hits[(page - 1) * 100:page * 100])}


def test_a_dense_lookback_window_cannot_hold_the_mark_for_ever():
    old = [item(k, body="a pull request with no marker", updated=f"2026-11-24T10:0{k // 60}:{k % 60:02d}Z")
           for k in range(300)]
    new = item(999, updated="2026-11-24T10:05:00Z")
    gh = WindowGH(old + [new])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T10:04:59Z"},
                                             "roadmaps": {}})
    ctx = context(gh)
    for _run in range(12):
        reports.sync_reports(ctx, now=NOW)
        if ctx.board.created:
            break
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/999"]]
    assert len(gh.queries) <= 40


def test_a_bors_title_counts_as_merged_only_when_the_queue_closed_it():
    gh = ReportsGH([item(1, login="stranger"), item(2, login="stranger", body=marker(anchor="diagonal chains")),
                    item(3, login="stranger", body=marker(anchor="rank one are equivalent"))])
    gh.closers = {2: "stranger", 3: None}                     # 2 closed by its author, 3 unreadable now
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/1"]]
    assert (f"reports: {LIBRARY}/pull/2: titled as a bors merge, but not closed by a merge-queue account ([reports] "
            "merge_bots); read as a pull request closed unmerged") in out
    assert f"reports: {LIBRARY}/pull/2: a pull request closed unmerged; not harvested" in out
    assert any(line.startswith(f"reports: {LIBRARY}/pull/3: titled as a bors merge; who closed it could not be read")
               for line in out)
    assert gh.register()["merges"] == {f"{LIBRARY}/pull/1": "tauceti-queue[bot]"}
    assert gh.register()["retry"] == [{"url": f"{LIBRARY}/pull/3", "forge": "github"}]     # queued for later
    # A verified merge is read once, and so is a refusal for the revision it read.
    reads = len(gh.closer_reads)
    gh.closers = {2: "stranger"}
    reports.sync_reports(ctx, now=NOW)
    assert sorted(gh.closer_reads[reads:]) == [3]


# --- the field rules shared with the worker's checker and the review runner (SPEC "Exact field rules") -------
# The cases mirror TauCetiReview's tests/test_roadmap_reports.py (Form.INVALID and Form.STRICTER), so that
# the three readers give the same verdict on each: whatever the worker and the review call valid, the board
# harvests.

SHARED_INVALID = [
    {"roadmap": None}, {"roadmap": "../Secrets"}, {"roadmap": "Representation Theory"}, {"roadmap": "Quadratic-Forms"},
    {"roadmap": "1Forms"}, {"file": "STATUS.md"}, {"roadmap_sha": "abc"}, {"roadmap_sha": "abc123"},
    {"anchor": "x" * 301}, {"anchor": "  "}, {"kind": "typo"}, {"effect": "fixed"}, {"refutes": "TauCeti.x"},
    {"refutes": [f"TauCeti.n{i}" for i in range(11)]}, {"replaces": [f"TauCeti.x{i}" for i in range(11)]},
    {"replaces": ["two words"]}, {"replaces": ["TauCeti.x y"]}, {"refutes": ["`TauCeti.x`"]}, {"refutes": [3]},
    {"replaces": ["TauCeti." + "x" * 200]},
    # once accepted here by trimming or case-folding first; refused by all three now
    {"roadmap": " QuadraticFormInvariants "}, {"roadmap": "QuadraticFormInvariants/"}, {"roadmap_sha": "A" * 40},
    {"roadmap_sha": " " + SHA}, {"anchor": "x " + " " * 150 + "y" * 150},
    {"refutes": ["TauCeti..x"]}, {"refutes": ["TauCeti.x."]}, {"refutes": ["TauCeti.<x>"]}, {"replaces": ["@TauCeti.x"]},
]
SHARED_VALID = [{}, {"refutes": None, "replaces": None, "future": 1}, {"roadmap": "RepresentationTheory/CompactGroups"},
                {"file": "Suggested.lean", "anchor": "theorem foo {R} : \"}\""}]


@pytest.mark.parametrize("fields", SHARED_INVALID, ids=lambda f: json.dumps(f)[:40])
def test_the_shared_rules_refuse(fields):
    found, problems = reports.parse(marker(**fields) + "\n" + marker(anchor="another"))
    assert [r.anchor for r in found] == ["another"] and len(problems) == 1 and problems[0].startswith("marker 1: ")


@pytest.mark.parametrize("fields", SHARED_VALID, ids=lambda f: json.dumps(f)[:40])
def test_the_shared_rules_accept(fields):
    assert len(reports.parse(marker(**fields))[0]) == 1


def test_a_deeply_nested_marker_is_invalid_not_a_crash():
    nested = reports.MARKER + "[" * 100000 + "]" * 100000 + "-->"
    found, problems = reports.parse(nested + "\n" + marker())
    assert len(found) == 1 and problems == ["marker 1: not valid JSON"]
    # And a body carrying it costs the sync nothing else: the other sources are harvested.
    gh = ReportsGH([item(1, body=nested), item(2, body=marker(anchor="diagonal chains"))])
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/2"]]
    assert f"reports: {LIBRARY}/pull/1: marker 1: not valid JSON; not read" in out


def test_a_body_the_reader_cannot_read_is_logged_and_passed(monkeypatch):
    real = reports.parse
    monkeypatch.setattr(reports, "parse", lambda body: (_ for _ in ()).throw(MemoryError()) if "boom" in body
                        else real(body))
    gh = ReportsGH([item(1, body="boom " + marker()), item(2, body=marker(anchor="diagonal chains"))])
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/2"]]
    assert f"reports: {LIBRARY}/pull/1: its body could not be read (MemoryError); passed" in out



# --- review round 2 ----------------------------------------------------------------------------------------------

def test_negative_merge_checks_are_kept_and_cannot_starve_a_later_report():
    fakes = [item(k, login="stranger", body=marker(anchor=f"Two forms {k}"), updated=f"2026-11-24T10:00:{k:02d}Z")
             for k in range(20)]
    genuine = item(21, login="stranger", body=marker(anchor="diagonal chains"), updated="2026-11-24T10:01:00Z")
    gh = WindowGH(fakes + [genuine])
    gh.closers = {k: "stranger" for k in range(20)}
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T10:00:00Z"},
                                             "roadmaps": {}})
    ctx = context(gh)
    for _run in range(6):
        reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/21"]]
    assert sorted(gh.closer_reads).count(0) == 1                 # a decided closer is read once per revision


def test_a_source_left_for_later_is_queued_and_never_holds_the_mark():
    stuck = item(3, login="stranger", body=marker(anchor="diagonal chains"), updated="2026-11-24T10:00:00Z")
    gh = WindowGH([stuck, item(4, body=marker(anchor="rank one"), updated="2026-11-24T10:30:00Z")])
    gh.closers = {3: None}                                       # its closer cannot be read
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {}})
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    register = gh.register()
    assert register["scanned"]["github"] == "2026-11-24T10:30:00Z"        # the mark moves past it
    assert register["retry"] == [{"url": f"{LIBRARY}/pull/3", "forge": "github"}]
    # Later runs read it by its number, outside every search window, until it is decided.
    gh.search = [stuck]                                           # (the fake's issue read)
    gh.closers = {}
    gh.queries.clear()
    reports.sync_reports(ctx, now=NOW)
    assert f"{LIBRARY}/pull/3" in [j.inputs["sources"][0] for j, _s, _p in ctx.board.created]
    assert gh.register()["retry"] == []


def test_a_queued_source_that_cannot_be_read_goes_to_the_back_of_the_queue():
    queue = [{"url": f"{LIBRARY}/pull/{n}", "forge": "github"} for n in range(1, 26)]
    old = [item(n, login="stranger", state="closed", title="feat: x", body="no marker",
                updated="2026-11-01T00:00:00Z") for n in range(1, 26)]     # outside the search window
    gh = WindowGH(old)
    gh.closers = {1: None}                                        # the first cannot be read now
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T11:59:00Z"},
                                             "roadmaps": {}, "retry": queue})
    reports.sync_reports(context(gh), now=NOW)
    assert gh.closer_reads == list(range(1, 21))                  # at most twenty a run, oldest first
    assert [q["url"].rsplit("/", 1)[1] for q in gh.register()["retry"]] == [str(n) for n in range(21, 26)] + ["1"]


def test_an_aggregated_report_beyond_one_markers_limits_stays_a_valid_job():
    six = [f"TauCeti.a{i}" for i in range(6)]
    other = [f"TauCeti.b{i}" for i in range(6)]
    gh = ReportsGH([item(1, body=marker(refutes=six)), item(2, body=marker(refutes=other),
                                                             updated="2026-11-24T10:05:00Z")])
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    [(job, _s, _p)] = ctx.board.created
    job.number, job.state = 501, "available"
    ctx.board.jobs = [job]
    gh.search = [item(2, body=marker(refutes=other), updated="2026-11-24T10:06:00Z")]
    reports.sync_reports(ctx, now=NOW)
    assert len(job.inputs["refutes"]) == 12
    report = reports.report_of(job)
    assert report is not None and len(report.refutes) == 12


def test_a_job_note_is_posted_once_even_when_the_register_write_fails():
    gh = ReportsGH([item(1)])
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    [(job, _s, _p)] = ctx.board.created
    job.number, job.state = 501, "available"
    ctx.board.jobs = [job]
    gh.search = [item(2, body=marker(), updated="2026-11-24T11:00:00Z")]
    write = gh.write
    gh.write = lambda m, p, payload=None: (_ for _ in ()).throw(GHError("PUT: HTTP 502"))
    with pytest.raises(GHError):
        reports.sync_reports(ctx, now=NOW)
    gh.write = write
    reports.sync_reports(ctx, now=NOW)
    notes = [t for n, t in ctx.board.posted if n == 501 and t.startswith("Reported again in")]
    assert notes == [f"Reported again in {LIBRARY}/pull/2 (a merged pull request)."]


def test_an_open_jobs_spec_is_rewritten_from_the_register():
    gh = ReportsGH([])
    entry = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "open",
                              f"{LIBRARY}/pull/1")
    entry["refutes"], entry["evidence"][f"{LIBRARY}/pull/1"]["refutes"] = ["TauCeti.now"], ["TauCeti.now"]
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    report = reports.parse(marker())[0][0]
    job = new_job("REPORT-QuadraticFormInvariants~1", inputs={
        "roadmap": report.roadmap, "file": report.file, "roadmap_sha": report.roadmap_sha, "anchor": report.anchor,
        "kind": report.kind, "effect": report.effect, "refutes": ["TauCeti.then"], "replaces": [],
        "sources": [f"{LIBRARY}/pull/1"]})
    job.number, job.state = 12, "available"
    ctx = context(gh, jobs=[job])
    out = reports.sync_reports(ctx, now=NOW)
    assert job.inputs["refutes"] == ["TauCeti.now"] and ctx.board.specs[-1][0] == job.id
    assert f"reports: {job.id}: spec rewritten from the register (refutes, replaces)" in out



def test_queued_sources_are_served_before_newer_ones():
    old = item(3, body=marker(anchor="iff their"), updated="2026-11-01T00:00:00Z")     # outside the window
    anchors = ["Two forms", "rank one", "are equivalent", "diagonal", "chains are"]
    new = [item(10 + k, body=marker(anchor=a), updated=f"2026-11-24T11:0{k}:00Z") for k, a in enumerate(anchors)]
    gh = WindowGH([old] + new)
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T10:59:00Z"},
                                             "roadmaps": {}, "retry": [{"url": f"{LIBRARY}/pull/3", "forge": "github"}]})
    ctx = context(gh, per_run=1, per_author=5)
    reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{LIBRARY}/pull/3"]]


# --- review round 3: partial reads are never complete answers -------------------------------------------------

def test_an_incomplete_github_search_never_moves_the_mark():
    early = item(3, body=marker(anchor="diagonal chains"), updated="2026-11-24T10:00:00Z")
    late = item(4, body=marker(anchor="rank one"), updated="2026-11-24T10:30:00Z")

    class Incomplete(WindowGH):
        partial = True

        def api(self, path, paginate=False):
            got = super().api(path, paginate)
            if path.startswith("search/issues?") and self.partial:
                self.partial = False
                return {**got, "items": [i for i in got["items"] if i["number"] == 4], "incomplete_results": True}
            return got
    gh = Incomplete([early, late])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {}})
    ctx = context(gh)
    out = reports.sync_reports(ctx, now=NOW)
    assert any("incomplete" in line for line in out) and ctx.board.created == []
    assert gh.register()["scanned"]["github"] == "2026-11-24T09:00:00Z"
    reports.sync_reports(ctx, now=NOW)
    assert sorted(j.inputs["sources"][0] for j, _s, _p in ctx.board.created) == [f"{LIBRARY}/pull/3",
                                                                               f"{LIBRARY}/pull/4"]


def test_a_register_that_cannot_be_read_stops_the_step():
    class Down(ReportsGH):
        def api(self, path, paginate=False):
            if path.endswith("/contents/" + reports.REGISTER):
                raise GHError(f"GET {path}: HTTP 502")
            return super().api(path, paginate)
    gh = Down([item(1)])
    ctx = context(gh)
    with pytest.raises(GHError):
        reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == [] and gh.writes == []


@pytest.mark.parametrize("body, headers", [
    (json.dumps({"message": "upstream error"}).encode(), {}),           # an error page that parses as JSON
    (json.dumps([item(1, base=f"{FORGEJO}/TauCetiProject/TauCeti")]).encode(), {"X-Total-Count": "7"}),   # short
], ids=["an object, not a list", "fewer items than its total"])
def test_a_forgejo_listing_that_is_not_whole_is_a_failure(body, headers):
    transport = FakeTransport()
    base = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues?type=pulls&state=all&limit=50"
    transport.add(f"{base}&page=1&since=2026-11-24T08%3A55%3A00Z", body, headers=headers)
    gh = ReportsGH([])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "roadmaps": {},
                                             "scanned": {"github": "2026-11-24T09:00:00Z", "forgejo": "2026-11-24T09:00:00Z"}})
    out = reports.sync_reports(context(gh, transport=transport, forgejo=FORGEJO), now=NOW)
    assert transport.requests[0] == f"{base}&page=1&since=2026-11-24T08%3A55%3A00Z"
    assert any(line.startswith(f"reports: the Forgejo listing of {FORGEJO} failed") and "HTTP" not in line
               for line in out)
    assert gh.register()["scanned"]["forgejo"] == "2026-11-24T09:00:00Z"


def test_a_closer_beyond_the_pages_read_is_unknown_not_refused():
    transport = FakeTransport()
    page = json.dumps([{"type": "comment", "user": {"login": "x"}}] * 50).encode()
    for n in range(1, 11):
        transport.add(f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues/7/timeline?limit=50&page={n}", page)
    ctx = context(ReportsGH([]), transport=transport, forgejo=FORGEJO)
    source = reports.Source("forgejo", f"{FORGEJO}/TauCetiProject/TauCeti/pulls/7", "a", False, False, "t", bors=True)
    assert reports.closer(ctx, source) is None


def test_a_queued_source_that_cannot_be_read_is_never_gone():
    """Pull request 404 failing for another reason than a 404, and answers that are no issue, are unknown."""
    class Flaky(ReportsGH):
        answer = None

        def api(self, path, paginate=False):
            if path == "repos/TauCetiProject/TauCeti/issues/404":
                if self.answer is None:
                    raise GHError(f"GET {path}: HTTP 502")
                return self.answer
            return super().api(path, paginate)
    gh = Flaky([])
    ctx = context(gh)
    url = f"{LIBRARY}/pull/404"
    assert reports.fetch_source(ctx, reports.GITHUB, url) is None
    gh.answer = {"message": "Server Error"}
    assert reports.fetch_source(ctx, reports.GITHUB, url) is None
    transport = FakeTransport()
    transport.add(f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues/7", json.dumps({"message": "x"}).encode())
    ctx = context(ReportsGH([]), transport=transport, forgejo=FORGEJO)
    assert reports.fetch_source(ctx, reports.FORGEJO, f"{FORGEJO}/TauCetiProject/TauCeti/pulls/7") is None
