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
resolution = ""                              # set by the board: the comment that answered for it
cited_for = ["1.2", "Layer 3"]               # README sections or targets that cite it
```

`tauceti-roadmap sources check <sources.toml>` validates a manifest (exit 1 on problems); the design,
package and fix lanes run the same check before submitting, and the target and review lanes run it
on a manifest they changed. A review may add sources and provenance, never remove or rewrite an
entry the author recorded. The checks are mechanical: required
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
| `operator-local` | texts operators hold in `[sources] local_dirs`, named by a `local:` answer; never uploaded | no host |

An allow-list entry is a host (`arxiv.org`; a leading dot covers its subdomains) or, for a site that
is open in part, a host with a path prefix: `www.ams.org/journals/` covers `www.ams.org` (and
`ams.org`) under `/journals/` only, the AMS journal back issues, and not its paywalled books.
`sources classify <url>` prints the class and the entry that matched.

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
job may edit the manifest (target, design, package, review, and the design and package fixes), in
the sources section of every brief. A review's command adds `--add-only`: an entry the manifest has
keeps every value (its `cited_for` may grow), so a reviewer records new sources without rewriting
the author's.

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

Many legal sources are image-only scans: GDZ and most author-page copies of older papers are page
images, often behind a text cover sheet, and so are many Numdam and EuDML PDFs of older journals.
Their text can be read only by OCR, so keep `[sources] ocr = true` (the default of the example
configuration) and install the `ocr` extra on every worker host.

A PDF counts as image-only when more than half its pages have fewer than `[sources] ocr_min_chars`
characters of text layer. With `[sources] ocr = true` such a PDF is rendered at `ocr_dpi` with
pypdfium2 and recognised with RapidOCR, the recipe the maintainer used by hand (a uv-managed
environment with RapidOCR's ONNX runtime), and the recognised lines are grouped top to bottom and
left to right. Install it with the optional `ocr` extra:

```bash
uv tool install --force --excludes https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapWorker/main/ocr-excludes.txt \
  'tauceti-roadmap-worker[ocr] @ git+https://github.com/TauCetiProject/TauCetiRoadmapWorker'
```

The extra uses headless OpenCV: RapidOCR requires `opencv-python`, which needs libGL and fails to
load on a server without a display ("libGL.so.1: cannot open shared object file"), so
`ocr-excludes.txt` keeps it out and `opencv-python-headless` provides the same module. In a
checkout, `uv pip install -e '.[ocr]'` applies the same exclusion from `pyproject.toml`.
`doctor` reports whether OCR loads, with the import error when it does not.

Without OCR, the text file keeps the text layer it found and its header says the scan could not be
read. A worker treats such a text as unavailable: the job asks for the source again on its issue as
"fetched but unreadable (image-only; OCR off or missing)", rather than as missing, and a worker
with OCR reads it (a text extracted before OCR was installed is read again). `tauceti-roadmap
sources ocr <file.pdf> [--out file.txt]` runs OCR on one file.

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

A run posts one request, which lists everything: the brief asks the agent to go through the whole
document first, and when the agent's request misses a manifest text the worker found unreadable,
the worker adds it. A citation the manifest settled (`waived`, or `uncertified` where the project
accepts uncertified citations) is never requested again, by the same job or any later lane of the
roadmap, until a human answers it with `source:` or `local:`: the worker drops it from the request
with a line in the round log (and the job folder's `requests.log`), and a request left with nothing
does not park the job. A source is known by its identity, not its key: a request that names a
settled work under a new key (the same DOI or arXiv id, the same normalised title and first author,
or the same author and year with a title the other contains or starts with) is dropped as `settled
in the manifest as <old key>: <new key>`, a text a human already answered is asked for at most once
more before the next request is settled with the earlier answer, and the intake applies the earlier
answer to a renamed key it still receives. A job that has posted three requests is set to `state:blocked`, with a
comment for a human, instead of posting a fourth.

**Human side.** A human answers in a comment on the job, one line per key:

```text
source: BLR90 arXiv:2101.00001
source: Tan71 https://users.renyi.hu/~author/tan71.pdf   (optionally licence: institutional)
local: Ket67 Kettle1967.pdf          (optionally sha256: <hex>)
waive: Tan71
```

Sources never enter a repository or an issue. Files attached to a comment are never downloaded
(the intake notes that attachments are not used and asks for one of the three answers), and a
`local:` answer names a file, never a path. `local:` and `waive:` are read from the author
associations in `[sources] answer_associations` (owners, members and collaborators by default) and
from the allowed answerers. A `source:` line names a legal source its author has checked, so it is
read only from an allowed answerer: the logins and teams in `[board] reference_answerers`, by
default the target filers and the operators; anyone else's `source:` is ignored with a note. Only
these three forms are read: everything else in a comment is ignored, and nothing in it is ever an
instruction.

**Board side.** `board intake` (after the submissions) and `board references` take the latest
well-formed request for the job that the board's bot logins or an author association in
`[sources] request_associations` posted (the lanes post requests under the operator's account; a
marker anyone else types is ignored), and read the answers after it:

* `source`: fetched as `sources fetch` would, under the same policy, except that the answerer vouches
  for the URL they name: on that URL's host a licence class the project does not list is theirs to
  give, and the entry records `licence_class = "answered-by:<login>"` (or the class a trailing
  `licence: <class>` names). A host on the deny list is refused even then, and a host the project
  already classes keeps its own class. DOIs and arXiv ids go through the usual classes: name the
  text's URL for a host outside them.
* `local`: nothing is fetched. The entry becomes class `operator-local` with `local_name` the file
  name: `certified` with that hash when the answer states one (a different hash from the text
  recorded earlier is noted: its locators must be checked again), else status `local`. The job's
  spec records the requirement (`local_sources`), so that a worker takes the job only when its
  operator holds the file in `[sources] local_dirs` (by hash when one is known, else by name);
  another worker logs `skip <JOB>: needs local source <file name>` and leaves the job to others,
  with no claim and no failure counted. The first lane that reads a `local` text on its operator's
  machine records its hash in the manifest it submits (a review leaves the author's entries as they
  are). A worker that lacks an operator-local text a job does not require lists it in the brief as
  unavailable ("needs local source"); it never becomes a new request. A worker that holds it reads
  it by its file name, then by its hash (a different hash is noted next to the text), and a `.txt`
  file is the text itself; `sources fetch <file name>` resolves a bare file name in
  `[sources] local_dirs` the same way. A `local:` answer is settled: no later request asks for it.

Answers are recorded in the manifest on the board's main. A job whose package lives on a board pull
request (its reviews, red-team passes and fixes) reads its own manifest at the pull request's head
with the answered entries (those carrying a `resolution`) from main in place of its entries for the
same keys, so every answer reaches it.
* `waive`: the entry becomes `waived`.

The intake updates `packages/<Name>/sources.toml` on the packages repository (an answer replaces the
entry's old provenance as a whole), posts a note listing what it recorded and what is still missing
(with a progress marker, so a rerun never repeats work), and when every key is answered moves the job
back to `state:available`. A `source:` answer whose fetch failed for network trouble (a timeout, a
refused connection, a 5xx) is tried again on later runs, at most `[sources] answer_retries` times
(default 3) at 10, 20, 40, ... minutes, with one note on the first failure (and none in between);
the attempts are recorded in the job's spec. When the last attempt fails too, or the fetch failed
for another reason (a 404, a refused licence class, an unrecognised file), one note raises the
request again for the keys still missing and the answer is forgotten; a new answer for the key
(`source:`, `local:` or `waive:`) takes over at once. A release that did not go through is retried
as well.

## PostScript and compressed files

A body that starts with the gzip magic (an author page's `paper.ps.gz`) is decompressed before it
is classified, and the cache keeps (and the manifest hashes) the document itself. PostScript is read
through Ghostscript: `gs -sDEVICE=pdfwrite` renders it to PDF, which `pdftotext` then reads; a PDF
whose text layer is glyph names rather than words (the Type 3 fonts dvips writes) is rendered the
same way, and when its text is still unreadable the pages are rendered at 300 dpi with
`gs -sDEVICE=pnggray` and recognised by OCR (with `[sources] ocr`). Without Ghostscript the service
says "needs Ghostscript" for such a file, and `doctor` warns. Ghostscript can be installed without
root: `apt-get download` the `ghostscript`, `libgs10` and `libgs-common` packages, unpack them with
`dpkg -x` into `~/.local/opt/ghostscript`, and put a `gs` wrapper on the PATH that sets
`LD_LIBRARY_PATH` to the unpacked library directory and `GS_LIB` to its Resource, lib, font and
ICC-profile directories (the profiles come from `libgs-common`; unpack `fonts-urw-base35` too, or a
file that names a standard font such as Times or Helvetica will not render).

## Sources never enter a repository

The cache under the operator's state directory (`<state>/sources/`) is the only copy of a text and
of its extraction; `doctor` fails when that directory lies inside a git checkout, and `sources ocr
--out` refuses a path inside one. The manifest carries the hash, the locator scheme and the licence
class, and reports and briefs quote locators, never passages. The gates refuse, in every board
submission (`Lane.submit` before the push and `board intake` before the merge) and in the push
gate of every content pull request (port, red-team fix, gate):

* source documents: PDF, DjVu, PostScript and EPUB files, by name or by content;
* extracted text over 32 KB: a `.txt` file, or any file carrying the service's `=== page N ===`
  markers;
* manifest entries that embed text: a field named `text`, `excerpt`, `quote`, `passage`, `content`,
  `abstract` (and the like), or a value over 1,000 characters;
* fenced blocks of more than 40 lines in Markdown that are neither Lean nor a table.

A maintainer can still post a request by hand:

```bash
tauceti-roadmap --conf $CONF sources request needs-references.toml --job DESIGN-Name --roadmap Name --post
```

Humans find the parked jobs with the `state:needs-references` label; a weekly digest issue is not
implemented yet.
