# TauCetiRoadmapWorker

`tauceti-roadmap` plans Tau Ceti roadmaps with a "bring your own agent" approach, one level above
[TauCetiWorker](https://github.com/TauCetiProject/TauCetiWorker): humans name targets, operators'
agents design the roadmaps that reach them, independent agents review the plans, and the result is
a draft pull request to [TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap) that a
human merges. The contract it implements is [docs/DESIGN.md](docs/DESIGN.md); how to run a fleet is
[docs/OPERATORS.md](docs/OPERATORS.md); the sources rules are [docs/SOURCES.md](docs/SOURCES.md).

It runs as your authenticated `gh` account. You author through **your own forks** (created on first
use), so you need no write access to the canonical repositories. Several workers per operator and
several operators per project coexist through leases in a claims namespace; a reviewer is never the
author's operator.

## Quickstart

You need `gh`, `git`, `uv`, and at least one agent CLI. The port, red-team fix and gate lanes also
need an `elan`/`lake` toolchain. Log `gh` in as the account the worker should act as, and log in to
each subscription agent you want to use:

```bash
gh auth login
codex login            # for --agent codex
claude auth login      # for --agent claude
```

Install it as a tool, no clone needed:

```bash
uv tool install git+https://github.com/TauCetiProject/TauCetiRoadmapWorker

CONF=https://raw.githubusercontent.com/<owner>/<repo>/main/tauceti-roadmap.toml

tauceti-roadmap --conf $CONF doctor                               # tools, credentials, repositories, claims, sandbox
tauceti-roadmap --conf $CONF work --worker-id alice-1             # one round of the most useful job, then exit
tauceti-roadmap --conf $CONF work --worker-id alice-2 --only review,redteam --loop   # a focused reviewer
tauceti-roadmap --conf $CONF work --worker-id alice-3 --loop      # keep picking the most useful job
tauceti-roadmap --conf $CONF targets list                         # targets and designs on the board
tauceti-roadmap --conf $CONF targets file --statement "..." --why "..." --name PrismaticCohomology
tauceti-roadmap --conf $CONF sources fetch arXiv:2101.00001 --manifest sources.toml   # a legal source, cached and recorded
```

Ctrl-C stops a worker. `--dry-run` prints every GitHub write instead of making it (and writes the
brief without running the agent). The configuration is a human-owned `tauceti-roadmap.toml`; every
field is documented in [tauceti-roadmap.toml.example](tauceti-roadmap.toml.example).

## What a round does

1. List the available jobs on the board (GitHub issues labelled `kind:*` and `state:*`), rank them
   (humans waiting on a pull request first, then work in flight, then new work; bottom-up in the
   merge order), and take the first one this worker may do: the right agent, not the author's
   operator for a review, the branch owner for work on a pull request, the daily design cap.
2. Take a lease on it (`refs/tauceti-claims/roadmap/<project>/job/<JOB>`), renewed while it runs.
3. Prepare fresh checkouts under `~/.local/state/tauceti/projects/<project>/workers/<id>/`, bring the
   texts the roadmap's `sources.toml` records into the sources cache, render the lane's brief from
   `prompts/`, and run the agent on it.
4. If the agent could not read a source legally, park the job in `state:needs-references` with a
   request naming exactly what is missing; humans answer it on the board (name a legal source, name
   the file operators hold locally, or waive the citation; sources never enter a repository or an
   issue) and the intake releases the job. Otherwise check the result
   mechanically (the gates), do
   the lane's last mile (push, open the draft pull request, reply to reviewers), and submit the
   report and deliverables to the board as a pull request that names the job.
5. The board's intake merges valid submissions and creates the next jobs.

| Kind | Lane |
| --- | --- |
| `target` | triage a filed target: new roadmap, Part II, or already owned elsewhere |
| `design` | the roadmap plan: layers, targets with locators, suppliers by name and section, consumers, sources manifest |
| `package` | the roadmap folder: README at upstream density, `Suggested.lean`, `metadata.toml` |
| `review` | independent review of a design or package: seeded locator sample, adversarial pass, duplication and supplier-order audits |
| `fix` | resolve a review's findings (package files only), or a red-team round's findings on the pull request branch |
| `port` | copy the accepted package into the roadmap repository, run the gates, open the draft pull request |
| `redteam` | two independent whole-document passes by different agents, each round followed by a fix; optionally one more pass per README section |
| `gate` | answer the human reviewers: ingest every review since the last reply, fix, push, reply finding by finding |

## The gates

`tauceti-roadmap gate <worktree> <Name> --base origin/main` runs every push gate on one roadmap
folder and exits 1 on any failure: `set_option`, pinned blob links, LaTeX and TeX residue,
pull-request numbers, sorry'd data and admitted examples, process residue, local paths,
`import Mathlib.<submodule>`, the line cap, configured forbidden patterns and dropped roadmaps,
README names missing from the Lean, empty Lean sections, process-named labels, the dependency check
against the merge order, a dirty tree and the outside-folder diff (`--build` adds the targeted
build). Call it directly, never through a pipe; `--out FILE` keeps the structured result.

## Sources

`tauceti-roadmap sources fetch <DOI | arXiv id | URL | "Authors, Title, Year">` resolves a citation to
a legal source (the operator's own texts, arXiv, HAL, Numdam, GDZ, EuDML, Project Euclid open
content, publisher open archives, author pages, institutional repositories), never a pirate mirror
and never past a paywall, keeps it in `~/.local/state/tauceti/projects/<project>/sources/` under its
SHA-256 with a line-numbered text beside it, and records `url`, `fetched_from`, `fetched_at`,
`sha256` and `licence_class` in the roadmap's `sources.toml`. A citation that resolves only to a
paywall is recorded as `uncertified` with its DOI and locator. Image-only scans go through OCR when
the optional extra is installed. The rules and the hand-off are in [docs/SOURCES.md](docs/SOURCES.md).

## Status of this version

Build-plan steps 1 to 4 are in place: the scaffold, the job board and intake, the port, red-team and
gate lanes (with optional section-sharded red-team passes), and the sources service with the
manifest, cache, deny and paywall policy, OCR and the `needs-references` hand-off. Intentions on the
roadmap repository become targets with `targets import-intentions`. The harness is standalone for
now; running its lanes as TauCetiWorker `only=` kinds under a TauCetiTools project configuration is
the intended integration.

## Development

```bash
uv venv && uv pip install -e . pytest       # add '.[pdf]' to exercise the pypdf fallback
.venv/bin/python -m pytest
python3 -m compileall -q tauceti_roadmap_worker
```

The package uses the standard library only; the optional extras are `pdf` (pypdf, when poppler's
`pdftotext` is missing) and `ocr` (RapidOCR with pypdfium2). The tests never touch the network: a
fixture refuses real requests and the sources tests run on canned responses. Briefs live in `prompts/` (one per lane plus shared
`sections/`); every brief carries the principles of DESIGN.md section 1 verbatim, and the tests
enforce it.

## Licence

Apache License 2.0, as TauCetiTools. See [LICENSE](LICENSE).
