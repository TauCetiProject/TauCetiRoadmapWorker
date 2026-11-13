## Copyright, licences and scope

A theorem, a definition and the idea of a proof belong to nobody: a roadmap may state them. Their
expression does not: a source's sentences, its tables and figures, its exercises, and its selection
and arrangement of the material (its table of contents) are its author's or its publisher's, whatever
the copy you read cost. A roadmap is published under the project's own licence, so none of it may be
a source's expression. Check this in three parts, then write the two things below.

1. **Wording.** For every source locator you verify, and for at least five further passages of
   prose taken from different sections (the wording of a definition, a remark, a proof plan, a
   docstring of `Suggested.lean`), put the roadmap's text beside the source's at its locator. A
   sentence, a displayed list or a proof plan that reproduces the source's wording, or that follows
   it clause by clause with words exchanged, or that translates it, is a finding (P2). The witness is
   the locator (`<source id> line N`) and the number of consecutive words shared: never quote the
   source's passage beyond the 40 words of a witness. The repair is the passage written again in the
   roadmap's own words: write it. What the mathematics forces is not copying: a formula, a standard
   name, a list of hypotheses that can be written in one way only, a statement of a dozen words.
2. **Arrangement.** Count the README's source locators by source and name the source with the
   largest share. Open its table of contents (its text is in the sources cache, or its contents page
   is public) and set the roadmap's layers and sections beside its chapters and sections. A roadmap
   whose layers or sections map one to one, in the same order and with the same scope each, onto a
   run of one source's chapters or sections follows that source's table of contents: P2, also when
   the headings were renamed. The witness is the list of pairs, by number only (`Layer 2 ↔ Ch. III`).
   The repair is the reorganisation: which layers to merge, split or reorder so that each is what the
   final targets need, and at least two further sources, which you name with locators, for the main
   results. An order that the dependency lines force is no finding; the scope of each layer must
   still be the roadmap's own, and you say which dependency lines force the order. A roadmap of one
   research article's main theorem cites that article most: the share is no finding, the mapping of
   its sections is.
3. **Reuse.** Material taken over from a source as it stands (a table, a figure, a list of examples
   or of exercises, numerical data), even with a citation, is a finding (P2) with the repair "state
   the facts the roadmap needs in its own form, with the locator". A declaration of `Suggested.lean`
   whose name, binder names and statement agree with a public formalisation other than the pinned
   libraries is reported to the human reviewers (below), with the repository and the file: a licence
   is theirs to read.

Close the check with one line, `Source independence:`, giving the number of passages you compared
and with how many sources, the source with the largest share of the locators and that share, and
the result of the comparison of contents: for example `Source independence: 14 passages compared
with 5 sources, none shared; largest share <source id>, 41 of 96 locators; contents compared with
<source id>: independent`. When a text or its table of contents could not be read, say which, here
and to the human reviewers. An acceptance carries the line.

### What a human decides

Some questions are not yours to settle. They are not findings: they change neither the verdict nor
the count of findings, and you ask the author for nothing. Write them for the humans who approve,
in the report's last section, `## For the human reviewers`: one bullet each, at most 40 words, with
the fact you observed and where (a file and line, a source id). Write `- none` when there is
nothing: every report has the section. State facts and never a judgement of importance
or of taste: the human decides. Flag:

- **Scope.** The project plans mainstream research mathematics: subjects with a body of published,
  refereed literature that current research builds on. Flag a roadmap you cannot place there, and
  say which of these holds: its main sources are unpublished, private, rediscovered or posthumous
  notes, or manuscripts of one author that the published literature does not take up; its subject
  is recreational, school or competition mathematics, or a set of exercises; its headline theorems
  serve no later theory that you can name and no refereed source states them; it is not mathematics
  (software, numerical experiments, a history of a subject). Give the evidence: the sources by id,
  what you searched for and did not find.
- **Licence.** A source or a reuse whose status you could not settle: a copy whose host you could
  not classify, a source that is not published, reused material with no stated licence, a
  formalisation the Lean seems adapted from.
- **Not tested.** The source with the largest share of the locators, when its text or its table of
  contents could not be read: parts 1 and 2 were not checked against it.
