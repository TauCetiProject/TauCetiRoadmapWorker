"""`targets file-many`: a list of targets filed once each, with GitHub faked."""

import json

from tauceti_roadmap_worker import targets
from tauceti_roadmap_worker.board import new_job

from .fakes import FakeBoard, bot_context


class GH:
    def __init__(self, issues=()):
        self.issues, self.writes = list(issues), []

    def api(self, path, paginate=False):
        assert "kind:target" in path and "state=all" in path
        return self.issues

    def write(self, method, path, payload=None):
        self.writes.append(payload)
        return {"number": 40 + len(self.writes)}


def test_a_toml_list_files_each_target_once(tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text('[[target]]\nname = "QuadraticFormsLocal"\nstatement = "Quadratic forms over local fields."\n'
                    'why = "A supplier."\nsources = "Serre, Ch. IV"\n\n'
                    '[[target]]\ntitle = "phantom supplier: Hecke operators"\nstatement = "Hecke operators."\nwhy = "x"\n\n'
                    '[[target]]\nname = "Existing"\nstatement = "s"\nwhy = "w"\n\n'
                    '[[target]]\nname = "Filed"\nstatement = "s"\nwhy = "w"\n\n'
                    '[[target]]\nstatement = "no why"\n\n'
                    '[[target]]\nname = "QuadraticFormsLocal"\nstatement = "again"\nwhy = "w"\n')
    filed = {"number": 9, "title": "[Target] Filed", "body": "### Suggested name\n\nFiled\n"}
    gh = GH([filed])
    ctx = bot_context(gh=gh, board=FakeBoard(jobs=[new_job("DESIGN-Existing")]))
    entries = targets.read_list(path)
    assert targets.file_many(ctx, entries) == [
        "QuadraticFormsLocal: would file '[Target] QuadraticFormsLocal' (dry run; pass --yes)",
        "PhantomSupplierHeckeOperators: would file '[Target] phantom supplier: Hecke operators' (dry run; pass --yes)",
        "Existing: already on the board (DESIGN-Existing); skipped",
        "Filed: already on the board (#9); skipped",
        "entry 5: needs a statement, a why and a usable name; skipped",
        "QuadraticFormsLocal: already on the board (this list); skipped"]
    assert gh.writes == []
    out = targets.file_many(ctx, entries, yes=True)
    assert out[:2] == ["QuadraticFormsLocal: filed #41 '[Target] QuadraticFormsLocal'",
                       "PhantomSupplierHeckeOperators: filed #42 '[Target] phantom supplier: Hecke operators'"]
    first = gh.writes[0]
    assert first["labels"] == ["roadmap-job", "kind:target"] and "### Known sources\n\nSerre, Ch. IV" in first["body"]


def test_a_jsonl_list(tmp_path):
    path = tmp_path / "targets.jsonl"
    path.write_text("# phantom suppliers\n" + json.dumps({"name": "Alpha", "statement": "s", "why": "w"}) + "\n\n")
    assert targets.read_list(path) == [{"name": "Alpha", "statement": "s", "why": "w"}]
