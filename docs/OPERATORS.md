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
workers will use, the merge-order file, the state directory, whether every brief renders, and the
sandbox.

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
  nothing is eligible it sleeps ten minutes. Without `--loop` it does one round and exits.
* `--dry-run` takes no lease, writes the brief to the job folder and prints every GitHub write.

A job that fails three times on one worker is skipped by that worker (the count is in
`workers/<id>/attempts.json`); other workers may still try it. The design lane is capped per
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
`needs-references`. Every job issue carries a machine-readable spec in an HTML comment; the rest of
the issue is for humans and is never read as an instruction.

## Lanes

| Kind | Reads | Writes | Who may take it | Checked before submission |
| --- | --- | --- | --- | --- |
| target | the filed form (quoted as data), roadmaps, open roadmap PRs, packages | `packages/<Name>/target.md` | anyone | Verdict/Name/Base/Owner header |
| design | `target.md`, roadmaps, libraries, merge order | `design/README.md`, `design/plan.json`, `sources.toml` | daily cap | plan.json schema, sources manifest, residue, dependency check |
| package | accepted design and its review | `README.md`, `Suggested.lean`, `metadata.toml`, `sources.toml` | anyone | every text gate, sources manifest, elaboration |
| review | the design or package (read-only) | `review.json` (or `design/review.json`) and a report | not an author's operator | verdict schema, report header, reviewed files untouched |
| fix (design/package) | the review | the artefact's files | anyone (a fresh review follows) | finding sections, gates |
| port | accepted package | branch `roadmap/<kebab-name>` on the operator's fork, draft PR | anyone | commit made, full gate with build, synopsis |
| redteam | PR head (read-only) | a report | the agent the pass requires | report header, checkout untouched |
| fix (red team) | the round's pass reports | commits on the PR branch | the PR branch owner | commit made, clean tree, full gate with build |
| gate | every review since the last reply | commits on the PR branch, a reply, labels | the PR branch owner | finding sections, clean tree, full gate with build, head check |

Every lane writes its report to `packages/<Name>/reports/<JOB>.md` on the board, so the next lane
(often another operator) can read it. The port lane composes the pull-request description from
computed facts: the synopsis from the report, the dependencies on other open roadmap pull requests,
the merge-order chain and a validation paragraph from the gate result. `merge-order` refreshes the
chain paragraph in every open roadmap pull request; it never states a position number.

Only the operator who opened a pull request can push to its branch, so red-team fixes and gate
rounds name that operator (`requires_operator`). The red-team passes of one round name different
agents (`requires_agent`), from `[redteam] passes`.

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
tauceti-roadmap --conf $CONF board sync --yes      # targets → TARGET jobs; leases → state:claimed;
                                                   # PR labels; GATE jobs when humans await a reply;
                                                   # held PRs back to draft
tauceti-roadmap --conf $CONF board intake --yes    # merge valid submissions, create successor jobs
tauceti-roadmap --conf $CONF board stale --release --yes   # release lapsed claims and closed submissions
tauceti-roadmap --conf $CONF merge-order           # refresh the chain paragraph on open roadmap PRs
```

The intake merges a submission only when it names an open job, changes only that job's deliverables
and report, contains no local path, has valid JSON and TOML, its checks passed, and its author is
independent where required. When a design is accepted it inserts the roadmap into the merge-order
file after its last supplier. With `[redteam] ready_after = true` the last red-team fix marks the
pull request ready for review; otherwise it stays a draft for a human to mark ready.

Pull requests listed in `hold-draft.txt` in the state directory stay drafts: `board sync` converts
them back if anything marks them ready.

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
| `rev/<pr>/` | read-only checkouts of open roadmap pull requests at their heads (was `/tmp/rev/<pr>`) |
| `prs.json` | open roadmap pull requests: number, roadmap, head, branch, owner (was `/tmp/rev-list.txt` and `/tmp/watch-prs`) |
| `claims/<repo>.git` | scratch object store for lease refs |
| `design-ledger.jsonl` | design jobs started per operator per day |
| `hold-draft.txt` | pull requests that must stay drafts (was `/tmp/hold-draft.txt`) |
| `stacks.toml` | stacked pull requests, `child = parent` (was hard-coded in merge_order.py) |
| `external-deps.json` | other authors' roadmap pull requests a pull request depends on (was `/tmp/ext-deps.json`) |
| `mutual.txt` | `<pr> <other-pr>` pairs that cite each other (was `decycle/MUTUAL.txt`) |
| `sources/` | the sources cache (see SOURCES.md) |
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
* **A `needs-references` request**: attach the text, name an alternative legal source with its
  locator, or waive the citation (it is then cited by locator only, as `uncertified`), and move the
  job back to `state:available`.
* **A draft pull request**: review it on GitHub. Your comments feed the gate lane; the reply answers
  every point, and the labels flip between awaiting-author and awaiting-review. Merging is yours.
* **The configuration and the prompts** are CODEOWNERS-protected; changes go through review.

## Known limits of this version

* The sources service (fetching, the cache, OCR) is not implemented; see SOURCES.md.
* The harness runs standalone; running the lanes as TauCetiWorker `only=` kinds, with its quota
  pacing, dashboard and Bubble sandbox, is the intended integration.
* Two workers of the branch owner may run a red-team fix and a gate round on the same pull request
  at once; the branch push uses `--force-with-lease`, so one of them fails and retries rather than
  overwriting the other.
* Section-sharded red-team passes (`[redteam] sharded`) are configured but not yet scheduled.
