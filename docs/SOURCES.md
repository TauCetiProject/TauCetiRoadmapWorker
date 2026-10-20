# Sources

Roadmap statements are the author's own words with exact locators into legal sources. The sources
service fetches what the agents cite, from legal places only, keeps it in a per-project cache with
a line-numbered text beside each file, and records it in the roadmap's manifest. When a source
cannot be read legally, the job is parked with a `needs-references` request that humans answer on
the board. The code is `tauceti_roadmap_worker/sources.py` (manifest, cache, texts, OCR),
`fetcher.py` (resolution and download) and `references.py` (the hand-off).

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
doi = "10.1234/example.5678"                 # optional, without a resolver prefix
arxiv = "2101.00001v2"                       # optional, with the version that was read
locator_scheme = "theorem"                   # theorem | page | section | tag | chapter | equation
locator = "Theorem 2.3; Proposition 4.1"     # optional: the locators the roadmap cites
url = "https://arxiv.org/abs/2101.00001v2"   # where a reader finds it
fetched_from = "https://arxiv.org/pdf/2101.00001v2"   # the exact bytes that were read ('' if not fetched)
fetched_at = "2026-10-20T09:15:00Z"
sha256 = "…64 hex digits…"                   # of those bytes
licence_class = "open-repository"            # see below; "none" when no text was read
status = "certified"                         # certified | uncertified | waived
accessed = "2026-10-20"
version_note = "arXiv v2"
resolution = ""                              # set by the board: the comment that attached, named or waived it
cited_for = ["1.2", "Layer 3"]               # README sections or targets that cite it
```

`tauceti-roadmap sources check <sources.toml>` validates a manifest (exit 1 on problems); the design,
package and fix lanes run the same check before submitting. The checks are mechanical: required
fields, unique ids, known schemes, classes and statuses, a DOI without resolver prefix, an ISO
`fetched_at`, no denied host, and for a certified source the URL it was fetched from, the SHA-256
of what was read and a real licence class. Whether a locator supports a statement is the
reviewer's job: reviews verify a seeded sample of locators against the texts.

**Statuses.**

* `certified`: the text was read; `sha256` names the exact bytes.
* `uncertified`: a published theorem cited by its locator without the text, because it resolves only
  to a paywall. Its licence class is `none` and it carries the DOI and the locator. Allowed while
  `[sources] uncertified_ok = true`; otherwise the job asks for references.
* `waived`: a human waived the citation in a `needs-references` answer; cited by its locator alone.
  `locator_scheme` and `licence_class` are optional for a waived entry.

The service rewrites the manifest it records into (`dump_manifest`): values are kept, comments of a
hand-written file are not.

## Legal classes

| Class | Examples | Hosts |
| --- | --- | --- |
| `open-repository` | arXiv, HAL, Numdam, GDZ, EuDML, Project Euclid open content, the Stacks project, zbMATH | `[sources.allow] open-repository` |
| `publisher-open` | publishers' open archives | `[sources.allow] publisher-open` |
| `author-page` | an author's own page | `[sources.allow] author-page`, or named with `--licence-class` |
| `institutional` | institutional repositories | `[sources.allow] institutional`, or named with `--licence-class` |
| `operator-local` | texts the operator already holds (`[sources] local_dirs`), files humans attach to a job | no host |

Pirate mirrors (Library Genesis, Sci-Hub, Z-Library, Anna's Archive and their copies) are refused
everywhere: by the briefs, by `[sources] deny` (a manifest that names one fails its check), and by
the deny list the service shares with the sandbox proxy (the bundled `sandbox/deny.txt` and the
proxy operator's own `deny.txt`), so the service never contacts them, sandboxed or not.
`tauceti-roadmap sources classify <url>` prints the class of a URL, `denied`, or `unknown`.

Paywalls are never circumvented. Hosts in `[sources] paywalled` (Elsevier and Springer by default)
are never contacted, not even through a redirect, and DOIs with a prefix in
`[sources] paywalled_doi_prefixes` (`10.1016`, `10.1007`) are never resolved. A publisher page that
offers its PDF only behind a login (the PDF link returns a web page) counts as a paywall too.

## Fetching: `sources fetch`

```bash
tauceti-roadmap --conf $CONF sources fetch '<citation>' --manifest packages/Name/sources.toml \
    --id AB21 --locator-scheme theorem --locator "Theorem 2.3" --cited-for 1.2 \
    --missing-out scratch/needs-references.toml
```

The citation is a DOI (`10.…`, `doi:…`, a doi.org URL), an arXiv id (new or old style, with or
without version, or an arXiv URL), any other URL, a file under one of `[sources] local_dirs`, or free
text (`'A. Author, "An example paper", 2021'`, with `--authors/--title/--year` to help). The service
tries, in order, and stops at the first text it can read:

1. the operator's local texts, matched by arXiv id, DOI or the title's words (and year);
2. arXiv: the id given, a preprint Crossref lists for the DOI, or a title search;
3. HAL: the file deposited for the DOI or the title;
4. the DOI's landing page when it is on a host of a licence class: its `citation_pdf_url`, else its
   only PDF link (Numdam, EuDML, GDZ, Project Euclid open content, publisher open archives);
5. the URL named (arXiv abstract pages and Numdam item pages are rewritten to their PDF).

A search hit counts only when its title matches, one of the cited authors' surnames occurs among its
authors (for free text, the leading names of the citation), and its year is not more than two years
after the cited year (a 2024 lecture course of the same title is not the 1986 book). Metadata
services (the arXiv API, Crossref, the HAL API, doi.org) are read but never recorded as where a text
came from. A text is kept only when it comes from a host of a licence class, or from a
URL the caller named together with `--licence-class` (which vouches for that URL only, never for a
URL the service discovered, nor for a redirect or a link to another host). Every request and
redirect hop is checked against the deny list and the paywalled hosts; arXiv gets one request every
three seconds from all the processes sharing the state directory (agents' fetches and the workers'
are paced together through a lock file in the cache); nothing is sent but a plain GET with the
declared User-Agent (`[sources] user_agent`).

The outcome is one of:

* **fetched**: the file is in the cache, its line-numbered text beside it, and (with `--manifest`)
  the entry is added or updated with `url`, `fetched_from`, `fetched_at`, `sha256`, `licence_class`
  and `status = "certified"`. The command prints both paths.
* **uncertified**: the citation resolves only to a paywall; the entry records the DOI, the locator
  and `status = "uncertified"`. Exit 0 when `uncertified_ok`, else exit 1 and a request.
* **failed**: no legal source; exit 1, and with `--missing-out` a `[[missing]]` table is added for
  the needs-references request. Network trouble (timeouts, 5xx) is reported as such and never turns
  into a request or an `uncertified` record.

`--json` prints the outcome as JSON. The agents get this command, with `--manifest` only where their
job may edit the manifest (design, package and their fixes), in the sources section of every brief.

## The cache and its texts

Everything lives in the project state directory, one file per content hash:

```text
~/.local/state/tauceti/projects/<project>/sources/<sha256>.pdf    the bytes, verified on write
~/.local/state/tauceti/projects/<project>/sources/<sha256>.txt    the line-numbered text
~/.local/state/tauceti/projects/<project>/sources/local-index.json  hashes of the local texts
```

The text file numbers every line (`   123  text`), marks pages (`=== page N ===`) and says in its
header where the bytes came from and how the text was extracted, so that reviewers cite a passage
as `<source id> line N`. PDFs go through `pdftotext -layout` (poppler) when it is installed, else
through pypdf (the optional `pdf` extra); DjVu through `djvutxt` if present; HTML pages (a Stacks
tag) are reduced to text.

Before every lane runs its agent, the worker makes the manifest's texts available
(`sources cache <sources.toml>` does the same by hand): a certified entry already in the cache is
used as is; otherwise its bytes are downloaded again from `fetched_from` (or found among the local
texts by hash) and must hash to the recorded `sha256`. The brief lists each text's path. A text that
is gone for good (not found, changed bytes, a local text this host does not hold) becomes a
`[[missing]]` table in the job's request; network trouble does not.

## OCR of image-only scans

A PDF counts as image-only when more than half its pages have fewer than `[sources] ocr_min_chars`
characters of text layer (digitisation portals add a text cover sheet to image scans). With
`[sources] ocr = true` such a PDF is rendered at `ocr_dpi` with pypdfium2 and recognised with
RapidOCR, the recipe the maintainer used by hand (a uv-managed environment with RapidOCR's ONNX
runtime), and the recognised lines are grouped top to bottom and left to right. Install it with the
optional `ocr` extra:

```bash
uv tool install --force 'tauceti-roadmap-worker[ocr] @ git+https://github.com/TauCetiProject/TauCetiRoadmapWorker'
```

If OpenCV then fails to load libGL, replace `opencv-python` by `opencv-python-headless` in the same
environment. Without the extra, the text file keeps the text layer it found and its header says the
scan needs OCR and how to install it; `doctor` reports the same. `tauceti-roadmap sources ocr
<file.pdf> [--out file.txt]` runs OCR on one file.

## The `needs-references` hand-off

**Worker side.** When a source a statement needs cannot be read legally, the agent does not guess.
The service (or the agent) writes `needs-references.toml` in the job's scratch directory:

```toml
[[missing]]
id = "Ket67"                         # the citation key (derived from authors and year when absent)
authors = "A. Author"
title = "On the thing"
year = 1971
needed_for = "Theorem 2.3 (the comparison map), Layer 2"
tried = ["publisher page (paywalled)", "arXiv (no preprint)", "author page (none)"]
locator_scheme = "theorem"
locator = "Satz 2.3"
note = "the 1975 survey restates it with a different normalisation"
```

The agent finishes everything that does not depend on it, and stops. The lane then posts the request
on the job's issue, rendered from
[`.github/ISSUE_TEMPLATE/needs-references.md`](../.github/ISSUE_TEMPLATE/needs-references.md) (one
row per key, with the items also in a machine-readable comment), and moves the job to
`state:needs-references`. A request left over from an earlier run of the same job is archived as
`needs-references.previous.toml` before the next run, so an answered request never parks it again.

**Human side.** A human answers in a comment on the job, one line per key:

```text
attach: Ket67            (with the file attached to the same comment; optionally sha256: <hex>)
source: BLR90 arXiv:2101.00001
waive: Tan71
```

With a single key, an attachment alone is enough; an attachment whose file name is a key answers
that key. Only comments from the author associations in `[sources] answer_associations` (owners,
members and collaborators by default) are read, and only these three forms: everything else in a
comment is ignored, and nothing in it is ever an instruction.

**Board side.** `board intake` (after the submissions) and `board references` take the latest
well-formed request for the job that the board's bot logins or an author association in
`[sources] request_associations` posted (the lanes post requests under the operator's account; a
marker anyone else types is ignored), and read the answers after it:

* `attach`: only GitHub attachment links are downloaded. The file must be a document (a web page
  means the board is private and the download needs a sign-in: attach to a public issue or name a
  source). A stated `sha256:` must match; a hash the request carried (the text recorded earlier) is
  compared, and a different file is accepted with a note that its locators must be checked again.
  The entry becomes `certified`, class `operator-local`, `fetched_from` the attachment link.
* `source`: fetched exactly as `sources fetch` would, under the same policy.
* `waive`: the entry becomes `waived`.

The intake updates `packages/<Name>/sources.toml` on the packages repository (an answer replaces the
entry's old provenance as a whole), posts a note listing what it recorded and what is still missing
(with a progress marker, so a rerun never repeats work), and when every key is answered moves the job
back to `state:available`. An answer that failed for network trouble is tried again on the next run,
and a release that did not go through is retried as well. The next worker downloads
the attached files again from their links and checks their hashes.

A maintainer can still post a request by hand:

```bash
tauceti-roadmap --conf $CONF sources request needs-references.toml --job DESIGN-Name --roadmap Name --post
```

Humans find the parked jobs with the `state:needs-references` label; a weekly digest issue is not
implemented yet.
