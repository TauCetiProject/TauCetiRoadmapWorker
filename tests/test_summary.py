"""The summary block that opens a pull request's description: a summary of the roadmap when the pull
request adds it, of the change when it revises a roadmap already on its base branch."""
import json
from types import SimpleNamespace

import pytest

from tauceti_roadmap_worker import summary
from tauceti_roadmap_worker.gh import GHError
from tauceti_roadmap_worker.lanes import prreview as lane_prreview

ACTIVE = "TauCetiRoadmap"
NEW_TEXT = ("**What this roadmap builds.** The groups in low degrees, with their matrix interfaces and maps.\n\n"
            "**Why Tau Ceti wants it.** Later roadmaps compare their own groups with the ones stated here.\n\n"
            "**Approach and generality.** Rings are arbitrary, and the library's carriers are used as they are.")
REV_TEXT = ("**What this revision changes.** The boundary formula gains its orientation and a recorded word.\n\n"
            "**Why the change is made.** The formula as written was false for a general invertible matrix.\n\n"
            "**What the roadmap keeps.** The patching theorem and every other layer stand as they were.")
MARKER = '<!-- tauceti-roadmap-pr {"roadmap": "Alpha"} -->'
BODY = f"Corrects the boundary formula of Alpha, which was false as stated.\n\n{MARKER}\n"


def changed(path, status="modified", **more):
    return {"filename": path, "status": status, **more}


def block(text):
    return f"{summary.MARK_OPEN}\n{text}\n{summary.MARK_CLOSE}"


def test_an_added_readme_is_a_new_roadmap_and_a_changed_one_a_revision():
    new = summary.classify([changed(f"{ACTIVE}/Alpha/README.md", "added"), changed(f"{ACTIVE}/Alpha/Suggested.lean", "added"),
                            changed(f"{ACTIVE}/Alpha/metadata.toml", "added")], ACTIVE)
    assert (new.kind, new.folders) == ("new", [f"{ACTIVE}/Alpha"])
    rev = summary.classify([changed(f"{ACTIVE}/Alpha/README.md")], ACTIVE)
    assert (rev.kind, rev.folders) == ("revision", [f"{ACTIVE}/Alpha"])
    lean = summary.classify([changed(f"{ACTIVE}/Alpha/Suggested.lean")], ACTIVE)
    assert lean.kind == "revision"
    none = summary.classify([changed("lakefile.toml"), changed(f"{ACTIVE}/Alpha/metadata.toml")], ACTIVE)
    assert none.kind == "" and "no roadmap" in none.why


def test_a_new_roadmap_that_edits_others_beside_it_is_still_new():
    files = [changed(f"{ACTIVE}/Family/Part/README.md", "added"), changed(f"{ACTIVE}/Family/README.md"),
             changed(f"{ACTIVE}/Consumer/README.md")]
    subject = summary.classify(files, ACTIVE)
    assert (subject.kind, subject.folders) == ("new", [f"{ACTIVE}/Family/Part"])
    assert [f["filename"] for f in subject.files] == [f"{ACTIVE}/Family/Part/README.md"]


def test_several_added_readmes_are_one_roadmap_by_containment_or_by_the_marker():
    family = [changed(f"{ACTIVE}/Family/README.md", "added"), changed(f"{ACTIVE}/Family/One/README.md", "added"),
              changed(f"{ACTIVE}/Family/Two/README.md", "added")]
    assert summary.classify(family, ACTIVE).folders == [f"{ACTIVE}/Family"]
    two = [changed(f"{ACTIVE}/Alpha/README.md", "added"), changed(f"{ACTIVE}/Bravo/README.md", "added")]
    assert summary.classify(two, ACTIVE, "Bravo").folders == [f"{ACTIVE}/Bravo"]
    unnamed = summary.classify(two, ACTIVE)
    assert unnamed.kind == "" and "several roadmaps" in unnamed.why


def test_a_revision_of_several_roadmaps_puts_the_marked_one_first():
    files = [changed(f"{ACTIVE}/Alpha/README.md"), changed(f"{ACTIVE}/Bravo/README.md"), changed(f"{ACTIVE}/Charlie/Suggested.lean")]
    assert summary.classify(files, ACTIVE, "Bravo").folders == [f"{ACTIVE}/Bravo", f"{ACTIVE}/Alpha", f"{ACTIVE}/Charlie"]


def test_the_kind_of_a_block_is_read_from_its_first_heading():
    assert summary.block_kind(block(NEW_TEXT) + "\n\nProse.") == "new"
    assert summary.block_kind("Prose.\n\n" + block(REV_TEXT)) == "revision"
    assert summary.block_kind(block("Three paragraphs of another form.")) == "other"
    assert summary.block_kind("Prose only.") == ""


def test_the_block_goes_first_in_place_of_one_anywhere_in_the_description():
    prose = "Corrects the boundary formula, which was false as stated for a general matrix."
    at_end = f"{prose}\n\n{block(NEW_TEXT)}\n\n{MARKER}\n"
    new = summary.apply(at_end, REV_TEXT)
    assert new == f"{block(REV_TEXT)}\n\n{prose}\n\n{MARKER}\n"
    assert new.count(summary.MARK_OPEN) == 1 and "What this roadmap builds" not in new
    assert summary.apply(new, REV_TEXT) == new                      # idempotent
    assert summary.apply(f"{prose}\n\n{MARKER}\n", NEW_TEXT).startswith(block(NEW_TEXT) + "\n\n" + prose)


@pytest.mark.parametrize("body", ["", "   \n", '{"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"}'])
def test_a_description_that_reads_empty_or_as_an_api_error_is_never_rewritten(body):
    assert summary.apply(body, NEW_TEXT) is None


def test_a_summary_is_checked_for_its_form_and_for_what_it_may_not_say():
    assert summary.validate(NEW_TEXT, "new") == [] and summary.validate(REV_TEXT, "revision") == []
    assert any("three paragraphs" in p for p in summary.validate(NEW_TEXT, "revision"))     # the wrong kind's headings
    assert any("three paragraphs" in p for p in summary.validate(REV_TEXT.split("\n\n")[0], "revision"))
    for bad, what in [("as asked in #1234", "number"), ("since 2026-11-06", "date"), ("in this PR", "pull request"),
                      ("see README.md", "file name"), ("a reviewer asked for it", "review history"),
                      ("we now state it", "first-person")]:
        problems = summary.validate(REV_TEXT.replace("stand as they were", f"stand as they were, {bad}"), "revision")
        assert any(what in p for p in problems), (bad, problems)
    long = REV_TEXT + " " + " ".join(["word"] * summary.WORD_LIMIT)
    assert any("words" in p for p in summary.validate(long, "revision"))
    empty = REV_TEXT.replace(" The formula as written was false for a general invertible matrix.", "")
    assert any("empty" in p for p in summary.validate(empty, "revision"))


def test_a_revision_gets_both_texts_and_their_differences_and_a_new_roadmap_its_readme(tmp_path):
    texts = {("TauCetiRoadmap/Alpha/README.md", "before"): "# Alpha\n\nThe boundary is d(u).\n",
             ("TauCetiRoadmap/Alpha/README.md", "after"): "# Alpha\n\nThe boundary is d(u) with its orientation.\n",
             ("TauCetiRoadmap/Alpha/Suggested.lean", "after"): "theorem boundary : True := sorry\n"}
    read = lambda path, side: texts[(path, side)]
    subject = summary.classify([changed(f"{ACTIVE}/Alpha/README.md"), changed(f"{ACTIVE}/Alpha/Suggested.lean", "added")], ACTIVE)
    (tmp_path / summary.OUT_NAME).write_text("stale")
    written = summary.write_material(tmp_path, subject, read, "The author's account.")
    assert written == ["before/Alpha/README.md", "after/Alpha/README.md", "changes/Alpha/README.md.diff",
                       "after/Alpha/Suggested.lean", "changes/Alpha/Suggested.lean.diff", "description.md"]
    diff = (tmp_path / "changes/Alpha/README.md.diff").read_text()
    assert "-The boundary is d(u)." in diff and "+The boundary is d(u) with its orientation." in diff
    assert not (tmp_path / summary.OUT_NAME).exists()                 # an earlier run's text is not read as this one's
    brief = summary.brief_for(subject, "Alpha: orient the boundary", written)
    assert "REVISES Alpha, which already exists" in brief and "- `changes/Alpha/README.md.diff`" in brief
    assert "**What this revision changes.**" in brief and "What this roadmap builds" not in brief
    new = summary.classify([changed(f"{ACTIVE}/Alpha/README.md", "added")], ACTIVE)
    assert summary.write_material(tmp_path, new, read) == ["README.md"]
    assert not (tmp_path / "before").exists() and not (tmp_path / "description.md").exists()
    assert "ADDS the roadmap Alpha" in summary.brief_for(new, "Alpha: roadmap", ["README.md"])


def test_a_renamed_file_is_compared_with_its_text_under_the_old_name(tmp_path):
    seen = []

    def read(path, side):
        seen.append((path, side))
        return f"{path} {side}\n"

    subject = summary.classify([changed(f"{ACTIVE}/Family/Part/README.md", "renamed",
                                        previous_filename=f"{ACTIVE}/Part/README.md")], ACTIVE)
    assert subject.kind == "revision"
    summary.write_material(tmp_path, subject, read)
    assert seen == [(f"{ACTIVE}/Part/README.md", "before"), (f"{ACTIVE}/Family/Part/README.md", "after")]


def test_a_refused_text_gets_one_more_attempt_with_the_reasons(tmp_path):
    subject = summary.classify([changed(f"{ACTIVE}/Alpha/README.md")], ACTIVE)
    briefs = []

    def run(brief, attempt):
        briefs.append(brief)
        (tmp_path / summary.OUT_NAME).write_text(NEW_TEXT if attempt == 1 else REV_TEXT)
        return SimpleNamespace(limited=False)

    text, problems = summary.write_summary(tmp_path, subject, "t", [], run)
    assert (text, problems) == (REV_TEXT, []) and len(briefs) == 2
    assert "previous attempt was refused" in briefs[1] and "three paragraphs" in briefs[1]
    silent = summary.write_summary(tmp_path, subject, "t", [], lambda brief, attempt: SimpleNamespace(limited=False))
    assert silent == ("", ["it wrote no `summary.md`"])
    limited = summary.write_summary(tmp_path, subject, "t", [], lambda b, a: SimpleNamespace(limited=True, message="limit"))
    assert limited[0] == "" and "usage limit" in limited[1][0]


class Hub:
    """One pull request: its description, file list and the texts on both sides."""

    def __init__(self, body, files, texts=None):
        self.body, self.files, self.texts = body, files, texts or {}
        self.writes, self.reads = [], []

    def api(self, path, paginate=False):
        self.reads.append(path)
        if path.endswith("/files?per_page=100"):
            return self.files
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": "mergebase"}}
        if "/pulls/" in path:
            return {"number": 7, "title": "Alpha: orient the boundary", "body": self.body,
                    "base": {"sha": "basetip"}, "head": {"sha": "headsha"}}
        raise GHError(f"GET {path}: not faked")

    def raw_file(self, repo, path, ref=""):
        return self.texts[(path, ref)]

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        self.body = payload["body"]


def context(hub, tmp_path):
    cfg = SimpleNamespace(repositories=SimpleNamespace(content="org/content"), roadmap=SimpleNamespace(active_dir=ACTIVE),
                          sandbox=SimpleNamespace(profile="off", proxy_url="", netjail="", claude_hook=False))
    return SimpleNamespace(cfg=cfg, gh=hub, state=tmp_path / "state", wdir=tmp_path / "worker")


def agent_writing(text):
    """A stand-in for the agent process: it writes `text` to the output file of the working directory."""
    def launch(argv, cwd=None, **kwargs):
        (cwd / summary.OUT_NAME).write_text(text)
        return SimpleNamespace(stdin=SimpleNamespace(write=lambda s: None, close=lambda: None), stdout=iter(()),
                               wait=lambda timeout=None: 0, pid=0)
    return launch


def revision_hub(body):
    readme = f"{ACTIVE}/Alpha/README.md"
    return Hub(body, [changed(readme)], {(readme, "mergebase"): "The boundary is d(u).\n",
                                         (readme, "headsha"): "The boundary is d(u) with its orientation.\n"})


def test_a_whole_roadmap_summary_on_a_revision_is_replaced_by_a_summary_of_the_revision(tmp_path):
    hub = revision_hub(f"{BODY}\n{block(NEW_TEXT)}\n")
    ctx = context(hub, tmp_path)
    assert summary.refresh(ctx, [7], agent="claude", model="m", listing=True) == \
        ["#7 Alpha: would write a revision summary in place of its new block"]
    assert hub.writes == []
    lines = summary.refresh(ctx, [7], agent="claude", model="m", launch=agent_writing(REV_TEXT))
    assert lines == ["#7 Alpha: wrote a revision summary in place of its new block (45 words)"]
    assert hub.body.startswith(block(REV_TEXT)) and "What this roadmap builds" not in hub.body and MARKER in hub.body
    assert "org/content/compare/basetip...headsha" in " ".join(hub.reads)          # the texts before are the merge base's
    work = tmp_path / "state" / "summaries" / "pr-7"
    assert "-The boundary is d(u)." in (work / "changes/Alpha/README.md.diff").read_text()
    assert "Corrects the boundary formula" in (work / "description.md").read_text()
    assert json.loads((work / "record.json").read_text())["kind"] == "revision"
    # the block is now of the right kind: a second pass leaves it
    assert summary.refresh(ctx, [7], agent="claude", model="m", launch=agent_writing("never read")) == \
        ["#7 Alpha: a revision summary is present; unchanged"]


def test_a_description_without_a_block_gets_one_only_when_asked(tmp_path):
    hub = revision_hub(BODY)
    ctx = context(hub, tmp_path)
    assert "pass --missing" in summary.refresh(ctx, [7], agent="claude", model="m", launch=agent_writing(REV_TEXT))[0]
    assert hub.writes == []
    summary.refresh(ctx, [7], agent="claude", model="m", missing=True, launch=agent_writing(REV_TEXT))
    assert hub.body.startswith(block(REV_TEXT))


def test_a_usage_limit_met_by_an_earlier_run_does_not_refuse_the_next_one(tmp_path):
    hub = revision_hub(BODY)
    ctx = context(hub, tmp_path)
    work = tmp_path / "state" / "summaries" / "pr-7"
    work.mkdir(parents=True)
    (work / "run-1.out").write_text("START\nYou've hit your weekly limit · resets 3pm (UTC)\nEXIT 1\n")
    lines = summary.refresh(ctx, [7], agent="claude", model="m", missing=True, launch=agent_writing(REV_TEXT))
    assert lines == ["#7 Alpha: wrote a revision summary (45 words)"] and hub.body.startswith(block(REV_TEXT))
    assert "weekly limit" not in (work / "run-1.out").read_text()


def test_a_refused_summary_leaves_the_description_alone(tmp_path):
    hub = revision_hub(f"{block(NEW_TEXT)}\n\n{BODY}")
    lines = summary.refresh(context(hub, tmp_path), [7], agent="claude", model="m", launch=agent_writing(NEW_TEXT))
    assert "summary refused" in lines[0] and "description unchanged" in lines[0] and hub.writes == []


def test_a_new_roadmap_keeps_its_summary_and_a_pull_request_without_roadmap_text_gets_none(tmp_path):
    readme = f"{ACTIVE}/Alpha/README.md"
    hub = Hub(f"{block(NEW_TEXT)}\n\n{BODY}", [changed(readme, "added")])
    assert summary.refresh(context(hub, tmp_path), [7], agent="claude", model="m") == \
        ["#7 Alpha: a new summary is present; unchanged"]
    chore = Hub(BODY, [changed("lake-manifest.json")])
    assert "no summary" in summary.refresh(context(chore, tmp_path), [7], agent="claude", model="m", missing=True)[0]


def test_a_repair_keeps_the_summary_block_first_in_the_description_it_delivers():
    old = "\n\n".join(["Old prose.", "<!-- depends-on -->\n- [ ] #5 Bravo\n<!-- /depends-on -->", block(REV_TEXT), MARKER])
    assert lane_prreview.merged_description("New prose.", old) == "\n\n".join([
        block(REV_TEXT), "New prose.", "<!-- depends-on -->\n- [ ] #5 Bravo\n<!-- /depends-on -->", MARKER]) + "\n"


def test_a_pass_over_the_open_pull_requests_reads_each_once_and_reports_as_it_goes(tmp_path):
    readme = f"{ACTIVE}/Alpha/README.md"
    hub = Hub(f"{block(NEW_TEXT)}\n\n{BODY}", [changed(readme)])
    listed = {"number": 7, "title": "Alpha: orient the boundary", "body": hub.body,
              "base": {"sha": "basetip"}, "head": {"sha": "headsha"}}
    plain = {"number": 8, "title": "Bravo", "body": "No block and no marker."}
    marked = {"number": 9, "title": "Charlie", "body": BODY}
    hub.api = lambda path, paginate=False, real=hub.api: (
        [plain, marked, listed] if path.endswith("/pulls?state=open&per_page=100") else real(path, paginate))
    ctx = context(hub, tmp_path)
    known = summary.candidates(ctx)
    assert list(known) == [7] and list(summary.candidates(ctx, missing=True)) == [7, 9]
    hub.reads.clear()
    seen = []
    lines = summary.refresh(ctx, list(known), agent="claude", model="m", listing=True, known=known, report=seen.append)
    assert seen == lines == ["#7 Alpha: would write a revision summary in place of its new block"]
    assert hub.reads == ["repos/org/content/pulls/7/files?per_page=100"]       # the listing's record is not read again
