## Working rules

- Work only in `__WORKDIR__` and your scratch directory `__SCRATCH__`. Edit only the files this brief
  names. Other folders and checkouts are read-only.
- Never push, never open or edit pull requests or issues, never post comments: the worker does every
  GitHub write after checking your result.
- Lean: never run a bare `lake build`, `lake update` or `lake exe cache get`, and never touch
  `lake-manifest.json` or `lean-toolchain`. The only Lean commands you may run are given in this
  brief. Elaboration takes minutes; run it when it matters, one at a time, and leave nothing running.
- Do the mathematics, every verdict and every edit yourself. If your agent can start helpers, use
  them only to locate text, and read every quoted line yourself before relying on it.
- Keep a worklist in your scratch directory and save as you go, so partial work survives.
- In everything you write, refer to files by repository-relative paths only (`packages/<Name>/README.md`,
  `TauCetiRoadmap/<Name>/Suggested.lean`) and to sources as `<source id> line N`. The absolute paths
  in this brief are for your tools: never write an absolute path, a home directory, the worker's
  state directory or a temporary path into a report, a README, a Lean file or the manifest. A
  submission with a machine path is refused.

## Tools

__TOOLS__
