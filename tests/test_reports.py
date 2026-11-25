"""Roadmap reports: the marker, the harvest from the library on GitHub and Forgejo, the REPORT jobs and
the register `board sync` keeps (reports.py), and the safety of a job kind no lane takes."""

import base64
import copy
import datetime as dt
import hashlib
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tauceti_roadmap_worker import board, intake, prbatch, reports, watch
from tauceti_roadmap_worker.board import Board, Job, new_job
from tauceti_roadmap_worker.gh import GHError

from .fakes import FakeBoard, FakeIssuesGH, FakeTransport, example_config, job_issue

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
    assert r.anchor == "Two forms of rank one" and r.identity[2] == "Two forms of rank one"
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


def blob_id(text: str) -> str:
    """The git blob id of a file whose bytes are `text` in UTF-8, as GitHub's `oid` and `git rev-parse` give it."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


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
            out[alias] = None if text is None else {"text": text, "isTruncated": False, "oid": blob_id(text)}
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
                         "replaces": ["TauCeti.diagonalChain_iff_equivalent_of_two_le"], "roadmap_sha": SHA}}}
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
    other = marker(anchor="Two forms of rank one  are\n equivalent iff their diagonal chains are")     # spacing only
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


def forgejo_item(number, login, title="[Merged by Bors] - feat: x", merged=False, state="closed", **kw):
    got = item(number, base=f"{FORGEJO}/TauCetiProject/TauCeti", login=login, title=title, state=state, **kw)
    got["pull_request"] = {"merged": merged, "merged_at": None, "html_url": got["html_url"]}
    return got


def test_forgejo_pull_requests_count_only_from_listed_reporters_whatever_their_title():
    """No Forgejo merge is verified (the instance has no queue account): a Forgejo pull request counts when its
    author is a listed Forgejo reporter, open, merged or bors-titled, and never otherwise; no timeline is read."""
    transport = FakeTransport()
    base = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues?type=pulls&state=all&limit=50"
    listed = [forgejo_item(3668, "kim", updated="2026-11-23T11:40:44Z"),
              forgejo_item(3700, "kim", title="feat: y", state="open", body=marker(anchor="diagonal chains"),
                           updated="2026-11-23T11:00:00Z"),
              forgejo_item(3701, "stranger", body=marker(anchor="rank one"), updated="2026-11-23T10:00:00Z"),
              forgejo_item(3702, "stranger", title="feat: z", merged=True, body=marker(anchor="Two forms"),
                           updated="2026-11-23T10:00:00Z"),
              forgejo_item(3703, "kim", title="feat: w", body=marker(anchor="equivalent"),      # closed unmerged
                           updated="2026-11-23T10:00:00Z")]
    transport.add(f"{base}&page=1", json.dumps(listed).encode())
    gh = ReportsGH([])
    ctx = context(gh, transport=transport, forgejo=FORGEJO, forgejo_reporters=["Kim"])
    out = reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [
        [f"{FORGEJO}/TauCetiProject/TauCeti/pulls/3700"], [f"{FORGEJO}/TauCetiProject/TauCeti/pulls/3668"]]
    assert not [u for u in transport.requests if "/timeline" in u]
    assert any("pulls/3702: a merged pull request of @stranger, who is not a listed Forgejo reporter" in x for x in out)
    assert any("pulls/3703: a pull request closed unmerged; not harvested" in x for x in out)
    assert gh.register()["scanned"]["forgejo"] == "2026-11-23T11:40:44Z" and "2 filed" in out[-2]
    # The next listing asks for what changed since the mark, less the margin.
    transport.add(f"{base}&page=1&since=2026-11-23T11%3A35%3A44Z", b"[]")
    reports.sync_reports(ctx, now=NOW)
    assert transport.requests[-1].endswith("&since=2026-11-23T11%3A35%3A44Z")


def test_a_long_forgejo_timeline_never_holds_a_listed_reporters_report():
    """(v) A bors-titled Forgejo pull request with 151 timeline events was deferred on every sync (each read
    pages 1-3 again). Nothing reads the timeline now: the listed reporter's report is filed on the first sync."""
    transport = FakeTransport()
    base = f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues?type=pulls&state=all&limit=50"
    transport.add(f"{base}&page=1", json.dumps([forgejo_item(7, "kim")]).encode())
    for n, events in enumerate(([{"type": "comment", "user": {"login": "x"}}] * 50,) * 3
                               + ([{"type": "close", "user": {"login": "queue"}}],), start=1):
        transport.add(f"{FORGEJO}/api/v1/repos/TauCetiProject/TauCeti/issues/7/timeline?limit=50&page={n}",
                      json.dumps(events).encode())
    gh = ReportsGH([])
    ctx = context(gh, transport=transport, forgejo=FORGEJO, forgejo_reporters=["kim"])
    reports.sync_reports(ctx, now=NOW)
    assert [j.inputs["sources"] for j, _s, _p in ctx.board.created] == [[f"{FORGEJO}/TauCetiProject/TauCeti/pulls/7"]]
    assert gh.register().get("retry", []) == []


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


# --- the job kind ------------------------------------------------------------------------------------------

def test_report_job_ids_titles_and_issue_text():
    job = new_job("REPORT-QuadraticFormInvariants~1", inputs={"roadmap": "QuadraticFormInvariants"})
    assert job.kind == "report" and board.make_id("report", "Foo") == "REPORT-Foo~1"
    assert board.title_for(job) == "[Report] QuadraticFormInvariants (round 1)"
    assert board.stage_of(job.id) == "REPORT"
    body = board.issue_body(job, "o/board", "packages", preamble="text")
    assert "tauceti-roadmap work --only report" in body
    assert board.decode_spec(body).id == job.id
    assert reports.job_name("RepresentationTheory/compactGroups") == "CompactGroups"


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
    assert number == 40 and text.endswith(reports.ASK_AGAIN) and "triage again" in text


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
    assert f"reports: {job.id}: spec rewritten from the register (refutes, replaces, evidence)" in out



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


# --- review round 6 -----------------------------------------------------------------------------------------------

def test_recovering_a_round_keeps_each_sources_own_evidence():
    """(ee) A and B were recorded with their own counterexamples; C files round 2, whose register write fails. The
    recovery from round 2's spec must not give B (or anyone) the union of the declarations: when A later
    withdraws its counterexample, it leaves the report, though B stays out of every search window."""
    def says(n, witness, updated="2026-11-24T10:00:00Z"):
        return item(n, body=marker(refutes=[f"TauCeti.{witness}"]), updated=updated)
    a, b = reports.parse(marker(refutes=["TauCeti.wrongWitness"]))[0][0], \
        reports.parse(marker(refutes=["TauCeti.otherWitness"]))[0][0]
    entry = reports.new_entry(a, "REPORT-QuadraticFormInvariants~1", "fixed", f"{LIBRARY}/pull/1")
    reports.revise(entry, f"{LIBRARY}/pull/2", b)
    gh = ReportsGH([says(9, "newWitness", "2026-11-24T11:00:00Z")])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    old = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="done",
              closed=True)
    ctx = context(gh, jobs=[old])
    write = gh.write
    gh.write = lambda m, p, payload=None: (_ for _ in ()).throw(GHError("PUT: HTTP 502")) \
        if p.endswith(reports.REGISTER) else write(m, p, payload)
    with pytest.raises(GHError):
        reports.sync_reports(ctx, now=NOW)                  # round 2 filed (and its spec refreshed); no register
    [(round2, _s, _p)] = ctx.board.created
    round2.number, round2.state = 41, "available"
    ctx.board.jobs.append(round2)
    gh.write = write
    gh.search = [says(1, "correctWitness", "2026-11-24T12:00:00Z")]    # A corrects itself; B and C out of window
    reports.sync_reports(ctx, now=NOW)
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert e["job"] == "REPORT-QuadraticFormInvariants~2"
    assert e["evidence"][f"{LIBRARY}/pull/2"]["refutes"] == ["TauCeti.otherWitness"]
    assert e["evidence"][f"{LIBRARY}/pull/9"]["refutes"] == ["TauCeti.newWitness"]
    assert "TauCeti.wrongWitness" not in e["refutes"] and "TauCeti.correctWitness" in e["refutes"]
    assert "TauCeti.wrongWitness" not in round2.inputs["refutes"]


def test_a_recovered_source_with_no_recorded_evidence_is_read_again():
    """(ee) A job whose spec records no source's own evidence (filed before it did): its sources are queued to be
    read again, never credited with the job's union of declarations."""
    report = reports.parse(marker())[0][0]
    job = new_job("REPORT-QuadraticFormInvariants~1", inputs={
        "roadmap": report.roadmap, "file": report.file, "roadmap_sha": report.roadmap_sha, "anchor": report.anchor,
        "kind": report.kind, "effect": report.effect, "refutes": list(report.refutes), "replaces": [],
        "sources": [f"{LIBRARY}/pull/1", f"{LIBRARY}/pull/2"]})
    job.number, job.state = 12, "available"
    gh = ReportsGH([])
    ctx = context(gh, jobs=[job])
    reports.sync_reports(ctx, now=NOW)
    [entry] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert entry["evidence"] == {} and entry["refutes"] == list(report.refutes)
    assert sorted(gh.closer_reads) == [1, 2]                 # queued, and read again by number in the same run


# --- review round 7 -----------------------------------------------------------------------------------------------

def test_a_fixed_report_its_own_source_reports_again_is_filed_as_the_next_round():
    """(gg) Round 1 came from #1, and its anchor left main (fixed). The anchor is back, and #1's author edits its
    marker to report it again (another kind and witness): that is the report said again, so round 2 is filed, as it
    is when a new pull request says it."""
    fixed = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1, body=marker(kind="missing-hypothesis", refutes=["TauCeti.newWitness"]))])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [fixed]}})
    old = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="done",
              closed=True)
    ctx = context(gh, jobs=[old])
    reports.sync_reports(ctx, now=NOW)
    [(job, state, _p)] = ctx.board.created
    assert job.id == "REPORT-QuadraticFormInvariants~2" and state == "available"
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert e["state"] == "open" and e["job"] == job.id and e["kind"] == "missing-hypothesis"
    assert e["sources"] == [f"{LIBRARY}/pull/1"] and e["evidence"][f"{LIBRARY}/pull/1"]["refutes"] == ["TauCeti.newWitness"]


def _said(state, said):
    """(entry or None, url, report) for a cell of `decide`'s table. `revised` is a changed field; `reread` is the
    same claim reported at another snapshot of the roadmap (only `roadmap_sha` changed), a revision too."""
    first = reports.parse(marker())[0][0]
    other = {"revised": reports.parse(marker(kind="missing-hypothesis"))[0][0],
             "reread": reports.parse(marker(roadmap_sha="b" * 40))[0][0]}
    if state is None:
        return None, f"{LIBRARY}/pull/1", first
    entry = reports.new_entry(first, "REPORT-QuadraticFormInvariants~1", state, f"{LIBRARY}/pull/1")
    return entry, f"{LIBRARY}/pull/{2 if said == 'new' else 1}", other.get(said, first)


@pytest.mark.parametrize("state, said, place, then, action", [
    *[(st, sd, pl, None, act) for st in ("open", "confirmed", "rejected")
      for sd, act in (("new", "add"), ("same", "nothing"), ("revised", "revise"))
      for pl in (reports.UNREAD, "active", "absent", None)],
    *[("fixed", "same", pl, None, "nothing") for pl in (reports.UNREAD, "active", "absent", None)],
    *[(st, sd, reports.UNREAD, None, "read main") for st, sd in ((None, "new"), ("fixed", "new"), ("fixed", "revised"))],
    *[(st, sd, None, None, "defer") for st, sd in ((None, "new"), ("fixed", "new"), ("fixed", "revised"))],
    *[(st, sd, pl, None, "file") for st, sd in ((None, "new"), ("fixed", "new"), ("fixed", "revised"))
      for pl in ("active", "completed")],
    (None, "new", "absent", "active", "record fixed"),
    ("fixed", "new", "absent", "active", "add to fixed"),
    ("fixed", "revised", "absent", "active", "add to fixed"),
    *[(st, sd, "absent", "absent", "not a report") for st, sd in ((None, "new"), ("fixed", "new"), ("fixed", "revised"))],
    *[(st, sd, "absent", None, "defer") for st, sd in ((None, "new"), ("fixed", "new"), ("fixed", "revised"))],
])
def test_one_table_decides_every_report(state, said, place, then, action):
    if said == "revised":                                 # (ii) a reread snapshot decides as a revision does
        entry, url, report = _said(state, "reread")
        assert reports.decide(entry, url, report, place, then) == action
    """(gg) Every cell of `decide`'s table that can occur."""
    entry, url, report = _said(state, said)
    assert reports.decide(entry, url, report, place, then) == action


def test_a_revised_marker_for_a_fixed_report_waits_on_an_unreadable_main_and_is_filed_later():
    fixed = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1, body=marker(kind="missing-hypothesis"))], main={})
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [fixed]}})
    real = gh.graphql
    gh.graphql = lambda *a, **k: (_ for _ in ()).throw(GHError("graphql: HTTP 502"))
    ctx = context(gh, jobs=[Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40,
                                state="done", closed=True)])
    reports.sync_reports(ctx, now=NOW)
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert ctx.board.created == [] and e["state"] == "fixed" and e["kind"] == "false-statement"     # nothing recorded
    gh.graphql, gh.main = real, {README: "intro\n- Two forms of rank one are equivalent iff their diagonal chains are."}
    reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~2"]


def test_a_fixed_report_whose_source_says_the_same_again_reopens_nothing():
    fixed = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1)])                              # the anchor is back on main; #1 is listed again unchanged
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [fixed]}})
    ctx = context(gh, jobs=[Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40,
                                state="done", closed=True)])
    reports.sync_reports(ctx, now=NOW)
    assert ctx.board.created == [] and gh.register()["roadmaps"]["QuadraticFormInvariants"][0]["state"] == "fixed"


def test_a_fixed_report_revised_while_its_anchor_is_still_gone_records_the_revision_only():
    fixed = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1, body=marker(kind="missing-hypothesis"))], main={README: "rewritten"},
                   at={(SHA, README): "- Two forms of rank one are equivalent iff their diagonal chains are."})
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [fixed]}})
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert ctx.board.created == [] and e["state"] == "fixed" and e["kind"] == "missing-hypothesis"



# --- review round 8 -----------------------------------------------------------------------------------------------

def test_a_fixed_report_its_own_source_rereports_at_a_new_snapshot_is_filed():
    """(ii) The same defect is back; #1's author rereads the roadmap and changes only `roadmap_sha`."""
    fixed = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "fixed",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1, body=marker(roadmap_sha="b" * 40))])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [fixed]}})
    ctx = context(gh, jobs=[Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1,
                                number=40, state="done", closed=True)])
    reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~2"]
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert e["evidence"][f"{LIBRARY}/pull/1"]["roadmap_sha"] == "b" * 40


def test_a_reread_snapshot_of_an_open_report_tells_the_job_nothing_new():
    entry = reports.new_entry(reports.parse(marker())[0][0], "REPORT-QuadraticFormInvariants~1", "open",
                              f"{LIBRARY}/pull/1")
    gh = ReportsGH([item(1, body=marker(roadmap_sha="b" * 40))])
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    job = Job("REPORT-QuadraticFormInvariants~1", "report", "QuadraticFormInvariants", 1, number=40, state="available")
    ctx = context(gh, jobs=[job])
    reports.sync_reports(ctx, now=NOW)
    assert ctx.board.posted == [] and ctx.board.created == []
    assert gh.register()["roadmaps"]["QuadraticFormInvariants"][0]["evidence"][f"{LIBRARY}/pull/1"]["roadmap_sha"] \
        == "b" * 40


# --- review round 9 -----------------------------------------------------------------------------------------------

PIN_PATH = "TauCetiRoadmap/QuadraticFormInvariants/Suggested.lean"


def test_two_pins_whose_names_differ_only_in_case_are_two_reports():
    """(kk) Lean names are case-sensitive: `Foo` and `foo` are two reports, and removing one fixes only it."""
    both = "import Mathlib\n\ntheorem Foo : False := by\n  sorry\n\ntheorem foo : (1 : Nat) = 2 := by\n  sorry\n"
    gh = ReportsGH([item(1, body=marker(file="Suggested.lean", anchor="Foo")),
                    item(2, body=marker(file="Suggested.lean", anchor="foo", refutes=["TauCeti.other"]))],
                   main={PIN_PATH: both})
    ctx = context(gh)
    reports.sync_reports(ctx, now=NOW)
    assert len(ctx.board.created) == 2
    for job, _s, _p in ctx.board.created:
        job.number, job.state = 500 + int(job.inputs["sources"][0].rsplit("/", 1)[1]), "available"
        ctx.board.jobs.append(job)
    gh.main[PIN_PATH] = both.replace("theorem Foo : False := by\n  sorry\n", "")
    reports.sync_reports(ctx, now=NOW)
    states = {e["anchor"]: e["state"] for e in gh.register()["roadmaps"]["QuadraticFormInvariants"]}
    assert states == {"Foo": "fixed", "foo": "open"}





# --- review round 10 ----------------------------------------------------------------------------------------------

PIN_TEXT = "import Mathlib\n\ntheorem square_root_sq (x : ℝ) : Real.sqrt (x ^ 2) = x := by\n  sorry\n"


@pytest.mark.parametrize("before, after", [
    ('theorem literal_length : (r#"a"\ntheorem fake\nb"#).length = 0 := by sorry\n',
     'theorem literal_length : (r#"a"\ntheorem fake\nb"#).length = 17 := by sorry\n'),                     # (oo)
    (PIN_TEXT, PIN_TEXT.replace("(x : ℝ) :", "(x : ℝ) (hx : 0 ≤ x) :")),
    (PIN_TEXT, PIN_TEXT + "\ntheorem after : True := trivial\n"),
    (PIN_TEXT, PIN_TEXT.replace("\n", "\r\n")),
], ids=["raw string", "hypothesis", "anything after it", "line endings"])
def test_a_pins_version_is_its_files_blob_and_any_change_changes_it(before, after):
    """(oo) No boundary is located: a `Suggested.lean` report's version is its file's git blob id at main, so every
    change of the file, a correction inside a raw string included, is a new version."""
    cfg = example_config()
    oids = {}
    for text in (before, after):
        gh = ReportsGH([], main={PIN_PATH: text})
        prbatch.blob_texts(gh, "o/r", [("main", PIN_PATH)], 4, oids=oids)
        assert oids[("main", PIN_PATH)] == blob_id(text)
        assert reports.statement_on(cfg, oids, "main", {"file": "Suggested.lean", "anchor": "x"},
                                    "QuadraticFormInvariants") == blob_id(text)
    assert blob_id(before) != blob_id(after)


def settled(state, right):
    entry = reports.new_entry(reports.parse(marker(file="Suggested.lean", anchor="square_root_sq"))[0][0],
                              "REPORT-QuadraticFormInvariants~2", state, f"{LIBRARY}/pull/1")
    entry["triage"] = {"job": "REPORT-QuadraticFormInvariants~2", "verdict": "rejected", "statement": right}
    return entry


@pytest.mark.parametrize("state", ["fixed", "rejected"])
@pytest.mark.parametrize("main, action", [("same", "settled"), ("other", "file")])
def test_a_settled_pin_is_looked_at_again_only_at_a_version_not_judged_right(state, main, action):
    """(qq) A pin a triage found right at a version (rejected, or fixed by a correction) and a new source: at that
    same version it is only recorded (a late report of the old snapshot); at another it is the next round."""
    entry = settled(state, blob_id(PIN_TEXT))
    report = reports.parse(marker(file="Suggested.lean", anchor="square_root_sq"))[0][0]
    version = blob_id(PIN_TEXT) if main == "same" else blob_id(PIN_TEXT + "\n")
    want = action if action == "file" else ("add to fixed" if state == "fixed" else "add")
    assert reports.decide(entry, f"{LIBRARY}/pull/9", report) == "read main"
    assert reports.decide(entry, f"{LIBRARY}/pull/9", report, "active", None, version) == want
    assert reports.decide(entry, f"{LIBRARY}/pull/9", report, "active", None, None) == "defer"


def test_a_late_report_of_a_corrected_pins_old_snapshot_files_nothing_and_a_regression_files_a_round():
    """(qq) Through sync: the pin was found right at the current version; a second pull request reports its old
    snapshot: recorded, no round. Then the file changes (a regression) and a source says it again: the next
    round is filed, after the rounds it follows (`previous`)."""
    entry = settled("fixed", blob_id(PIN_TEXT))
    gh = ReportsGH([item(9, body=marker(file="Suggested.lean", anchor="square_root_sq"))], main={PIN_PATH: PIN_TEXT})
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    old = Job("REPORT-QuadraticFormInvariants~2", "report", "QuadraticFormInvariants", 2, number=41, state="done",
              closed=True, inputs={"previous": [{"job": "REPORT-QuadraticFormInvariants~1", "issue": 40}]})
    ctx = context(gh, jobs=[old])
    reports.sync_reports(ctx, now=NOW)
    [e] = gh.register()["roadmaps"]["QuadraticFormInvariants"]
    assert ctx.board.created == [] and e["state"] == "fixed" and f"{LIBRARY}/pull/9" in e["sources"]
    gh.main[PIN_PATH] = PIN_TEXT.replace("(x : ℝ) :", "(x : ℝ) (hx : True) :")             # a regression
    gh.search = [item(9, body=marker(file="Suggested.lean", anchor="square_root_sq", roadmap_sha="b" * 40))]
    reports.sync_reports(ctx, now=NOW)
    [(job, _s, _p)] = ctx.board.created
    assert job.id == "REPORT-QuadraticFormInvariants~3"
    assert job.inputs["previous"] == [{"job": "REPORT-QuadraticFormInvariants~1", "issue": 40},
                                      {"job": "REPORT-QuadraticFormInvariants~2", "issue": 41}]


# --- review round 11 ----------------------------------------------------------------------------------------------

R = "r" * 40


@pytest.mark.parametrize("history, right", [
    ([{"job": "J~1", "verdict": "rejected", "statement": R}], R),
    ([{"job": "J~1", "verdict": "rejected", "statement": R}, {"job": "J~2", "verdict": "confirmed", "statement": R}],
     None),                                                         # a human's counterexample overturned it
    ([{"job": "J~1", "verdict": "rejected", "statement": R, "corrected": True}], R),           # fixed by correction
    ([{"job": "J~1", "verdict": "rejected", "statement": R}, {"job": "J~2", "verdict": "needs-human",
                                                               "statement": R}], None),
    ([{"job": "J~1", "verdict": "rejected", "statement": R}, {"job": "J~2", "verdict": "rejected", "by": "hand"}],
     None),                                                         # closed by hand: nobody judged a version
    ([{"job": "J~1", "verdict": "rejected", "statement": R}, {"job": "J~2", "verdict": "confirmed",
                                                               "statement": "s" * 40, "judged": "x", "now": "y"}],
     R),                                                            # a stale verdict was never recorded
    ([{"job": "J~1", "verdict": "confirmed", "statement": R}], None),
    ([], None),
    ([{"job": "J~1", "verdict": "rejected"}], None),                # no version read (a README report)
], ids=["rejected", "then confirmed", "corrected", "then needs-human", "then by hand", "then stale", "confirmed",
        "none", "no version"])
def test_the_version_a_pin_was_last_judged_right_at_is_read_from_its_history(history, right):
    """(rr) In order of recording, the last verdict validly recorded decides: a rejection gives the version it read,
    anything else none. Stale records count for nothing; a job closed by hand judged no version."""
    entry = {"file": "Suggested.lean", "triages": history[:-1], **({"triage": history[-1]} if history else {})}
    assert reports.judged_right(entry) == right


def test_a_pin_confirmed_after_its_rejection_reopens_when_it_comes_back_at_that_version():
    """(rr) Rejected at R, then confirmed at R on a human's request; the file is removed (fixed) and restored at R,
    and a source says it again: the confirmation stands, so the next round is filed."""
    entry = reports.new_entry(reports.parse(marker(file="Suggested.lean", anchor="square_root_sq"))[0][0],
                              "REPORT-QuadraticFormInvariants~2", "fixed", f"{LIBRARY}/pull/1")
    entry["triages"] = [{"job": "REPORT-QuadraticFormInvariants~1", "verdict": "rejected", "statement": blob_id(PIN_TEXT)}]
    entry["triage"] = {"job": "REPORT-QuadraticFormInvariants~2", "verdict": "confirmed", "statement": blob_id(PIN_TEXT)}
    gh = ReportsGH([item(1, body=marker(file="Suggested.lean", anchor="square_root_sq", roadmap_sha="b" * 40))],
                   main={PIN_PATH: PIN_TEXT})
    gh.files[reports.REGISTER] = json.dumps({"version": 1, "scanned": {"github": "2026-11-24T09:00:00Z"},
                                             "roadmaps": {"QuadraticFormInvariants": [entry]}})
    ctx = context(gh, jobs=[Job("REPORT-QuadraticFormInvariants~2", "report", "QuadraticFormInvariants", 2,
                                number=41, state="done", closed=True)])
    reports.sync_reports(ctx, now=NOW)
    assert [j.id for j, _s, _p in ctx.board.created] == ["REPORT-QuadraticFormInvariants~3"]
