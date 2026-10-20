## Sources

- Write everything in your own words: statements plus theorem, section and page numbers. Never
  copy a passage of a source into any file, and never summarise a source section by section. A
  standard fact may cite a standard textbook.
- Read sources only from legal places: open repositories (arXiv, HAL, Numdam, GDZ, EuDML, Project
  Euclid open content, the Stacks project), publisher open archives, author pages, institutional
  repositories, and texts the operator holds locally (listed in the job). Never use a pirate
  mirror (Library Genesis, Sci-Hub, Z-Library, Anna's Archive and their copies) and never get past a
  paywall. A published theorem behind a paywall may be cited by its locator without the text: it
  is then recorded as `uncertified`.
- Fetch what you read into your scratch directory `__SCRATCH__`, never into a repository. When you
  read a preprint version, check that its numbering agrees with the published version you cite,
  or cite the version you read.
- Record every cited work in the roadmap's `sources.toml` (schema `tauceti-roadmap-sources/v1`):
  `id`, `authors`, `title`, `year`, `locator_scheme` (theorem, page, section, tag, chapter or
  equation), `url`, `fetched_from` (the exact URL of the bytes you read), `sha256` of those bytes,
  `licence_class` (open-repository, publisher-open, author-page, institutional, operator-local),
  `status` (certified when you read the text and checked the locators, uncertified when you cite
  by locator only), `accessed`, `version_note`, `cited_for`.
- When a source you need cannot be read legally, do not guess its contents. Write
  `__SCRATCH__/needs-references.toml` with one `[[missing]]` table per work (`authors`, `title`,
  `year`, `needed_for` = the statement or section that needs it, `tried` = where you looked, `note`),
  finish everything that does not depend on it, and say so in your report. The worker parks the
  job until a human attaches the text, names an alternative legal source, or waives the citation.
