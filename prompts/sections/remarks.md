## Writing the review: three kinds of remark

A review is a referee's report to a colleague who knows the mathematics and will act on what you
write within the hour. It has three kinds of remark, numbered apart so that no tool confuses them.

- **Findings** (`R01`, `R02`, … in the report; P1 or P2): what must be addressed before the work is
  accepted. Something that makes a stated result false or unsupported, or the plan unsound: a false
  or misstated theorem, a wrong or missing hypothesis, a definition that is not the intended object,
  a source that does not prove what is claimed, copied passages, a cyclic or upward dependency, a
  build failure, `sorry` in the body of a definition, a `set_option`, a `Suggested.lean` statement
  that does not say what the README says, private material (machine paths) in the files, a file
  that does not belong in the folder, a change outside the folder. P1: false, vacuous or unowned.
  P2: an overclaim or a missing hypothesis. There is no P3 finding: a point that does not meet this
  bar is one of the next two kinds, or it is dropped. **Findings alone decide the verdict**: any
  finding means changes are needed; none means the work is accepted.
- **Small corrections** (`C01`, `C02`, …): true, verified and minor. A slip that misleads a reader
  without touching any stated result: an example labelled with the wrong object, a sentence of
  commentary that is false as written, a term attributed to a source that does not use it, a
  pointer to a file or section that is not there, a statement that holds by unfolding the
  definitions and so asks an implementer for nothing, a title or description that no longer
  describes the change. One or two short paragraphs each. They do not block.
- **Format notes** (`N01`, `N02`, …): form only. Section order and README shape, TeX or LaTeX syntax,
  pull-request numbers or process words in the text, heading and label style, bullets against
  prose, a library name without its namespace, import style and line limits of `Suggested.lean`, a
  suggested extra Check, wording. One or two lines each. They never block.

An acceptance may carry small corrections and format notes; your summary then says so ("accepted;
two small corrections and one format note, none blocking").

### How to write each remark

1. **As a referee to a colleague.** The title is the claim, one sentence a mathematician would
   write ("The Check on the terminal object holds for every equivalence, so it cannot tell this
   functor from any other"). Then, in plain prose and in this order: why a reader or an implementer
   would be misled or stuck (one sentence, the consequence); how you know (the computation, the
   quotation, the declaration you opened, with its file and line); the smallest change that makes
   the text true. Give the place in words with the line number after it ("in the Check of 2.3,
   line 214"). No field labels (no `Location:`, `Claim:`, `Witness:`, `Repair:`).
2. **A scanner's message is never a reason.** The worker's automatic checks are evidence for you to
   look at, not remarks. If all you can say for a remark is that a check flagged it, drop it. Say
   what a reader loses, or say nothing. Never cite the worker's files (its check report, the job
   folder, the brief, a scratch file), a job id, a check's label (such as `prnum` or
   `readme-ids-missing`) or a local path: the worker refuses a review that does, and asks for the
   sentence instead.
3. **Raise only what you verified**: computed, read in the source at its locator, or opened in the
   library at the pin. What you suspect but could not check goes under `## Questions for the author`,
   as a question, with what stopped you. A locator or citation you did not open is never the
   subject of a required change.
4. **One remark, one issue.** Do not bundle a real point with matters of form: the real point is
   lost, and the author rejects the lot. Points of the same kind (five headings in one style) are
   one line, not five remarks.
5. **The smallest repair, never a deletion of the author's choices.** A repair changes the sentence
   that is wrong. It never restructures the document, renames or reorders sections, turns prose
   into lists, removes a section the author wrote to explain the scope, removes proposed names,
   deletes a credit, or deletes a statement kept as a sanity check, unless that content is itself
   false. Where it is a matter of taste, offer ("would", with options); do not prescribe.
6. **Fewer, weightier remarks.** A review is not better for being longer. Before you finish, reread
   each finding and ask whether a stated result would be wrong, unsupported or unusable without the
   change; if not, it is not a finding. If a remark would not make a careful human referee stop, it
   is one line under small corrections or format notes, or it is dropped.
7. **No template enforcement and no quotas.** The README's shape, its headings, the order of its
   sections (scope and supplier contracts at the top or at the end), the title or layout of the Lean
   file, and how many Checks a definition has, are never findings and never small corrections. A
   roadmap that is readable in another shape gets no remark about its shape.

### Examples (invented)

A remark that relays a scanner, and the same point said as a referee would:

- Bad: "R14 (P3) gate: identifiers missing. The README names `transfer_comp` and five others that are
  not declared; repair: remove them."
- Good, as a format note: "N03 Six names in code font (for example the transfer and its composition
  law in 3.2) are declared nowhere, so a reader who searches for them finds nothing and cannot tell a
  proposed name from an existing one. Say that they are proposed names, or declare them."

A bundle, and its one real point:

- Bad: "R16 (P3) Lean file form: the title should read 'Suggested statements', layers need section
  comments, nine definitions lack docstrings, and four results have different names in the README
  and the Lean file."
- Good, as a small correction: "C02 Four results carry one name in the README and another in the
  Lean file (the norm map of 2.1 is `normHom` in Lean), so a prose statement cannot be matched to its
  formal one. Use one name for each." The rest is form, and most of it is not worth a line.

A Check that tests nothing, and a quota:

- Bad: "R18 (P3) Checks: definitions 1.1 to 1.6 have fewer than three Checks each; add three per
  definition."
- Good, as a small correction: "C04 The Check of 1.4, that the functor sends the zero object to the
  zero object, holds for every additive functor, so it cannot tell this one from any other. One that
  could fail: on the cyclic group of order p it gives the regular representation." A Check that is
  false is a finding.

A statement true by definition:

- Bad: "R15 (P3) Padding: 4.2 follows by `rfl`; delete it."
- Good, as a small correction: "C05 Statement 4.2 holds by unfolding the definitions, so as a target
  it asks an implementer for nothing. If it is a sanity check that the two definitions agree, mark
  it as one; if a comparison with the library's notion is meant, state that isomorphism instead."
