# Sources

Roadmap statements are the author's own words with exact locators into legal sources. Agents fetch
what they need themselves, within the rules below, and record every cited work in the roadmap's
manifest. This page describes the manifest, the legal classes, the cache and the hand-off to humans
when a source cannot be read. Build-plan step 4 (a sources service that fetches, caches and runs OCR)
is not implemented yet: `tauceti_roadmap_worker/sources.py` has the schema, the classification, the
cache layout and the hand-off, and `fetch`/`ocr` are stubs.

## The manifest: `packages/<Name>/sources.toml`

```toml
schema = "tauceti-roadmap-sources/v1"
roadmap = "ExampleRoadmap"

[[source]]
id = "AB21"                                  # the key the README's References section uses
authors = "A. Author, B. Author"
title = "An example paper"
year = 2021
venue = "J. Example Math. 12"                # optional
locator_scheme = "theorem"                   # theorem | page | section | tag | chapter | equation
url = "https://arxiv.org/abs/2101.00001"     # where a reader finds it
fetched_from = "https://arxiv.org/pdf/2101.00001v2"   # the exact bytes that were read ('' if not fetched)
sha256 = "…64 hex digits…"                   # of those bytes
licence_class = "open-repository"            # see below
status = "certified"                         # certified | uncertified | waived
accessed = "2026-10-20"
version_note = "arXiv v2; theorem numbering agrees with the published version"
cited_for = ["1.2", "Layer 3"]               # README sections or targets that cite it
```

`tauceti-roadmap sources check <sources.toml>` validates a manifest (exit 1 on problems); the design,
package and fix lanes run the same check before submitting. The checks are mechanical: required
fields, unique ids, known schemes, classes and statuses, no denied host, and for a certified source
the URL it was fetched from and the SHA-256 of what was read. Whether a locator supports a statement
is the reviewer's job: reviews verify a seeded sample of locators against the text.

**Statuses.**

* `certified`: the text was read and the locators checked against it.
* `uncertified`: a published theorem cited by its locator without the text (for example behind a
  paywall). Allowed while `[sources] uncertified_ok = true`; otherwise the job asks for references.
* `waived`: a human waived the citation in a `needs-references` request.

## Legal classes

| Class | Examples | Hosts |
| --- | --- | --- |
| `open-repository` | arXiv, HAL, Numdam, GDZ, EuDML, Project Euclid open content, the Stacks project, zbMATH | `[sources.allow] open-repository` |
| `publisher-open` | publishers' open archives | `[sources.allow] publisher-open` |
| `author-page` | an author's own page | `[sources.allow] author-page` (often unknown in advance) |
| `institutional` | institutional repositories | `[sources.allow] institutional` |
| `operator-local` | texts the operator already holds | no host |

Pirate mirrors (Library Genesis, Sci-Hub, Z-Library, Anna's Archive and their copies) are refused
three times: by the briefs, by `[sources] deny` (a manifest that names one fails its check), and by
the sandbox proxy's deny list. Paywalls are never circumvented. `tauceti-roadmap sources classify
<url>` prints the class of a URL, `denied`, or `unknown` (an unknown host is not refused, but the
reviewer looks at it).

## The cache

Fetched sources live in the project state directory, `…/projects/<project>/sources/`, one file per
content hash with its text extraction beside it, so that reviewers can quote locators by line:

```text
sources/ab/ab12…ef.pdf
sources/ab/ab12…ef.txt
```

Agents fetch into their own scratch directory, never into a repository. Image-only scans will go
through the OCR recipe (a uv virtual environment with RapidOCR) once the sources service exists.

## The `needs-references` hand-off

When a source a statement needs cannot be read legally, the agent does not guess. It writes
`needs-references.toml` in its scratch directory:

```toml
[[missing]]
authors = "A. Author"
title = "On the thing"
year = 1971
needed_for = "Theorem 2.3 (the comparison map), Layer 2"
tried = ["publisher page (paywalled)", "arXiv (no preprint)", "author page (none)"]
note = "the 1975 survey restates it with a different normalisation"
```

finishes everything that does not depend on it, and stops. The lane then:

1. posts a comment on the job's issue, rendered from
   [`.github/ISSUE_TEMPLATE/needs-references.md`](../.github/ISSUE_TEMPLATE/needs-references.md):
   one row per work (authors, title, year, what it is needed for, what was tried);
2. moves the job to `state:needs-references` (operators without write access to the board leave the
   comment and the state to `board sync`).

The job stays parked until a human attaches the text, names an alternative legal source with its
locator, or waives the citation, and moves the job back to `state:available`. A maintainer can post
the same request by hand:

```bash
tauceti-roadmap --conf $CONF sources request needs-references.toml --job DESIGN-Name --roadmap Name --post
```

Humans find the parked jobs with the `state:needs-references` label; a weekly digest issue is
planned with the sources service.
