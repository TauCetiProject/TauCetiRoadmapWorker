"""The errata register: source issues parsed from review and red-team comments and kept on the board."""

import base64
import json

from tauceti_roadmap_worker import errata, prreviews

from .fakes import FakeGH

COMMENT = """Review (claude) of abc1234

Verdict: needs_changes

1. **F01** (P3) a locator

## Source issues

- **Misprint** in Weibel-Kbook at III.6.9.2, printed p. 241: the source says the cyclic algebra map
  is bijective for every field; it should be injective in general; affects a stated result.
- **Error** in `Lemos2017` at Theorem 2.3, p. 7: the source says p > 37 suffices; it should be p > 37
  and p unramified; affects the proof; corrected in print at the published erratum.
- a free-form note the parser cannot read

## Not verifiable
- nothing

<!-- tauceti-roadmap-review {"job": "REV-PKG-Foo", "worker": "w1", "agent": "claude", "model": "opus"} -->
"""


def test_source_issues_are_parsed_and_unparsed_bullets_kept():
    issues = prreviews.source_issues(COMMENT)
    assert [i["kind"] for i in issues] == ["misprint", "error", "unparsed"]
    assert issues[0]["source"] == "Weibel-Kbook" and issues[0]["affects"] == "a stated result"
    assert issues[1]["source"] == "Lemos2017" and issues[1]["note"] == "corrected in print at the published erratum"
    assert prreviews.source_issues("## Source issues\n- none\n") == []
    assert prreviews.source_issues("no such section") == []


class ErrataGH(FakeGH):
    def __init__(self):
        super().__init__()
        self.files, self.writes = {}, []

    def api(self, path, paginate=False):
        if "/pulls?state=all" in path:
            return [{"number": 7, "updated_at": "2026-10-23T10:00:00Z"}]
        if "/issues/7/comments" in path:
            return [{"body": COMMENT, "created_at": "2026-10-23T09:00:00Z", "html_url": "u"},
                    {"body": "just a chat comment", "created_at": "2026-10-23T09:30:00Z"}]
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name in self.files:
                return {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": "s-" + name}
            from tauceti_roadmap_worker.gh import GHError
            raise GHError("404")
        return super().api(path, paginate)

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        name = path.split("/contents/", 1)[1]
        self.files[name] = base64.b64decode(payload["content"]).decode()
        return {}


def test_sync_errata_records_new_issues_once_and_renders_the_register():
    from .fakes import bot_context, example_config
    gh = ErrataGH()
    ctx = bot_context(cfg=example_config(), gh=gh)
    out = errata.sync_errata(ctx)
    assert out == ["errata: 3 new source issue(s) recorded (3 in all)"]
    data = json.loads(gh.files[errata.DATA])
    assert data["scanned_to"] == "2026-10-23T09:00:00Z" and len(data["issues"]) == 3
    assert data["issues"][0]["job"] == "REV-PKG-Foo" and data["issues"][0]["roadmap"] == "Foo"
    assert data["issues"][0]["board_pr"] == 7 and data["issues"][0]["head"] == "abc1234"
    register = gh.files[errata.REGISTER]
    assert "## Weibel-Kbook" in register and "**Misprint** at III.6.9.2" in register and "Found by Foo (#7 at abc1234" in register
    assert "1 unparsed bullets" in register
    # A second run adds nothing and writes nothing.
    assert errata.sync_errata(ctx) == ["errata: nothing new"] and len(gh.writes) == 2


def test_read_json_fetches_the_blob_when_the_contents_endpoint_returns_no_content():
    # The contents endpoint answers a file over 1 MB with its sha and an empty content; the blob is then read by sha.
    from .fakes import bot_context, example_config

    class LargeGH(ErrataGH):
        def api(self, path, paginate=False):
            if "/contents/" in path:
                return {"content": "", "encoding": "none", "size": 1102135, "sha": "big"}
            if path.endswith("/git/blobs/big"):
                return {"content": base64.b64encode(json.dumps({"issues": [{"job": "J"}]}).encode()).decode()}
            return super().api(path, paginate)

    ctx = bot_context(cfg=example_config(), gh=LargeGH())
    data, sha = errata._read_json(ctx, "o/r", errata.DATA)
    assert data == {"issues": [{"job": "J"}]} and sha == "big"
