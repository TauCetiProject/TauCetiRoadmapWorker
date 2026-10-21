## Sources

- Write everything in your own words: statements plus theorem, section and page numbers. Never
  copy a passage of a source into any file, and never summarise a source section by section. A
  standard fact may cite a standard textbook.
- Read sources only from legal places: open repositories (arXiv, HAL, Numdam, GDZ, EuDML, Project
  Euclid open content, the Stacks project), publisher open archives, author pages, institutional
  repositories, and texts the operator holds locally. Never use a pirate mirror (Library Genesis,
  Sci-Hub, Z-Library, Anna's Archive and their copies) and never get past a paywall. A published
  theorem behind a paywall may be cited by its locator without the text: it is then recorded as
  `uncertified`.
- The texts the roadmap's `sources.toml` already records are in the worker's sources cache, each
  as a line-numbered extraction (page markers `=== page N ===`): __SOURCE_TEXTS__
  Quote locators against these files and cite a passage as `<source id> line N`.
- Fetch every other source through the sources service, not by hand:

      __FETCH__ --id KEY --locator-scheme theorem --locator "Theorem 2.3" --cited-for "1.2" "<citation>"

  where the citation is a DOI, an arXiv id, a URL, or "Authors, Title, Year". It reads only from
  legal places, never past a paywall, keeps the file in the cache, prints the path of its
  line-numbered text, and (where your job edits the manifest) records the entry in `sources.toml`:
  `url`, `fetched_from`, `fetched_at`, `sha256`, `licence_class` and `status`. A citation that
  resolves only to a paywalled page is recorded as `uncertified` with its DOI and your locator.
  For a URL on a host the service does not know (an author's page, an institutional repository),
  check that it is a legal copy and add `--licence-class author-page` or `--licence-class
  institutional`. When you read a preprint, check that its numbering agrees with the published
  version you cite, or cite the version you read.
- Record every cited work in the roadmap's `sources.toml` (schema `tauceti-roadmap-sources/v1`):
  `id`, `authors`, `title`, `year`, `locator_scheme` (theorem, page, section, tag, chapter or
  equation), `locator`, `cited_for`, and what the service recorded. Never put a local path in it.
- When a source you need cannot be read legally, do not guess its contents. The service adds a
  `[[missing]]` table to `__SCRATCH__/needs-references.toml` when it finds nothing; add one yourself
  for a text you need that the manifest lacks (`id`, `authors`, `title`, `year`, `needed_for` = the
  statement or section that needs it, `tried` = where you looked, `locator_scheme`, `locator`,
  `note`). If that file already exists when you start, the worker could not read the texts it
  lists (texts the roadmap cites): keep them, and add yours.
- One request per run: before you stop for a missing text, go through the whole document, fetch
  every source it cites, and list every text you cannot read in that one file; then finish
  everything that does not depend on a missing text and say so in your report. The worker parks the
  job until a human names an alternative legal source, names a text operators hold locally, or
  waives the citation. A job that has asked three times is blocked for a human instead.
- A citation the manifest records as `waived`, or as `uncertified` (paywalled, cited by its locator
  alone), is settled: cite it by its locator and never list it in `needs-references.toml` (the
  worker drops it). Only a human's new answer reopens it.
- Sources stay on the operator's machine: never copy a source file or its extracted text into the
  repository you work in (not into the package folder, not into a report), and never paste a
  passage. Quote locators (`<source id> line N`, a theorem or page number); a fenced block of more
  than 40 lines that is neither Lean nor a table is refused at submission.
