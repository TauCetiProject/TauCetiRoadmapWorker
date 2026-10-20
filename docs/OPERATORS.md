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

* `--worker-id` names the worker: its directory, its branch prefix and its lease owner
  (`<login>/<worker-id>`). Use distinct ids for concurrent workers.
* `--only` pins a worker to some kinds (`target, design, package, review, fix, port, redteam, gate`);
  without it the worker takes whatever is most useful.
* `--agent` pins the agent. Without it, each job runs on its lane's configured agent
  (`[agents.lanes.<kind>]`, default `[agents] default`), and a red-team pass on the agent it requires,
  if that agent is installed.
* `--loop` keeps going: after a usage limit it sleeps until the reset time the agent printed; when
  nothing is eligible it sleeps `[worker] idle_seconds` (default 600). The first poll after a
  failure comes within a minute, and while a job it parked for references waits, it looks every
  minute whether the board answered it and polls at once when it did. Without `--loop` it does one
  round and exits.
* `--dry-run` takes no lease, writes the brief to the job folder and prints every GitHub write.

A round that stops before it submits or parks its job (the lane fails, git or GitHub raises, the
agent hits a usage limit, or you press Ctrl-C) releases the lease and puts the job back to
`state:available` with a "Released by worker" note. A job that fails three times on one worker is
skipped by that worker (the count is in `workers/<id>/attempts.json`; delete the job's entry to let
the worker try it again); other workers may still try it. The design lane is capped per
operator per day (`[design] daily_cap`), counted in `design-ledger.jsonl` on the host.

## The job graph

```text
target ──triage──▶ DESIGN-Name ──▶ REV-DESIGN-Name ─accepted─▶ PKG-Name ──▶ REV-PKG-Name ─accepted─▶ PORT-Name
                        ▲                │ needs_changes            ▲             │ needs_changes          │ draft PR #n
                        └── FIX-DESIGN-Name~2 ◀┘ (≤ max_rounds)     └── FIX-PKG-Name~2 ◀┘                    ▼
                                                                         RT-Name~1a, RT-Name~1b (two agents)
                                                                                    ▼
                                                                         FIX-RT-Name~1 (branch owner)
                                                                                    ▼
                                                                         RT-Name~2a, RT-Name~2b ─▶ FIX-RT-Name~2
                                                                                    ▼
                                                     human review on the draft ─▶ GATE-Name~k (branch owner) ─▶ reply
```

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
| review | the design or package (read-only) | `review.json` (or `design/review.json`) and a report; may add to `sources.toml` | not an author's operator | verdict schema, report header, reviewed files untouched, the author's manifest entries kept |
| fix (design/package) | the review | the artefact's files | anyone (a fresh review follows) | finding sections, gates |
| port | accepted package | branch `roadmap/<kebab-name>` on the operator's fork, draft PR | anyone | commit made, full gate with build, synopsis |
| redteam | PR head (read-only) | a report | the agent the pass requires | report header, checkout untouched |
| fix (red team) | the round's pass reports | commits on the PR branch | the PR branch owner | commit made, clean tree, full gate with build |
| gate | every review since the last reply | commits on the PR branch, a reply, labels | the PR branch owner | finding sections, clean tree, full gate with build, head check |

Every lane writes its report to `packages/<Name>/reports/<JOB>.md` on the board, so the next lane
(often another operator) can read it. The roadmap's `sources.toml` is a deliverable of every job that
works on its sources (target, design, package, review, and the design and package fixes); the lanes
on the content repository (port, red team, red-team fix, gate) submit only their report. The port lane composes the pull-request description from
computed facts: the synopsis from the report, the dependencies on other open roadmap pull requests,
the merge-order chain and a validation paragraph from the gate result. `merge-order` refreshes the
chain paragraph in every open roadmap pull request; it never states a position number.

Only the operator who opened a pull request can push to its branch, so red-team fixes and gate
rounds name that operator (`requires_operator`). The red-team passes of one round name different
agents (`requires_agent`), from `[redteam] passes`.

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
and fail-open: a lease that cannot be registered is logged and the round proceeds.

When a design, package or fix is submitted, the worker also records an author lease
(`roadmap/<project>/author/<JOB>`, operator login inside). A review job lists every job that wrote the
round it reviews (`independent_of`); a worker refuses it when its operator authored any of them,
and the intake refuses a submission whose GitHub author did (it reads the board's own
"Intake: merged #N from @login" notes, which only the board's logins can write). A fix worker's own
acceptance never counts.

## Board maintenance

Run with write access to the board (a scheduled workflow, or the maintainer). Every command is a dry
run unless `--yes` is given.

```bash
tauceti-roadmap --conf $CONF board sync --yes      # targets → TARGET jobs; one state label per job;
                                                   # leases → state:claimed; PR labels;
                                                   # GATE jobs when humans await a reply;
                                                   # held PRs back to draft
tauceti-roadmap --conf $CONF board intake --yes    # merge valid submissions, create successor jobs,
                                                   # then answer parked needs-references jobs
tauceti-roadmap --conf $CONF board references --yes   # only the needs-references answers
tauceti-roadmap --conf $CONF board stale --release --yes   # release lapsed claims and closed submissions
tauceti-roadmap --conf $CONF merge-order           # refresh the chain paragraph on open roadmap PRs
tauceti-roadmap --conf $CONF targets import-intentions --yes   # intentions → TARGET jobs (10 per run)
```

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
| `workers/<id>/jobs/<JOB>/unsubmitted/` | what the job's last round that left anything left in the board checkout beyond the board's main, set aside before the next round reset it |
| `rev/<pr>/` | read-only checkouts of open roadmap pull requests at their heads (was `/tmp/rev/<pr>`) |
| `prs.json` | open roadmap pull requests: number, roadmap, head, branch, owner (was `/tmp/rev-list.txt` and `/tmp/watch-prs`) |
| `claims/<repo>.git` | scratch object store for lease refs |
| `design-ledger.jsonl` | design jobs started per operator per day |
| `hold-draft.txt` | pull requests that must stay drafts (was `/tmp/hold-draft.txt`) |
| `stacks.toml` | stacked pull requests, `child = parent` (was hard-coded in merge_order.py) |
| `external-deps.json` | other authors' roadmap pull requests a pull request depends on (was `/tmp/ext-deps.json`) |
| `mutual.txt` | `<pr> <other-pr>` pairs that cite each other (was `decycle/MUTUAL.txt`) |
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

* **A target**: use the issue template on the board (or `targets file`). `board sync` turns it into a
  `TARGET-Name` job; the triage decides new roadmap, Part II, or duplicate.
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
