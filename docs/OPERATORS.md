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
slowly, with the optional `pdf` extra; OCR of image-only scans (GDZ, most author-page scans) needs
the optional `ocr` extra, installed with headless OpenCV
(`uv tool install --force --excludes https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapWorker/main/ocr-excludes.txt 'tauceti-roadmap-worker[pdf,ocr] @ git+https://github.com/TauCetiProject/TauCetiRoadmapWorker'`;
see [SOURCES.md](SOURCES.md#ocr-of-image-only-scans)).
Texts you already hold go in the directories of `[sources] local_dirs` (write them as `~/...` in a
shared configuration); see [SOURCES.md](SOURCES.md).

**Lean.** The package, package-review and package-fix lanes elaborate `Suggested.lean`
(`[gates] elaborate`, default `lake env lean {file}`) in the worker's content checkout, and the
port, red-team fix and gate lanes run the targeted build (`[gates] build`, default
`lake build {module}`, never a bare `lake build`) in a worktree. Elaboration needs `lake` on the
PATH (elan installs the pinned toolchain on first use) and the machine's shared Lake packages for
the checkout's pin, `<state>/lake/<pin>/packages`, which every checkout and worktree links as its
`.lake/packages` and which the first lane to need them fills with `lake exe cache get` once, under
`<state>/lake/<pin>/.lock`, marking `.ready`; that first fetch does not start below 20 GB free on
the state directory's disk (the disk guard), while a ready pin costs nothing. When elaboration still
cannot run (no `lake`, the fetch failed, the guard tripped) the lane never skips it silently: the
brief's `Elaborate` line says why, the worker's check adds the problem
`Suggested.lean: not elaborated — <reason>`, the package lane's repair pass receives it and the
round fails with it. Before any elaboration the text gates refuse a `Suggested.lean` whose imports
are not `import Mathlib` followed only by the individual `TauCeti.*` modules actually used (count
`imports`: never `Mathlib.<submodule>` lines, no roadmap module, no duplicate, nothing below the
header, and no `TauCeti.*` module missing from the pinned Tau Ceti's sources when the checkout
holds them) or that carries a `set_option` (count `set_option`); an elaboration that fails on a
missing object file with an import that is neither Mathlib nor an existing `TauCeti.*` module is
explained in its problem line. The shared packages hold Tau Ceti's sources without object files,
so a `Suggested.lean` that imports `TauCeti.*` modules is elaborated through the targeted build of
its module (`[gates] build`, the command the port lane runs): the package's file is synced to
`<active_dir>/<Name>/Suggested.lean` in the content checkout for the build's duration, the build
runs under the pin's lock, the disk guard and a timeout, and the Tau Ceti object files it makes
land in the shared packages for every worker; a Mathlib-only file keeps `[gates] elaborate`
(`lake env lean`). The round log's note says which way was taken (`Elaborate Suggested.lean:
lake env lean …` or `… targeted build … (built N Tau Ceti modules, M s)`). Elaboration problems
are reported whole: every error message up to 1000 characters with its continuation lines,
warnings only when there is no error, at most 40 diagnostics with the first error of every file
always among them. An operator who already holds a built `.lake/packages` may still export it:

```bash
export TAUCETI_ROADMAP_LAKE_PACKAGES=/path/to/TauCetiRoadmap/.lake/packages
```

Workers symlink it into their checkouts and worktrees instead of the shared copy.

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
  (`<login>/<worker-id>`). Use distinct ids for concurrent workers. A process holds
  `workers/<id>/worker.lock` for its lifetime, so a second one started with the same id on the same
  machine (a `--dry-run` too, which resets the same checkouts) exits at once with `worker <id> is
  already running here (pid N on <host>; ...)`.
* `--only` pins a worker to some kinds (`target, design, package, review, fix, port, redteam, gate`);
  without it the worker takes whatever is most useful: first the jobs whose roadmap descends from a
  target a human filed (origin `target:<issue>`), then imported ones (`explorer:<job>`; an unknown
  origin counts as imported), each by stage closest to delivery (GATE, PORT, FIX-RT, RT, FIX-PKG,
  REV-PKG, PKG, FIX-DESIGN, REV-DESIGN, DESIGN, TARGET), then the oldest issue. `[board] human_first`
  and `[board] priority` change that order; `board sync` records the origin on jobs that lack it,
  and `status` prints it.
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
* `--loop` keeps going: a usage limit pauses only the agent family that hit it, until the reset
  time the agent printed plus five minutes (half an hour when it printed none), and the worker goes
  on with jobs for its other families; a worker whose every usable family is paused (one pinned with
  `--agent` or `--model`, or with one agent installed) sleeps until the first reset. When nothing is
  eligible it sleeps `[worker] idle_seconds` (default 600). The first poll after a
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

**What it costs.** Both agent CLIs stream their events (`claude -p --output-format stream-json
--verbose`, `codex exec --json`); the raw events are kept as `jobs/<JOB>/run-N.jsonl`, and
`run-N.out` gets a readable rendering as they arrive (messages, tools and commands, errors the CLI
reports; never a tool's output), so `tail` shows a Claude run live too. Every run's token usage is
written as a `USAGE` line before its `EXIT` line and recorded, with its job, kind, lane, run, agent,
model, seconds and brief size, in `jobs/<JOB>/usage.jsonl` and `workers/<id>/usage.jsonl`. The
fields are the same for both CLIs: `input_tokens` (read without the cache), `cache_read_tokens`,
`cache_write_tokens`, `output_tokens` (thinking included), `thinking_tokens`, and for Claude its own
`cost_usd` and `turns`. `tauceti-roadmap usage [--worker-id ID] [--by lane|kind|job|agent|model|worker|day|label]
[--since DATE] [--json]` sums them for the host. Input read from the cache dominates long runs
(every turn reads the whole context again); output includes the thinking.

**The tools a brief lists.** Before the agent runs, the worker prepares three things and the brief's
"Tools" section points at them; each is best effort (a failure is a line in the round log and in the
brief, never a failed round):

* the index of open roadmap pull requests: every open pull request of the roadmap repository that
  adds or changes a roadmap folder, listed by one GraphQL query and fetched into a bare mirror shared
  by the host's workers (`<state>/open-roadmaps/`, about 60 MB), rebuilt at most every half hour
  under a lock. `tauceti-roadmap roadmaps list | grep <regex> | show <Name> [--file Suggested.lean]
  [--pr N]` read it with no GitHub token (`grep` searches the folders the open pull requests carry,
  at their heads, then every roadmap on main); `roadmaps refresh` rebuilds it by hand;
* `scratch/citations.md`: `roadmaps resolve` on the roadmap's README. It lists only what is found
  nowhere: a cross-roadmap citation (`Name §1.2`, `Name Layer 3`, `Name A1.2`) whose id the cited
  roadmap does not contain on main or at any open head (an item of a numbered list under its parent
  heading counts), a cited roadmap that exists nowhere, a backticked Lean name whose last component
  occurs nowhere in the pinned Mathlib and Tau Ceti sources (its additive or multiplicative twin
  counts), or a `TauCetiRoadmap.<Name>.x` that roadmap's Suggested.lean does not have. These are
  leads for the agent, never gates;
* `scratch/signatures.md` (lanes that elaborate, with the pin's Lake packages ready): the elaborated
  type of every declaration of Suggested.lean (`#check @name` appended to a copy, elaborated with
  `[gates] elaborate` and `--json`, split like a command line and run without a shell), and the
  section binders that look lost: an instance or a named hypothesis `h…` every variable of which the
  type keeps while the binder itself is gone. A red-team pass, which does not elaborate, gets the
  transcript only when the pin's packages are already fetched on the host: it never starts a fetch.
  The transcript is made before the lane's own gates read the file, so the worker first refuses a
  file that matches `[gates] forbidden_patterns` or runs code at elaboration (`#eval`, `run_cmd`,
  `initialize`, macros and elaborators, `unsafe`, `IO`); the brief then says why there is none.

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
agent hits a usage limit, its lease is lost, or you press Ctrl-C) releases the lease and puts the job
back to `state:available` with a "Released by worker" note (a job that needs a human goes to
`state:blocked` instead). Either move is made only while the job is still as the round left it: the
issue's newest "Claimed for" comment names this worker, no other owner holds a live lease on the
job, and its labels, read again just before the write, still say `state:claimed`. A job another
worker took over, or that was submitted or finished meanwhile, is left as it is (logged `<JOB>: left
as it is, not set to available: <why>`). A usage limit is any agent run whose stdout or
stderr carries one of the CLIs' limit messages ("out of usage credits", "usage limit", "manage
usage credits", "hit/reached your … limit", "model is at capacity"; on a run that exited non-zero
also "rate limit", "Too many requests"/429), wherever the text appears and whatever the exit code:
the round is logged `limited: <message>` (`limited (after N min of work): <message>` when the agent
had worked before the limit stopped it), the job is released, no attempt is consumed, the run's
output stays in the job folder, and the family is paused until the reset time the message gives
(half an hour when it gives none). A fault before any agent runs (a brief that does
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

Disk space: the port, red-team, red-team fix and gate lanes check out and build the roadmap repository,
so a worker does not start one when the state directory's disk has less than 20 GB free (it logs
`skip <JOB>: only N GB free …` and takes other work), and `doctor` warns below 40 GB. A full disk never
crashes the worker loop: the round is logged and the worker waits for its next poll.

A run that fails the worker's checks gets repair runs in the same workspace, each fed only the
problems that remain, up to three while their number strictly falls (74 → 20 → 3 → 0 submits; a
repair that does not reduce them ends the attempt). A design or package run that still fails leaves
its deliverables (those safe to publish: no source material, no local path) on `wip/<JOB>` on the
operator's fork, recorded on the job; the next attempt, on any worker, starts from them, and the
branch is deleted when the job submits.

Pushes name the head the work was built on (`--force-with-lease=<branch>:<head>`, checked against
the remote itself). When the branch moved while the job ran, nothing is pushed and the job is set
to `state:blocked` ("the pull request moved from X to Y while the fix ran"): a human, or the next
round, rebases the work. A push that fails otherwise is tried three times, a minute apart; then the
run is kept (`pending-push/`) and the job released, and the next attempt on this worker restores the
deliverables and the commit, runs the checks again and pushes, without running the agent (after
three such attempts the run is given up). Every checkout the worker creates carries the operator's
git identity in its own configuration, so an agent's own `git commit` works.

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
The merge carries the deliverables of the jobs that wrote the pull request and, for an explorer
import, the files its import marker declares under the roadmap's folder (the explorer's
`review.json`), each through the submission checks (source material, local paths, JSON/TOML). A merge
the checks refuse (`#N: FIX-RT-<Name>~r pushed, but not merged yet (...)`) leaves the fix submitted,
and every intake run tries it again; nothing needs to be pushed.

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
round, and every fix round is followed by a fresh independent review. After `[board] max_rounds`
rounds (5 in the example) the next fix round is created as `state:blocked`, listing the last
review's findings, and the review's issue says so: a human moves the fix to `state:available` to
run the next round (the round counter keeps counting, and every later round parks the same way), or
closes it to stop. `board sync` creates that blocked fix for a review parked before this rule (the
last review of its roadmap, asking for changes, with the parking comment, and no later fix). A fix may mark a finding `not actionable in this
package` with its reason (it asks another roadmap for something, or for a decision outside the
package); when the next review's blocking findings only re-raise such findings (`Re-raised: <id>`
in the finding, or the same title), the intake creates the next fix round as `state:blocked` with
the findings named on the review's issue: a human decides them, then moves the fix to
`state:available` to run it, or closes it. A design's declared supplier gaps (`gap:<slug>` with a
named supplier and a one-sentence `needs`) are contracts, not findings. The package stage (the
package, its fixes, a red-team fix on its board pull request) may add gaps to the top-level `gaps`
list of `design/plan.json` and nothing else in it (every gate refuses another change), stating each
under the README's "Exact supplier contracts" as `What <Supplier> must provide (gap:<slug>): ...`;
the intake announces such gaps on the supplier's job when the package is accepted. A review may accept
conditional on them (`conditional_gaps` in the verdict, or a `Conditional on the announced gaps:`
line in a comment review). A design-stage roadmap is checked as a merge-order candidate
(`gate --candidate <folder> --only <Name>` places it after its last supplier), since it enters the
merge order only when its design is accepted (an imported package or accepted design when it is
imported). The dependency check takes a roadmap the README names as its consumer (listed under
`## Downstream consumers` or a heading nested there, "X consumes ...", "consumed by X") as a supplier
only where an explicit supplier statement names it (a `*Needs:*` clause, a `### From X` heading, a
supplier table row or contract bullet, a `### Dependencies` sentence), never from a prose line that
also says "supplier".

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
| review | the board PR's head (read-only) | a comment on the board PR in the review shape (from its report and verdict) | not an author's operator (or, with `separation = "agent"`, not the authoring agent and model; with `"family"`, not the authoring family of models) | verdict schema, report header, reviewed files untouched, an acceptance's adversarial table with computed values |
| fix (design/package) | the review's comments | commits pushed to the board PR's branch | its owner (a fresh review follows) | a section for every finding of the review, gates |
| redteam | the board PR's head (read-only) | a comment on the board PR in the red-team shape | the agent and model the pass requires | report header, checkout untouched, a pass with no finding has its adversarial table with computed values |
| fix (red team) | the round's pass reports | commits pushed to the board PR's branch | its owner | a section for every finding of the round's passes, the package's gates, elaboration, the targeted build in a roadmap checkout |
| port | the merged, red-teamed package | branch `roadmap/<kebab-name>` on the operator's fork, the roadmap PR; the merge-order file when it places the roadmap | anyone | commit made, full gate with build, synopsis |
| gate | every review since the last reply | commits on the PR branch, a reply, labels | the PR branch owner | finding sections, clean tree, full gate with build, head check |

**What `Suggested.lean` holds.** It is the roadmap's acceptance test, not an API catalogue: the
headline theorems the finished roadmap will prove, stated now against the pinned Mathlib and Tau
Ceti with `sorry`, plus only the simple definitions they need; a headline that is not statable at
the pins is named in the module docstring. The package brief has its agent list the headline
theorems first; reviews and red-team passes ask whether every statable headline is stated and
report padding (API lemmas, boundary cases, declarations mirroring README Checks, value-computing
examples) as P3 deletions. README API bullets and Checks state their facts in prose and carry a
backticked Lean name only for a declaration of `Suggested.lean`. The text gates check form only
(imports, `set_option`, sorry'd data, an admitted `example`, and `readme-ids-missing`: every name of
the roadmap's own the README claims exists in the file); none counts declarations or examples, and
there is no minimum size.

Before the agent of a package fix (or of a red-team fix on a board pull request) runs, the lane runs
the package gate on the package as it stands. It removes `<a id>` anchor tags itself (internal links
pointed at the heading slugs) and lists every other failure in the brief's mandatory section "Gate
failures to clear": the round log's line, the gate's rule and every hit (logged `before the agent: ...`).
The gate after the agent is unchanged.

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
as soon as it is approved." when none of ours must come first). A pull request is ours when the
port lane opened it, or when the merge order names a roadmap folder it adds and one of the
project's operators opened it: a login in `[board] operators` (default: the running login) or one
the claims namespace records. So pull requests an operator opened by hand or with other tools are
links of the chain, which is the transitive closure of the roadmap's suppliers among ours, in
merge-order rank (a pull request that adds several roadmaps appears once). Every other open pull
request that adds a roadmap folder not on main is another author's, and counts when the README cites
that roadmap as a supplier: it is listed as `#248 (LFunctions, roed-math)` in "It also depends on
other authors' pull requests that add roadmaps it cites, which must merge first: ...". When the
open pull requests cannot be read, no paragraph is written (`merge-order` says so; the port lane
names it in its summary). A roadmap missing from the merge order gets no paragraph, and only the
port lane's pull requests get one from the tool.

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
else your own fork. A lease excludes only the workers that share its namespace: leases in your fork
keep your own workers apart and nobody else's, so two operators on fork namespaces can both run one
job, and the board's intake alone decides which submission is taken in.

Taking a lease is fail-open: a lease that cannot be registered is logged and the round proceeds
unclaimed, trying to take the lease at every renewal; once another owner holds it, the round has
lost it. Holding a lease is not: a job lease lasts an hour from its last renewal (`[claims]
job_ttl_seconds`, default 3600), the worker renews it every 20 minutes while the round runs
(`[claims] renew_seconds`, default 1200), and the lease is lost when a renewal finds it gone or
another owner's, or when every renewal fails until it expires (an unreachable namespace: anyone may
take an expired lease). The round's work goes on, but it writes nothing more to GitHub: the first
write it attempts (or its submission, which checks before its first push) ends the round as
`lease-lost: the lease ... was lost: <why>`, a checked run is kept in `pending-push/` as a run whose
push failed is (the next attempt on this worker pushes it without the agent), and the job is
released only while it is still this worker's claim.

A worker id runs one process at a time (`workers/<id>/worker.lock`): at its start a worker releases
any lease its previous process left behind (killed before it could release, or crashed), and any
claimed job whose last "Claimed for" comment names this worker id and operator even with no lease (a
process that died before it wrote one), and puts those jobs back to `state:available` while their
issues still say `state:claimed`. `board sync` puts back every claimed job whose lease has expired
(the worker stopped renewing it), and every claimed job with no lease at all whose "Claimed for"
comment is older than a job lease lasts ("Released: claimed by <worker> at <time> with no live
lease"; a claim made by a `reviewing` comment holds for its four hours), without waiting for `board
stale`; a claims namespace that cannot be read at that moment releases nothing.

When a design, package or fix is submitted, the worker also records an author lease
(`roadmap/<project>/author/<JOB>`, with the operator login, agent and model inside). A review job
lists every job that wrote the round it reviews (`independent_of`); a worker refuses it when its
operator authored any of them, and the intake refuses a submission whose GitHub author did (it reads
the board's own "Intake: merged #N from @login (agent, model)" notes, which only the board's logins
can write). With `[review] separation = "agent"` (a project with a single operator) the authoring
operator may take the review when the reviewing agent and model differ from the authoring ones
recorded for the job (an author record without its agent and model never counts as different), and
the intake applies the same test to the submission's agent and model. Projects with several
operators keep the default `"operator"`. With `separation = "family"` the reviewing agent's family of
models must differ from the authoring agent's (OpenAI's Codex and ChatGPT are one family, Anthropic's
Claude another), whoever the operators are; an author record without its agent falls back to another
operator. In every separation mode, a worker not pinned with `--agent` or `--model` that has
another family installed takes a review its default family may not take with that family: under
`"family"` that is what makes it independent, under `"agent"` another agent and model can be, and
under `"operator"` it changes nothing (the operator is the same), so the review is skipped as before. A fix worker's own acceptance never counts.

## Board maintenance

Run with write access to the board (a scheduled workflow, or the maintainer). Every command is a dry
run unless `--yes` is given.

```bash
tauceti-roadmap --conf $CONF board sync --yes      # targets → TARGET jobs; one state label per job;
                                                   # expired leases released, live ones → state:claimed;
                                                   # PR labels;
                                                   # GATE jobs when humans await a reply;
                                                   # the pinned "Review queue" issue;
                                                   # held PRs back to draft;
                                                   # the errata register (submitted ERRATA jobs → done)
tauceti-roadmap --conf $CONF board intake --yes    # take in submissions (designs and packages stay open
                                                   # for review), answer needs-references jobs, then read
                                                   # the reviews and passes on the board's open PRs
tauceti-roadmap --conf $CONF board references --yes   # only the needs-references answers
tauceti-roadmap --conf $CONF board stale --release --yes   # release lapsed claims and closed submissions
tauceti-roadmap --conf $CONF merge-order           # refresh the chain paragraph on open roadmap PRs
tauceti-roadmap --conf $CONF targets import-intentions --yes   # intentions → TARGET jobs (10 per run)
tauceti-roadmap --conf $CONF board import-explorer --from ~/tauceti-explorer --yes   # see below
tauceti-roadmap --conf $CONF board errata seed [--limit N] --yes   # an ERRATA-<Name> job per roadmap on main or
                                                   # in an open roadmap PR whose sources no errata cover yet
```

When a package's port is created, the intake inserts the roadmap into the merge order on the board's
main if it is absent, after its last supplier as its merged README names them (logged `<Name>
inserted into the merge order` or `already in the merge order`). Every `board intake` also inserts
each open import pull request (`Import <Name> from the explorer (...)`) whose roadmap the order lacks,
by the README at its head, in one commit (logged `#N: <Name> inserted into the merge order (an open
explorer import)`); an import whose design is still under review is left to that review. The
package gate reads the board main's order when the head of a board pull request carries an older
file. A roadmap that still reaches the
port without a place in the merge order is placed by the port lane: right after its last supplier
that the order lists (its README's supplier lines, as the dependency check reads them, and every
`TauCetiRoadmap.<X>` its Lean names), at the end when it has none, with a warning for a supplier the
order lacks. The edited `ORDER.txt` is part of
the port's submission, so the intake merges it with the report. When the place would put one of
its suppliers after one of its consumers, the job is set to `state:blocked` with the cycle in a
comment, and nothing is submitted.

A roadmap the order lists above a supplier its README names (an UPWARD edge; typically an import
placed by a README that cited the supplier under a name the dependency check did not know, which a
later fix repointed) is moved right after its last supplier instead of failing, unless a consumer of
it comes at or before that supplier (then the UPWARD line stands, with the reason in the log). The
port lane and a red-team fix of a roadmap pull request move it in the board checkout's order, which
their submission carries; the package gate (a package, its fixes, a red-team fix on a board pull
request) checks against a copy with the move, and the agents' check (`gate --depcheck ... --movable`)
prints `moved: ...` instead of the UPWARD line; when the port is created, the intake moves it on the
board's main. Every `board intake` also moves each roadmap the order lists above a supplier its board
README names (at the head of its open pull request under review, else on main; read by one shallow
git fetch of the board, no REST request), all in one commit, logged `merge order: <Name> moved in the
merge order from #a to #b, right after its last supplier <S>` (or `... not moved ...` with the
reason). A clean order is only read.

Every `board` command that writes (`intake`, `sync`, `references`, `stale --release`, `errata seed`
and `import-explorer`, each with `--yes`) first takes the board lock, a 15-minute lease ref in the
board repository (the workers' lease mechanism; the hosted workflow's token and a maintainer can
both push it), renews it every three minutes while it runs, and releases it at the end, so the
hosted run and a maintainer's shell never interleave, however long a run takes. A run that finds it
held prints `intake already running (intake:run-<id or host>, since <time>); nothing done` and exits
0: wait, or let the schedule pick things up. A run that cannot take it (no push access to the board
repository, no network) prints `board lock not taken (the lock ref could not be written); nothing
done` and exits 1. A run whose lock is lost (a renewal finds another owner, or every renewal fails
until the 15 minutes run out) stops at its next write with `stopped: the lease ... was lost: <why>;
the next run carries on` and exits 1. Every job creation
re-reads the live board first and skips a job whose issue exists (logged `already on the board as
#n`), and each intake closes an open job issue that duplicates another open one (`Duplicate of #n`,
labelled `duplicate`); a `state:done` twin is never closed. The twin kept is the one that got
furthest, not the older one: the twin an open submission pull request names (`Refs #n`, or
`state:submitted`), else the one a worker claimed and still holds the lease of, else any claimed or
parked one, else the oldest; the kept twin takes over the closed twin's pull request and state
(`submitted` while its submission is open, `claimed` while its lease is live), and the job's lease is
left alone (it is keyed by the job id, so it belongs to the worker on the kept twin).

### The board workflow

The board's maintenance need not depend on anyone running it: copy `board/board.yml` from this
repository to `.github/workflows/board.yml` in the jobs repository. Every ten minutes, when a
submission pull request opens or changes (`pull_request_target`), and on demand
(`workflow_dispatch`), it installs the tool from this repository's main and runs `board intake --yes`,
`board sync --yes` and `board stale --release --yes` with the workflow's own token (permissions:
contents, pull requests and issues write; one run at a time). Comments and issue edits are not
triggers: the schedule picks them up within ten minutes, and on a busy board event-driven runs spent
the token's budget. That budget is 1,000 REST requests per hour for the repository, whatever the
users' own tokens allow, and every run shares it; each `board intake|sync|stale` run ends with
`GitHub API calls this run: N (rate limit remaining R/L, resets HH:MMZ)` and a `WARN:` line when
fewer than 200 requests are left.
Everything the board writes works with that token: its notes are signed by `github-actions[bot]`
(keep it in `[board] bot_logins`), and authorship always comes from the submission pull request's
author.

GitHub's schedule is unreliable (scheduled runs are skipped under load), so a worker asks for a
board run itself (`gh workflow run board.yml -R <board repository>`, logged `board run requested`)
after every round that submitted, parked, blocked or released a job, at most once a minute; the
workflow's concurrency group collapses bursts, a failure is ignored, and the schedule stays the
fallback. `[board] dispatch = false` turns it off.

An accepted board pull request that conflicts with main (GitHub reports it `mergeable_state:
dirty`, or the merge call names a conflict; a merge the API refuses with any other error, a 403
included, on a pull request it reports clean is a failure retried by the next scan) is merged by the
intake itself: it squashes the pull request onto main in a checkout of the board, resolves each
conflicting file, commits with the pull request's author, pushes to main and closes the pull request
with a note. A conflicting sources.toml (the intake recorded answers in the roadmap's manifest on
main while the branch changed it too) is merged by key (main's answered entries, those with a
`resolution`, are kept; the pull request's other entries win). A conflicting `ORDER.txt` (an older
import branch placed its roadmap in the merge order itself while the intake rewrote the order on
main) is taken from main, which is authoritative, and the pull request's roadmap is inserted after
its last supplier when main's copy lacks it. A conflict in any other file sets the job that
concluded it (the accepting review, or the last red-team fix) to `state:blocked`: "the pull request
#N conflicts with main in <files>; a human rebases it".

An accepted design whose merge failed stays in a retry state on its review job (`merge_pending`: the
reviewed head and the accepting comments), and every intake retries the merge. When the head has
moved (a human rebase), the intake compares the two heads: if the pull request's own deliverables
did not change beyond sources.toml (main merged in), it merges the new head; otherwise it asks for a
fresh review of the new head. A board pull request merged on GitHub by hand finishes its review
from the recorded (or last) accepting review, closes the jobs that wrote it and creates what follows
(the package for a design; for a package, its port, since no pull request is left for the board red
team, which then runs on the roadmap pull request); the review is never reopened.

A GitHub blip never costs a run: every read is tried again after a 5xx answer or a timeout (2, 5
and 15 seconds later); a pull request whose reads still fail is skipped with a line in the log and
the run goes on with the next one (a sync step likewise), and the command exits 1 at the end listing
what it skipped, which the next run takes up. The intake reads the open pull requests' listing once
and uses its objects (no GET per pull request); a pull request whose head, labels and update time are
as at the last run, with a stable outcome (under review, left for the maintainer, not a submission),
is not read again (`intake-prs.json` in the state directory, empty on a fresh runner).

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

On the way a README loses its `<a id>` anchor tags (process residue to the package gate), its internal
links pointed at the heading slugs, and a roadmap whose `packets/<Name>.json` lists sources gets a
`sources.toml` made from them: each work `uncertified` (licence class `none`, since the board has
fetched none), with the URL, arXiv id, edition and the SHA-256 of the copy the explorer read; a lane
that fetches one with `sources fetch --id <id>` records it as certified. Once the pull requests are
open, the packages and accepted designs among them are inserted into the merge order on the board's
main after their last suppliers, in one commit (the outcome column says so).

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
| `workers/<id>/port/<Name>`, `pr/<Name>` | worktrees for the port lane and for work on a pull request branch; their `.lake/packages` is a link to `lake/<pin>/packages`; removed after the lane submits, and at worker start when the roadmap has no open job (only git worktrees of the content checkout under `pr/` and `port/`; `build/` holds the worker's snapshot of the briefs and is never swept) |
| `lake/<pin>/packages` | the machine's one copy of the Lake packages (Mathlib and Tau Ceti oleans, about 16 GB) per pin of the content repository's manifest, toolchain and lakefile, shared by every worktree of every worker; `lake exe cache get` fills it once (`.ready` marks it) |
| `workers/<id>/jobs/<JOB>/` | brief, agent logs (`run-N.out` with START/USAGE/EXIT lines, `run-N.err`, the raw events `run-N.jsonl`), scratch (with the worker's `citations.md` and `signatures.md`), problems, gate result, `usage.jsonl` |
| `workers/<id>/usage.jsonl` | the tokens of every agent run of this worker (`tauceti-roadmap usage`) |
| `open-roadmaps/mirror.git`, `open-roadmaps/index.json` | the bare mirror of the roadmap repository with the head of every open roadmap pull request (`refs/open/<n>`), and their index; shared by the host's workers, rebuilt at most every half hour |
| `open-roadmaps/tokens-<pin>.txt` | the identifiers of the pinned Mathlib and Tau Ceti sources, for `roadmaps resolve` |
| `workers/<id>/attempts.json` | failures per job on this worker |
| `workers/<id>/pending-push.json`, `workers/<id>/jobs/<JOB>/pending-push/` | runs kept after their push failed or their lease was lost (the job's deliverables, and the commit as a bundle and a `refs/tauceti-pending/` ref): the next attempt on this worker pushes them without running the agent |
| `workers/<id>/rounds.log` | every line the worker printed, timestamped (what `tail` follows) |
| `workers/<id>/worker.lock` | locked by the worker's running process (its pid and host inside): a second process with the same id exits |
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
  answers and makes the job available again once every key is answered. A `source:` whose host
  does not answer is tried again a few times over the next hour (one note says so); if it still
  fails, or the failure is not network trouble, the request is posted again and you answer anew.
* **An intention** on the roadmap repository becomes a TARGET job when the maintainer runs
  `targets import-intentions`; there is nothing else to do.
* **A draft pull request**: review it on GitHub. Your comments feed the gate lane; the reply answers
  every point, and the labels flip between awaiting-author and awaiting-review. Merging is yours.
  A maintainer whose login is also the one the workers act as is the author of every worker-ported
  draft, so on those drafts their comments would read as the author's own replies. List that login
  in `[replies] maintainer_logins` and open each review with `[replies] maintainer_review_prefix`
  (default `Maintainer review`; case-insensitive, emoji and whitespace before it allowed), for
  example `gh pr review <n> --comment --body "Maintainer review of Suggested.lean: ..."`. Such a
  review or issue comment counts as anyone else's review: the next `board sync` flips the draft to
  awaiting-author and creates its GATE job, and the gate lane answers it with its inline comments.
  Every other comment of that login on its own drafts stays a reply; the gate lane's replies open
  with `Fixed in`, never with the prefix.
* **The configuration and the prompts** are CODEOWNERS-protected; changes go through review.

## Known limits of this version

* The harness runs standalone; running the lanes as TauCetiWorker `only=` kinds, with its quota
  pacing, dashboard and Bubble sandbox, is the intended integration.
* Two workers of the branch owner may run a red-team fix and a gate round on the same pull request
  at once; the branch push uses `--force-with-lease`, so one of them fails and retries rather than
  overwriting the other.
* Leases and the board lock are not fencing tokens: a writer stops at the first write after it
  notices the loss, but a write already under way when the lease lapses still lands, and the board
  does not check which attempt a submission or a state change comes from. The conditional release
  reads the issue and then writes it, so a change made in between is overwritten.
* The citation resolver and the lost-binder flags are heuristics: a citation written in running
  prose, a generated Lean name (`to_additive`, `simps`, a structure projection) or a binder named
  otherwise than `h…` can be missed or flagged wrongly. They are leads for the agent, never gates.
* The sibling checkouts (`rev/<pr>/`), the merge-order chain and the push gate's sibling READMEs still
  come from the pull requests labelled `[roadmap] pr_label`; the index of every open roadmap pull
  request feeds the agents' search and the citation resolver only.
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
