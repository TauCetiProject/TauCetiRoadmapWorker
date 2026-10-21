# TauCetiRoadmapWorker

`tauceti-roadmap` plans [Tau Ceti](https://github.com/TauCetiProject/TauCeti) roadmaps with agents
that operators run on their own machines. Humans file targets on the job board; workers triage them,
design the roadmaps, write the roadmap packages, review each other's work independently, and port
accepted packages to [TauCetiRoadmap](https://github.com/TauCetiProject/TauCetiRoadmap) as draft
pull requests. Nothing merges into the roadmap repository without a human.

**Want a roadmap?** Open an issue on the job board,
[TauCetiProject/TauCetiRoadmapJobs](https://github.com/TauCetiProject/TauCetiRoadmapJobs/issues/new/choose),
with the "Target" template: the statement or topic you want a roadmap for, why it matters for the
library, and the sources you know. That is the whole hand-off; the workers do the rest, and the
issue shows you each stage. Issues on this repository are for the tool itself.

The contract is [docs/DESIGN.md](docs/DESIGN.md), the operators' and maintainers' guide is
[docs/OPERATORS.md](docs/OPERATORS.md), and the sources rules are [docs/SOURCES.md](docs/SOURCES.md).

## Install

You need `git`, `gh`, `uv` and at least one agent CLI (Codex, Claude, or both). The port, red-team
fix and gate lanes also need an `elan`/`lake` toolchain.

```bash
gh auth login                      # the GitHub account your workers act as
codex login                        # and/or: claude auth login
uv tool install git+https://github.com/TauCetiProject/TauCetiRoadmapWorker

CONF=https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapJobs/main/tauceti-roadmap.toml
tauceti-roadmap --conf $CONF doctor
```

`doctor` checks the tools, your `gh` login, access to the repositories, the claims namespace, the
state directory and the sources cache, the briefs, PDF text extraction, OCR and the sandbox, and
exits 1 on any failure. The configuration is a human-owned `tauceti-roadmap.toml`; every field is
explained in [tauceti-roadmap.toml.example](tauceti-roadmap.toml.example). You need write access
only to your own forks, which are created on first use.

## Run a worker

```bash
tauceti-roadmap --conf $CONF work --worker-id alice-1 --loop                    # the most useful job, round after round
tauceti-roadmap --conf $CONF work --worker-id alice-2 --only review,redteam --loop
tauceti-roadmap --conf $CONF work --worker-id alice-3 --agent claude --loop      # pin the agent family
tauceti-roadmap --conf $CONF work --worker-id alice-1 --dry-run                  # write the brief, run nothing
```

Each round takes the most useful job this worker may do, leases it, runs the lane's agent in fresh
checkouts, checks the result, and submits it to the board as a pull request; a round that stops
early puts the job back. `--worker-id` names the worker (its folder, branch prefix and lease owner);
run several with distinct ids. `--only` limits it to some job kinds; `--agent` pins the family and
leaves the model to the lane. Ctrl-C (or a stop signal) ends the round cleanly.

To keep a worker running in the background, a user service does well:

```bash
systemd-run --user --unit rmw-alice-1 --collect --property=Restart=always --property=RestartSec=30 \
  --setenv=PYTHONUNBUFFERED=1 \
  --setenv=PATH="$HOME/.elan/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  /usr/bin/bash -lc "exec tauceti-roadmap --conf $CONF work --worker-id alice-1 --loop \
    >> $HOME/.local/state/tauceti/projects/TauCetiRoadmap/logs/alice-1.log 2>&1"
systemctl --user stop rmw-alice-1                                   # stop it (the job is released)
```

A worker exits by itself after a reinstall of the tool (at the end of its current round), and the
service brings it back on the new build. If several `gh` accounts are logged in on the machine, pin
the one the worker uses (for example `--setenv=GH_TOKEN=...`; the token is then readable in the
unit's environment by your user).

Watch it:

```bash
tauceti-roadmap --conf $CONF tail --worker-id alice-1      # its rounds and the agent's live output
tauceti-roadmap --conf $CONF status                        # the board's jobs by state, ages, claimants
```

## Where things live

Everything a worker writes stays on your machine, under the project's state directory
`~/.local/state/tauceti/projects/<project>/` (`$TAUCETI_PROJECT_STATE` or `$XDG_STATE_HOME`
override it; it must not lie inside a git checkout):

| Path | What |
| --- | --- |
| `workers/<id>/board`, `workers/<id>/content` | the worker's checkouts of the board and the roadmap repository, reset to their main branches every round |
| `workers/<id>/jobs/<JOB>/brief.md` | the brief the agent got |
| `workers/<id>/jobs/<JOB>/run-N.out`, `run-N.err` | the agent's output, run by run |
| `workers/<id>/jobs/<JOB>/scratch/` | the agent's scratch space, and a pending `needs-references.toml` |
| `workers/<id>/jobs/<JOB>/unsubmitted/` | what a round left behind without submitting it |
| `workers/<id>/rounds.log`, `attempts.json` | the round log `tail` follows; failures per job |
| `sources/<sha256>.pdf`, `sources/<sha256>.txt` | the sources cache: each text under its hash, with a line-numbered extraction beside it |
| `logs/` | where the service example above writes the worker's output |

The full list is in [docs/OPERATORS.md](docs/OPERATORS.md#the-state-directory).

## Sources

Agents fetch the sources they cite themselves, through `tauceti-roadmap sources fetch`, and only
from legal places: open repositories (arXiv, HAL, Numdam, GDZ, EuDML, Project Euclid open content,
the Stacks project), publisher open archives, author pages, institutional repositories, and the
texts you hold. Pirate mirrors are refused and paywalls are never circumvented; a published theorem
behind a paywall may be cited by its locator alone (`uncertified`). Each text is kept in your
sources cache under its SHA-256, and the roadmap's `sources.toml` records where it came from, its
hash, its licence class and the locators the roadmap cites.

**Sources never enter a repository, a pull request or an issue.** The cache on your machine is
their only copy; reports and READMEs quote locators (`AB21 line 120`, a theorem or page number),
never passages. The submission gates refuse PDF, DjVu, PostScript and EPUB files, extracted text,
manifests that embed text, and long pasted passages.

**Texts you hold.** The project's configuration sets `[sources] local_dirs = ["~/tauceti-sources"]`:
put a paper you hold there under a plain file name, such as `~/tauceti-sources/Kettle1967.pdf`
(create the directory if it does not exist). The sources service looks there first, and the file
never leaves your machine. To give its hash in an answer, run
`sha256sum ~/tauceti-sources/Kettle1967.pdf`.

**Answering a needs-references request.** When a worker cannot read a source legally it parks the
job in `state:needs-references` and posts what is missing on the job's issue. Answer each key in a
comment, one line per key:

```text
source: BLR90 arXiv:2101.00001            an alternative legal source you checked (filers and operators)
local: Ket67 Kettle1967.pdf               a text operators hold in [sources] local_dirs (sha256: <hex> optional)
waive: Tan71                              cite it by its locator alone
```

Never attach a file: attachments are not read. After a `local:` answer, only a worker whose
operator holds that file (by name, or by hash when one is given) takes the job up; other workers
leave it alone. The board's intake records the answers in `sources.toml` and makes the job
available again once every key is answered.

## The jobs

| Kind | What the lane does |
| --- | --- |
| `target` | triage a filed target: a new roadmap, a Part II, a duplicate, or a merge into another open target or design |
| `design` | the roadmap plan: layers, targets with locators, suppliers by name and section, consumers, the sources manifest |
| `package` | the roadmap folder: README, `Suggested.lean`, `metadata.toml` |
| `review` | an independent review of a design or a package: a seeded sample of locators, an adversarial pass, duplication and supplier-order audits |
| `fix` | resolve a review's findings in the package files, or a red-team round's findings on the pull request branch |
| `port` | copy the accepted package into the roadmap repository, run the gates, open the draft pull request |
| `redteam` | independent passes on the draft pull request by different agents, each round followed by a fix |
| `gate` | answer the human reviewers of the draft pull request: fix, push, reply finding by finding |

## Filing targets

A target names a statement or topic, why it matters for the library, the sources you know and a
suggested name. File it with the "Target" issue template on the job board,
[TauCetiProject/TauCetiRoadmapJobs](https://github.com/TauCetiProject/TauCetiRoadmapJobs/issues/new/choose),
or from the command line. For now, targets are filed by members of the TauCetiProject `humans` team.

```bash
tauceti-roadmap --conf $CONF targets file --statement "..." --why "..." --name PrismaticCohomology
tauceti-roadmap --conf $CONF targets file-many --from targets.toml           # dry run
tauceti-roadmap --conf $CONF targets file-many --from targets.toml --yes     # file them
```

`targets.toml` holds `[[target]]` tables with `statement`, `why` and optionally `sources`, `name`,
`licence` and `title` (a JSON Lines file with the same fields works too); a target whose roadmap
name the board already has is skipped. The board's maintenance (`board sync`) turns filed targets
into jobs; see [docs/OPERATORS.md](docs/OPERATORS.md#board-maintenance).

## Reviews

Every design and package is reviewed by an agent of another operator (or, where the project allows
it, of another agent and model) before it moves on, and every fix round gets a fresh review; a
fixer's own acceptance never counts. Each design and package is reviewed on its own pull request
on the job board, which stays open through its reviews, their fixes and (for a package) two
red-team rounds; only then is it merged and ported, and the roadmap pull request gets human
reviews. Reviews and red-team passes run on the review models the configuration pins (Fable on
Claude, Astra on Codex), whichever family a worker runs. A browser model such as ChatGPT Pro can
review too: the pinned "Review queue" issue on the board lists what is open, each pull request's
first comment is the brief, and the review is one comment in the shape the brief gives (a first
line `Review (<agent>) of <sha7>`, a `Verdict:` line and numbered findings), which the board reads
like a worker's when it comes from a login the project lists.

## Development

```bash
uv venv && uv pip install -e . pytest       # add '.[pdf]' to exercise the pypdf fallback
.venv/bin/python -m pytest
python3 -m compileall -q tauceti_roadmap_worker
```

The package uses the standard library only; the optional extras are `pdf` (pypdf, when poppler's
`pdftotext` is missing) and `ocr` (RapidOCR with pypdfium2). The tests never touch the network.
Briefs live in `prompts/`, one per lane plus shared `sections/`; every brief carries the principles
of DESIGN.md section 1, and the tests enforce it.

## Licence

Apache License 2.0, as TauCetiTools. See [LICENSE](LICENSE).
