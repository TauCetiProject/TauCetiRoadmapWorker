# Running TauCetiRoadmapWorker

This is the operator's and maintainer's guide. The contract is [DESIGN.md](DESIGN.md); the sources
rules are [SOURCES.md](SOURCES.md).

## Who does what

| Role | Does |
| --- | --- |
| **Humans** | File targets (issue template or an intention on the roadmap board), answer `needs-references` requests, review and merge the final draft pull requests. Nothing merges into the roadmap repository without a human. |
| **Maintainer** | Owns `tauceti-roadmap.toml`, `prompts/` and the merge-order file (CODEOWNERS); runs the board's maintenance (`board sync`, `board intake`, `board stale`), by hand or from a scheduled job with write access to the board. |
| **Operators** | Run workers on their own machines, with their own `gh` account and agent subscriptions. They need push access only to their own forks. |

## Setting up an operator

```bash
gh auth login                      # the account the worker acts as
codex login                        # and/or: claude auth login
uv tool install git+https://github.com/TauCetiProject/TauCetiRoadmapWorker
tauceti-roadmap --conf <toml path or raw https URL> doctor
```

`doctor` exits 1 on any failure and reports: Python, git, gh, the agent CLIs, `lake`, the `gh` login,
access to the content, board, packages and library repositories, which claims namespace your
workers will use, the merge-order file, the state directory, whether every brief renders, the PDF
text extractor, OCR (when enabled) and the sandbox.

**Sources.** The sources service extracts PDF text with `pdftotext` (install poppler-utils) or, more
slowly, with the optional `pdf` extra; OCR of image-only scans needs the optional `ocr` extra
(`uv tool install --force 'tauceti-roadmap-worker[pdf,ocr] @ git+https://github.com/TauCetiProject/TauCetiRoadmapWorker'`).
Texts you already hold go in the directories of `[sources] local_dirs` (write them as `~/...` in a
shared configuration); see [SOURCES.md](SOURCES.md).

**Lean.** The package lane elaborates `Suggested.lean` and the port, red-team fix and gate lanes run
the targeted build (`[gates] build`, default `lake build {module}`, never a bare `lake build`). Each
worker has its own content checkout; do not build Mathlib per worker. Prepare one built
`.lake/packages` (for example in a checkout of the roadmap repository where you ran
`lake exe cache get` once) and export its path:

```bash
export TAUCETI_ROADMAP_LAKE_PACKAGES=/path/to/TauCetiRoadmap/.lake/packages
```

Workers symlink it into their checkouts and worktrees. Without it, elaboration is skipped and the
lanes that need a build report so.

## Running a fleet

```bash
tauceti-roadmap --conf $CONF work --worker-id alice-review --only review,redteam --loop
tauceti-roadmap --conf $CONF work --worker-id alice-author --only design,package,fix --loop
tauceti-roadmap --conf $CONF work --worker-id alice-port --only port,fix,gate --agent claude --loop
```

* A configuration given as an https URL is read with retries (after 5, 20 and 60 seconds); the last
  good copy is kept in `<state>/config/<sha256 of the URL>.toml` and used, with a warning naming its
  age, when every try fails (a worker fails only when there is no copy at all). The `gh` login at
  startup is retried the same way.
* `--worker-id` names the worker: its directory, its branch prefix and its lease owner
  (`<login>/<worker-id>`). Use distinct ids for concurrent workers.
* `--only` pins a worker to some kinds (`target, design, package, review, fix, port, redteam, gate`);
  without it the worker takes whatever is most useful.
* `--agent` pins the agent family. Without it, each job runs on its lane's configured agent
  (`[agents.lanes.<kind>]`, default `[agents] default`), and a red-team pass on the agent it requires,
  if that agent is installed.
* The model comes from the job and its lane: a red-team pass requires the model of its pass
  (`[redteam] passes`), a review the model `[agents.lanes.review]` pins for the worker's family
  (`codex_model`, `claude_model`: Astra on Codex, Fable on Claude), and the authoring lanes use the
  family's default model. `--model` pins the model as well: such a worker skips every job that needs
  another model, with a line like `skip REV-PKG-X: REV-PKG-X needs gpt-6-astra or fable; this worker is
  pinned to gpt-6.1-sol`. A worker pinned with `--agent` alone lets the lane pick the model. `doctor`
  warns when neither family of the review models is installed on the host.
* `--loop` keeps going: after a usage limit it sleeps until the reset time the agent printed; when
  nothing is eligible it sleeps `[worker] idle_seconds` (default 600). The first poll after a
  failure comes within a minute, and while a job it parked for references waits, it looks every
  minute whether the board answered it and polls at once when it did. Without `--loop` it does one
  round and exits.
* `--dry-run` takes no lease, writes the brief to the job folder and prints every GitHub write.

**Watching.** `tauceti-roadmap tail --worker-id <id> [--job <JOB>]` follows a worker's round log
(`workers/<id>/rounds.log`: every line the worker prints, timestamped) and the live output of the
agent on its current job (`jobs/<JOB>/run-N.out`), moving to the next job when the worker starts
one; Ctrl-C stops it. `tauceti-roadmap status [--all]` lists the board's jobs by state with their
age since the issue last changed and, for claimed jobs, the lease's owner and how long it has
held the job.

When the worker's checks of a run's deliverables fail (the plan's schema, the report's shape, the
manifest, the privacy and source-material rules), the agent gets one repair run in the same
workspace, with the problems appended to its brief ("Repair these problems in place; change
nothing else"), before the job fails; the two runs count as one attempt. When a job is retried on
the same worker, its brief points at the earlier attempt's `problems.txt` (kept as
`problems.previous.txt`) and its `unsubmitted/` folder, so the agent repairs instead of starting
over. A design that needs something a supplier has not written yet says so with a `gap:<slug>`
prerequisite and a `gaps` entry naming the supplier and its section; the intake posts each such gap
on the supplier's open job on the board (or on the design's own issue when it has none).

A round that stops before it submits or parks its job (the lane fails, git or GitHub raises, the
agent hits a usage limit, or you press Ctrl-C) releases the lease and puts the job back to
`state:available` with a "Released by worker" note. A fault before any agent runs (a brief that does
not render, a checkout that fails) is on the operator's side: the job is released as
`setup-failed`, not counted against it, and the worker process leaves it alone until it restarts.

**Upgrading.** At startup a worker copies the briefs and issue templates of its build into
`workers/<id>/build/<version>+<hash>/` and renders only from that copy, so reinstalling the tool
(`uv tool install --force ...`) under a running worker cannot mix new briefs with old code. At
every round boundary it compares the installed build with the one it started on; when they differ
it logs `installed build changed (old → new); exiting for restart` and exits 0, so run workers under
a service that restarts them (`Restart=always`), or start them again by hand. A job that fails three times on one worker is
skipped by that worker (the count is in `workers/<id>/attempts.json`; delete the job's entry to let
the worker try it again); other workers may still try it. The design lane is capped per
operator per day (`[design] daily_cap`), counted in `design-ledger.jsonl` on the host.

## The job graph

```text
target ──triage──▶ DESIGN-Name ═══ board PR (needs-review) ════════════▶ merged ──▶ PKG-Name ═══ board PR (needs-review) ══╗
                         REV-DESIGN-Name ─accepted─┘                                   REV-PKG-Name ─accepted─┐          ║
                          │ needs_changes ▲                                             │ needs_changes ▲     ▼          ║
                          └▶ FIX-DESIGN-Name~2 (pushes to the PR) ┘                       └▶ FIX-PKG-Name~2 ┘  RT-Name~1a, RT-Name~1b
                                                                                                              ▼          ║
                                                                                                FIX-RT-Name~1 (pushes)  ║
                                                                                                              ▼          ║
                                                                                        RT-Name~2a, ~2b ─▶ FIX-RT-Name~2 ║
                                                                                                              ▼          ║
                                                                           PORT-Name ◀── merged ◀════════════════════════╝
                                                                              │ roadmap PR (draft, human reviews only)
                                                                              ▼
                                                                  GATE-Name~k (branch owner) ─▶ reply
```

A design or package submission is not merged right away: its board pull request stays open, labelled
`needs-review`, through its reviews, their fix rounds (pushed to its branch by its owner) and, for a
package, the red-team rounds; every review or pass is a comment on it (see "Reviewing from a browser
model" below for the shape). After the last red-team fix the intake merges it and creates the port.

**Consolidation.** Before a target becomes a design, its triage compares it with every other open
target and every open design on the board (names, statements, and the triage or design files that
name their suppliers; the brief lists them). Besides `design`, `part-ii` and `duplicate`, the
triage may answer `Verdict: merge-into <TARGET-Name or DESIGN-Name>` when the target overlaps that
job. The intake then closes the target with the `merged-into` label and a link, and adds its
statement and sources to the surviving job's inputs (`absorbed`); a target that merges into
another target reaches that target's design brief, which lists every absorbed statement; a target
merged into an open design (one narrower than the design) is also posted on the design's issue as
a note for its author. A merge into a job that is not an open target or design is parked for a
human.

Job ids follow `KIND-NAME~roundN`. Round 1 of a design, package, review or design/package fix has
no suffix; red-team passes (`RT-Name~1a`), red-team fixes (`FIX-RT-Name~1`) and gate rounds
(`GATE-Name~1`) always name their round. A review that does not accept sends its artefact to a fix
round, and every fix round is followed by a fresh independent review; after `[board] max_rounds`
the roadmap is parked (`state:blocked`) for a human.

States are the labels `state:available`, `claimed`, `submitted`, `done`, `blocked` and
`needs-references`. A job issue carries exactly one: every state change adds the new label first and
then removes every other `state:` label the issue carries at that moment. `board sync` repairs an
issue that still carries several (a removal failed, or two writers raced) from the issue's events:
the label added last is what the latest writer meant, so `done`, `blocked`, `needs-references` or
`submitted` added last stands, and when the last write moved the job to `claimed` or `available`
(out of a holding state too, such as an unblock) the lease decides between those two. Without the
events, a holding state wins (`done`, `blocked`, `needs-references`, `submitted`, in that order). Every job issue
carries a machine-readable spec in an HTML comment; the rest of the issue is for humans and is never
read as an instruction.

## Lanes

| Kind | Reads | Writes | Who may take it | Checked before submission |
| --- | --- | --- | --- | --- |
| target | the filed form (quoted as data), roadmaps, open roadmap PRs, packages | `packages/<Name>/target.md`, `sources.toml` | anyone | Verdict/Name/Base/Owner header, sources manifest if changed |
| design | `target.md`, roadmaps, libraries, merge order | `design/README.md`, `design/plan.json`, `sources.toml` | daily cap | plan.json schema, sources manifest, residue, dependency check |
| package | accepted design and its review | `README.md`, `Suggested.lean`, `metadata.toml`, `sources.toml` | anyone | every text gate, sources manifest, elaboration |
| review | the board PR's head (read-only) | a comment on the board PR in the review shape (from its report and verdict) | not an author's operator (or, with `separation = "agent"`, not the authoring agent and model) | verdict schema, report header, reviewed files untouched |
| fix (design/package) | the review's comments | commits pushed to the board PR's branch | its owner (a fresh review follows) | finding sections, gates |
| redteam | the board PR's head (read-only) | a comment on the board PR in the red-team shape | the agent and model the pass requires | report header, checkout untouched |
| fix (red team) | the round's pass reports | commits pushed to the board PR's branch | its owner | finding sections, the package's gates, elaboration, the targeted build in a roadmap checkout |
| port | the merged, red-teamed package | branch `roadmap/<kebab-name>` on the operator's fork, the roadmap PR; the merge-order file when it places the roadmap | anyone | commit made, full gate with build, synopsis |
| gate | every review since the last reply | commits on the PR branch, a reply, labels | the PR branch owner | finding sections, clean tree, full gate with build, head check |

Every lane writes its report to `packages/<Name>/reports/<JOB>.md` on the board, so the next lane
(often another operator) can read it. The roadmap's `sources.toml` is a deliverable of every job that
works on its sources (target, design, package, review, and the design and package fixes); the lanes
on the content repository (port, red team, red-team fix, gate) submit only their report. The port lane composes the pull-request description from
computed facts: the synopsis from the report, the dependencies on other open roadmap pull requests,
the merge-order chain and a validation paragraph from the gate result. `merge-order` refreshes the
chain paragraph in every open roadmap pull request, and the port lane writes the new pull request's
with the same renderer; it never states a position number. The paragraph reads

    **Merge order.** #a (Name) → #b (Name) → **this pull request** (#n). It depends on no other author's open pull request. Roadmaps already on main are not listed.

(or "No open pull request of ours must merge before this one; **this pull request** (#n) can merge
as soon as it is approved." when no open roadmap pull request of the project must come first). The
chain is the transitive closure of the roadmap's suppliers among those pull requests, in merge-order
rank. Other authors' pull requests are read from the content repository: an open pull request the
port lane did not open counts when it adds a roadmap folder that is not on main and the README cites
that roadmap as a supplier; it is listed as `#248 (LFunctions, roed-math)` in "It also depends on
other authors' pull requests that add roadmaps it cites, which must merge first: ...". When those
pull requests cannot be read the sentence is left out, never replaced by "no other author's".
A roadmap missing from the merge order gets no paragraph.

Only the operator who opened a pull request can push to its branch, so the fix rounds on a board
pull request and the gate rounds on a roadmap pull request name that operator (`requires_operator`).
The red-team passes of one round name different agents and models (`requires_agent`,
`requires_model`), from `[redteam] passes`.

### Reviewing from a browser model

A browser model such as ChatGPT Pro reads GitHub and leaves comments, and that is all a review on
the board needs. The steps for the human who drives it:

1. Open the pinned "Review queue" issue on the job board (`board sync` keeps it current): it lists
   every review and red-team pass open on a board pull request, with links.
2. Pick a pull request and give the model its link. The pull request's first comment is the brief:
   the job, the files at the head (raw links), the principles, what to check, and the shape of the
   review. The model holds no sources: it samples locators against legal copies it can open itself
   (arXiv, HAL, Numdam, GDZ, publisher open pages) and marks the rest as not verifiable.
3. Optionally post `reviewing` (one word) first, to claim the review so that no worker starts it.
4. Post the review as one comment: first line `Review (<agent>) of <sha7>` (for a red-team pass,
   `Red team round <N> pass <x> (<agent>) of <sha7>`), then `Verdict: accepted`, `needs_changes` or
   `rejected`, then numbered findings `1. **F01** (P1) <title>` with location, claim, witness and
   repair, then `## Verified sound` and `## Not verifiable`.

The board reads such comments only from the logins in `[review] comment_reviewers` (and from the
workers that claimed the job), only of the pull request's current head (a review of an older head is
answered once, as stale), and only when the reviewer is independent of the authors in the
configured separation mode (a browser model's agent name counts as its agent). A worker that
already holds a review is not preempted: the board waits for its review, and both count.

With `[redteam] sharded = true`, every red-team job runs its whole-document pass and then one section
pass per README section that states targets (the `## Layer` sections, or the `### Layer`
subsections of a single `## ` wrapper; framing sections such as the scope, conventions, references
and provenance are skipped). Each section pass instantiates every statement of its section on
small examples (brief `prompts/redteam-section.md`). At most `[redteam] shard_max` section passes run
per job; beyond that, adjacent sections are grouped, balanced by length. Their findings are
renumbered after the job's own, tagged `[§k Section]`, and merged into the job's report before the
fix lane reads it. A sharded job costs one agent run per section more than a whole-document pass.

Every lane makes the texts its roadmap's `sources.toml` records available in the sources cache before
the agent starts and lists their line-numbered files in the brief; agents fetch new sources through
`tauceti-roadmap sources fetch` (see [SOURCES.md](SOURCES.md)). Sources live only on the operator's
machine: the cache under the state directory is their only copy, and every submission gate refuses
source documents, extracted text, manifests that embed text and fenced passages of more than 40
lines (see "Sources never enter a repository" in SOURCES.md).

### Machine paths

Agents are told to write repository-relative paths only. As a safety net, after the agent runs and
before any check, the worker rewrites machine paths in the job's hand-off files (its report,
`target.md`, the verdict files and `sources.toml`) and in the references request it posts: the
project state directory becomes `<state>`, the home directory `~`, and a `/tmp/` path
`<scratch>/`. README and Lean content is never rewritten: a machine path there is a defect, and the
privacy gate refuses the submission ("local path in ...") as it refuses any path left elsewhere.

### Every round starts from the board

A worker never carries files from one round to the next in its checkouts. Each round fetches the
main branches of the board (packages) and content repositories and checks them out with `--force`:
edits, untracked files and, inside the roadmap's package folder, ignored files that an earlier
round left behind are discarded, and the board's copy wins. Whatever the last round left in the
board checkout beyond the board's main (uncommitted, or committed by a submission whose push or pull
request may have failed) is first copied to `workers/<id>/jobs/<JOB>/unsubmitted/` (the job whose
branch was checked out) for the operator to inspect. A lane gets those files back only if it
declares that it resumes a checkpoint (`resumes_checkpoint`; no lane does today), and only for the
same job.

The rule for lanes and their briefs follows: **anything a lane writes before a submission or a
parked request is either submitted or reproducible from the board.** Deliverables reach the board
only through the submission pull request; a `needs-references` request lives in its comment (with
its marker) and the answers in the board's `sources.toml`; fetched texts are in the sources cache by
hash and are fetched again from the URLs the manifest records. A parked job is therefore run again
from the start once it is answered, on the board's current files.

## Campaign-specific rules

A project adds its own rules without touching the code or the shared briefs, through three hooks in
`tauceti-roadmap.toml`:

| Hook | What it does | Where the file lives |
| --- | --- | --- |
| `[gates] forbidden_patterns` | regular expressions that must not match a roadmap's README.md or Suggested.lean; checked by the package lane, by every push gate and by `tauceti-roadmap gate` (count `forbidden`) | in the toml |
| `[gates] dropped_names_file` | roadmap names that must never be cited (dropped or abandoned roadmaps), one per line, `#` comments; checked by the same gates (count `dropped`) and listed in every brief's dependency section | relative to each host's state directory |
| `[prompts] extra_rules_file` | Markdown appended to every brief under "Project rules" | relative to the packages repository (commit it there, CODEOWNERS-protected) |

**Worked example: a project decides that no roadmap targets the abc conjecture or its explicit forms.**
The rule has three parts: the gate catches the names in the files, the dropped-names file removes a
roadmap that was started on it, and the brief tells authors and reviewers why.

```toml
[gates]
# Single-quoted TOML strings keep the backslashes of the regular expressions.
forbidden_patterns = [
  '(?i)\babc[ -]conjecture\b',                    # prose: "the abc conjecture", "ABC-conjecture"
  '(?i)\b(?:explicit|effective|uniform) abc\b',    # its named forms
  '\babc_[A-Za-z]\w*',                             # Lean names: abc_conjecture, abc_effective_bound
]
dropped_names_file = "dropped-roadmaps.txt"

[prompts]
extra_rules_file = "rules/project-rules.md"
```

`dropped-roadmaps.txt`, in the state directory of every host that runs workers or the gate:

```text
# Roadmaps the project dropped; never cite them.
AbcConjecture
```

`rules/project-rules.md`, committed to the packages repository:

```markdown
- No roadmap targets the abc conjecture or any explicit, effective or uniform form of it, and no
  roadmap plans a route whose purpose is one of them. A statement that would serve only such a route
  is cut; say so in the report.
- Results of this family may be mentioned as background in prose; they are never targets, never
  `Suggested.lean` declarations, and never cited as suppliers.
```

Check the patterns before you rely on them: a configuration with an invalid regular expression is
refused at load time, and `tauceti-roadmap --conf $CONF gate <worktree> <Name>` on a roadmap that
mentions the family reports `forbidden=N` with the offending lines. Patterns are searched in the
whole text of both files, Lean comments included, and each match is reported with its line, so keep
them specific (a bare `\babc\b` would
also flag an unrelated variable named `abc`). The dropped-names file and the rules file are read
afresh for every job; a change to the toml itself (the patterns) reaches a `--loop` worker when it
restarts, since the configuration is loaded once per start.

## Claims and separation

Leases use the TauCetiTools format (TauCetiWorker `scripts/claim.sh`): refs
`refs/tauceti-claims/roadmap/<project>/job/<JOB>` updated with an atomic compare-and-swap push. The
namespace is `$CLAIM_REPO` if set, else `[repositories] claims` when your account can push there,
else your own fork (then your workers de-duplicate only among themselves). Leases are cooperative
and fail-open: a lease that cannot be registered is logged and the round proceeds. A worker id
runs one process at a time: at its start a worker releases any lease its previous process left
behind (killed before it could release, or crashed) and puts those jobs back to `state:available`;
and `board sync` puts back every claimed job whose lease has expired (the worker stopped renewing
it), without waiting for `board stale`.

When a design, package or fix is submitted, the worker also records an author lease
(`roadmap/<project>/author/<JOB>`, with the operator login, agent and model inside). A review job
lists every job that wrote the round it reviews (`independent_of`); a worker refuses it when its
operator authored any of them, and the intake refuses a submission whose GitHub author did (it reads
the board's own "Intake: merged #N from @login (agent, model)" notes, which only the board's logins
can write). With `[review] separation = "agent"` (a project with a single operator) the authoring
operator may take the review when the reviewing agent and model differ from the authoring ones
recorded for the job (an author record without its agent and model never counts as different), and
the intake applies the same test to the submission's agent and model. Projects with several
operators keep the default `"operator"`. A fix worker's own acceptance never counts.

## Board maintenance

Run with write access to the board (a scheduled workflow, or the maintainer). Every command is a dry
run unless `--yes` is given.

```bash
tauceti-roadmap --conf $CONF board sync --yes      # targets → TARGET jobs; one state label per job;
                                                   # expired leases released, live ones → state:claimed;
                                                   # PR labels;
                                                   # GATE jobs when humans await a reply;
                                                   # the pinned "Review queue" issue;
                                                   # held PRs back to draft
tauceti-roadmap --conf $CONF board intake --yes    # take in submissions (designs and packages stay open
                                                   # for review), answer needs-references jobs, then read
                                                   # the reviews and passes on the board's open PRs
tauceti-roadmap --conf $CONF board references --yes   # only the needs-references answers
tauceti-roadmap --conf $CONF board stale --release --yes   # release lapsed claims and closed submissions
tauceti-roadmap --conf $CONF merge-order           # refresh the chain paragraph on open roadmap PRs
tauceti-roadmap --conf $CONF targets import-intentions --yes   # intentions → TARGET jobs (10 per run)
tauceti-roadmap --conf $CONF board import-explorer --from ~/tauceti-explorer --yes   # see below
```

A roadmap that reaches the port without a place in the merge order (an imported package, say) is
placed by the port lane: right after its last supplier that the order lists (its README's supplier
lines, as the dependency check reads them, and every `TauCetiRoadmap.<X>` its Lean names), at the end
when it has none, with a warning for a supplier the order lacks. The edited `ORDER.txt` is part of
the port's submission, so the intake merges it with the report. When the place would put one of
its suppliers after one of its consumers, the job is set to `state:blocked` with the cycle in a
comment, and nothing is submitted.

### The board workflow

The board's maintenance need not depend on anyone running it: copy `board/board.yml` from this
repository to `.github/workflows/board.yml` in the jobs repository. Every ten minutes, and on every
pull request, issue and comment event of the board, it installs the tool from this repository's
main and runs `board intake --yes`, `board sync --yes` and `board stale --release --yes` with the
workflow's own token (permissions: contents, pull requests and issues write; one run at a time).
Everything the board writes works with that token: its notes are signed by `github-actions[bot]`
(keep it in `[board] bot_logins`), and authorship always comes from the submission pull request's
author.

Two things that token cannot reach are skipped, not failed. The claims namespace is another
repository: `board intake` reads it to wait for a worker that holds a review (and to take a lease
for a `reviewing` comment), `board sync` to mirror live leases and release expired ones, and
`board stale` to spare a claim whose lease is live; without access they see no leases (the claim is
still made on the issue, and `board stale` falls back to the issue's inactivity). And the roadmap
repository: `board sync` flips its pull requests' labels and keeps held ones as drafts only with a
token that can write there, so a maintainer's own runs (or a token with that access) cover those;
the workflow notes what it skipped. Team membership for `[board] target_filers` comes from
`board/filers.json`, which every run with a maintainer's token keeps current.

The intake merges a submission only when it names an open job, changes only that job's deliverables
and report, contains no local path, has valid JSON and TOML, its checks passed, and its author is
independent where required. When a design is accepted it inserts the roadmap into the merge-order
file after its last supplier. With `[redteam] ready_after = true` the last red-team fix marks the
pull request ready for review; otherwise it stays a draft for a human to mark ready.

Pull requests listed in `hold-draft.txt` in the state directory stay drafts: `board sync` converts
them back if anything marks them ready.

For every job parked in `state:needs-references`, the intake reads the humans' answers after the
latest request (source, local, waive; see [SOURCES.md](SOURCES.md)), records them in the roadmap's
`sources.toml` on the packages repository, and makes the job available again once every key is
answered. Its notes carry a progress marker, so running it every few minutes repeats nothing.

### Importing from the explorer

`board import-explorer --from <checkout of CBirkbeck/tauceti-explorer> [--kinds package,design,target]
[--names A,B] [--yes]` carries the explorer's roadmap work onto this board, once per roadmap, and
prints a table of what it did (a dry run without `--yes`). It reads `research/blueprint/queue.json`
(job states), `issues.json` (explorer issue numbers), `packages/<Name>/` (with its `review.json`),
`readmes/<Name>.md`, `packets/<Name>.json` and the design reviews in `reviews/`. Per roadmap:

| Explorer state | Board job | Files carried to `packages/<Name>/` |
| --- | --- | --- |
| package accepted by an independent review (reviewer `independent-review-...`) | `PORT-<Name>` | README.md, Suggested.lean, metadata.toml, review.json |
| package accepted only by its fixer (reviewer `fix-...`), or not reviewed yet | `REV-PKG-<Name>` | README.md, Suggested.lean, metadata.toml |
| design accepted by its review, not packaged | `PKG-<Name>` | design/README.md (the design), design/plan.json (the packet) |
| design written, its review not done | `REV-DESIGN-<Name>` | design/README.md, design/plan.json |
| design queued, not written (`--kinds target`) | `TARGET-<Name>` | none |

A fixer's acceptance never counts: such a package gets its independent review here. Skipped, with
the reason in the table: a roadmap already ported (its folder on the roadmap repository's main,
active or completed, or an open pull request there adding its README; the table names the pull
request), a roadmap with an explorer job `external` or claimed (in flight), a review
that asks for changes or has no readable verdict, a roadmap the board already has a job for, and one
waiting in an open import pull request. Every job records the explorer job and issue it rests on
(`inputs.explorer`), so reruns import nothing twice.

Files never reach the board by a direct push: each roadmap gets one pull request from your fork,
after the submission gates have passed on its files, with a marker naming the job. `board intake`
merges an import pull request only from an owner, member or collaborator, only with the marker's
files under the named roadmaps' folders and passing checks, and only then creates the job. A
TARGET job carries no files and is created at once.

`targets import-intentions` turns open issues of the content repository that carry every label of
`[board] intention_labels` into TARGET jobs, oldest first, at most `--limit` (default 10) per run,
or one with `--number N`. Each intention is imported once: the mapping is kept in `intentions.json`
in the state directory, and an intention a board job already names is never imported again even if
that file is lost. The intention's text is quoted into the job as data; the triage decides whether
it asks for a new roadmap, a Part II, or is owned already.

## The state directory

`$TAUCETI_PROJECT_STATE`, else `$XDG_STATE_HOME/tauceti/projects/<project>/`, else
`~/.local/state/tauceti/projects/<project>/`. Files that used to live in `/tmp` on the maintainer's
machine are here:

| Path | What |
| --- | --- |
| `workers/<id>/board`, `content` | the worker's checkouts of the packages and content repositories |
| `workers/<id>/port/<Name>`, `pr/<Name>` | worktrees for the port lane and for work on a pull request branch |
| `workers/<id>/jobs/<JOB>/` | brief, agent logs (`run-N.out` with START/EXIT lines), scratch, problems, gate result |
| `workers/<id>/attempts.json` | failures per job on this worker |
| `workers/<id>/rounds.log` | every line the worker printed, timestamped (what `tail` follows) |
| `workers/<id>/jobs/<JOB>/unsubmitted/` | what the job's last round that left anything left in the board checkout beyond the board's main, set aside before the next round reset it |
| `rev/<pr>/` | read-only checkouts of open roadmap pull requests at their heads (was `/tmp/rev/<pr>`) |
| `prs.json` | open roadmap pull requests: number, roadmap, head, branch, owner (was `/tmp/rev-list.txt` and `/tmp/watch-prs`) |
| `claims/<repo>.git` | scratch object store for lease refs |
| `design-ledger.jsonl` | design jobs started per operator per day |
| `hold-draft.txt` | pull requests that must stay drafts (was `/tmp/hold-draft.txt`) |
| `stacks.toml` | stacked pull requests, `child = parent` (was hard-coded in merge_order.py) |
| `external-deps.json` | other authors' roadmap pull requests a pull request depends on that the scan cannot see, added to what it finds (was `/tmp/ext-deps.json`; ext_deps.py's shape is read too) |
| `other-prs.json` | the roadmap folders other authors' open pull requests add, by head (the scan's cache) |
| `dropped-external.txt` | other authors' pull requests that never count as dependencies, one number per line (was `decycle/DROPPED_EXTERNAL.txt`) |
| `mutual.txt` | `<pr> <other-pr>` pairs that cite each other: no whole-pull-request order between them, and an acyclic stage-level graph (was `decycle/MUTUAL.txt`) |
| `sources/<sha256>.<ext>`, `sources/<sha256>.txt` | the sources cache: fetched files and their line-numbered texts (see SOURCES.md) |
| `sources/local-index.json` | hashes of the texts in `[sources] local_dirs`, by path, size and modification time |
| `intentions.json` | intentions already imported: `<owner>/<repo>#<n>` → TARGET job and board issue |
| `workers/<id>/jobs/<JOB>/scratch/needs-references.toml` | the job's open request (`.previous.toml`: the one a resumed run archived) |
| `tauceti-roadmap.toml` | the saved copy of a configuration given as a URL |

`[gates] dropped_names_file` and a `merge_order_repo = "local"` order file are also relative to it.

## The sandbox profile (optional, off by default)

The bundled files are in the installed package's `sandbox/` folder.

* **proxy**: run `python3 -I sandbox/allowproxy.py` (127.0.0.1:3128 and a unix socket). It allows
  and logs every host except the deny lists (exfiltration channels and pirate mirrors, plus your own
  `deny.txt` in `~/.local/state/tauceti/allowproxy/`). Set `[sandbox] profile = "proxy"`.
* **netjail**: build `cc -O2 -Wall -o netjail sandbox/netjail.c`, grant it user namespaces with
  `sandbox/netjail.apparmor` on Ubuntu, set `profile = "netjail"` and `netjail = "<path>"`. The agent
  then runs in a private network namespace whose only route out is the proxy. If the jail cannot
  start, the worker falls back to the proxy profile.
* **Claude hook**: with `claude_hook = true`, Claude agents get a PreToolUse hook (`url-guard.py`)
  that refuses to execute fetched content, raw network tools, proxy changes and credential reads.

Agents always run with a minimal environment: no GitHub token is passed to them; the worker makes
every GitHub write itself after checking the result. TauCetiWorker's `--bubble` sandbox is the
alternative for operators who already use it.

## What a human does when

* **A target**: use the issue template on the board (or `targets file`). For now only the logins and
  teams of `[board] target_filers` file targets (here the members of the TauCetiProject `humans`
  team, and `roed-math`): `board sync` closes a target issue from anyone else with one comment and
  the `not-a-filer` label, and `targets file` refuses. Team members are read through the API and
  kept for an hour in the state directory; a token that cannot read the organisation (the board
  workflow's) uses the last list it read, then `board/filers.json` in the board repository, which
  every run that can read the team keeps current; with no list at all nothing is closed. `board sync` turns it into a
  `TARGET-Name` job; the triage decides new roadmap, Part II, duplicate, or a merge into another
  open target or design. Many targets at once: `targets file-many --from targets.toml --yes` (a
  TOML file of `[[target]]` tables or a JSON Lines file, with `statement`, `why` and optionally
  `sources`, `name`, `licence`, `title`); an entry whose roadmap name the board already has, as a
  target issue or a job, is skipped, so a list can be filed again after it grows.
* **A `needs-references` request**: answer each key in a comment on the job, one line each:
  `source: KEY <DOI, arXiv id or URL>`, `local: KEY <file name>` (operators hold the text in
  `[sources] local_dirs`; only a worker whose operator holds it resumes the job), or `waive: KEY`
  (the citation is then cited by its locator alone, as `waived`). Never attach a file: sources never
  enter a repository or an issue, and attachments are not downloaded. The board's intake records the
  answers and makes the job available again once every key is answered.
* **An intention** on the roadmap repository becomes a TARGET job when the maintainer runs
  `targets import-intentions`; there is nothing else to do.
* **A draft pull request**: review it on GitHub. Your comments feed the gate lane; the reply answers
  every point, and the labels flip between awaiting-author and awaiting-review. Merging is yours.
* **The configuration and the prompts** are CODEOWNERS-protected; changes go through review.

## Known limits of this version

* The harness runs standalone; running the lanes as TauCetiWorker `only=` kinds, with its quota
  pacing, dashboard and Bubble sandbox, is the intended integration.
* Two workers of the branch owner may run a red-team fix and a gate round on the same pull request
  at once; the branch push uses `--force-with-lease`, so one of them fails and retries rather than
  overwriting the other.
* A job parked for references is run again from the start once answered; its earlier partial work
  stays only in that worker's job folder (`unsubmitted/`).
* A `local:` requirement is recorded on the answered job only: a later job of the roadmap (its
  review, say) that needs the text runs on any worker and lists the text as unavailable when its
  operator does not hold it.
* The weekly digest issue of open `needs-references` requests is not implemented; use the label.
* The intake reads a request only from the board's logins and `[sources] request_associations`: an
  operator with no association on the board yet (before a first merged submission) has their request
  ignored, and a maintainer reposts it with `sources request ... --post`.
* Which README sections get a sharded red-team pass is a heuristic on the headings (layers, and
  sections with bold-labelled statements, numbered subsections or Lean, minus framing sections).
* The dropped-names file lives in each host's state directory, so every operator keeps a copy.
