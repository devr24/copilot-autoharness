# Publishing Copilot Harness to GitHub

This guide takes the project from a local folder to something others can install with
`copilot plugin marketplace add OWNER/REPO`. Steps marked **(you)** need your GitHub account.

## What gets published, and what does not

Copilot Harness has two parts, and a Copilot plugin only carries one of them:

| Part | How users get it |
|---|---|
| Hook manifest (`plugin.json`, `hooks/hooks.json`) | `copilot plugin install` from your marketplace |
| The `harness` executable the hooks run | `pip`/`pipx` install (from PyPI or directly from GitHub) |

Hooks are spawned without a shell, so `harness` must be a real executable on `PATH`. Publishing
only the plugin gives users hooks that fail with `spawn harness ENOENT`. Always document both steps
together (the README already does) and tell users to run `harness doctor`.

## 1. Pre-flight checks

1. Run the tests: `PYTHONPATH=src python -m unittest discover -s tests`. They include
   `tests/test_release.py`, which fails if the version differs between `pyproject.toml`,
   `src/harness/__init__.py`, `plugin.json` and `.github/plugin/marketplace.json`, or if a personal
   `C:\Users\<name>` path has crept into the README, docs or manifests.
2. Check authorship and licence. `LICENSE` is MIT; `pyproject.toml` lists the author as
   "Copilot Harness contributors". Replace with your name or organisation if you prefer, and add a
   `repository` / `homepage` URL under `[project.urls]` once the repo exists.
3. Pick the repository name. The README examples use `OWNER/REPO`; keep that placeholder until you know it.
4. Make sure no local state is included: `.gitignore` excludes `*.sqlite3`, `debug.log*`,
   `reflection.log`, `build/`, `dist/`, `*.egg-info/` and virtual environments. Harness stores its
   data outside the project (`%LOCALAPPDATA%\copilot-harness` or `~/.local/state/copilot-harness`),
   so nothing sensitive should be in the folder, but check `git status` before the first push.

## 2. Create the repository **(you)**

The project folder is not a git repository yet.

```powershell
cd "C:\path\to\copilot-harness"
git init -b main
git add .
git status          # review the file list: no logs, databases, or venvs
git commit -m "Initial release 0.1.0"
```

Create an empty repository on github.com (no README/licence/gitignore, since they already exist), then:

```powershell
git remote add origin https://github.com/OWNER/REPO.git
git push -u origin main
```

With the GitHub CLI, `gh repo create OWNER/REPO --public --source . --push` does both. Choose
**private** first if you want to test with colleagues before going public; `copilot plugin
marketplace add` works with repositories the user can access.

## 3. Turn on CI

`.github/workflows/ci.yml` runs the unit tests on Ubuntu, macOS and Windows with Python 3.11 and
3.13, then installs the package and runs `harness --version` and `harness doctor`. This is also the
first real test of the background-process code on Linux and macOS, which has only been run on Windows
so far. Expect to fix something on the first run; the detached-process path
(`start_new_session`) and the POSIX `sh` reflector command are the likeliest spots. Do not release
until the matrix is green.

## 4. Verify the marketplace from a clean state **(you)**

The marketplace manifest uses `"source": "."` (the plugin is the repository root). That was verified
with a local-directory marketplace; it has **not** been verified against a remote GitHub marketplace.
Test it before announcing anything:

```powershell
pipx install "git+https://github.com/OWNER/REPO.git"     # or: py -m pip install .
copilot plugin marketplace add OWNER/REPO
copilot plugin install copilot-harness@copilot-harness-marketplace
harness config init
harness doctor
```

Then start a Copilot session in a throwaway repository and confirm `harness status` shows a captured
session. If the remote install fails to resolve the plugin, move the plugin into a subfolder
(for example `plugins/copilot-harness/` containing `plugin.json` and `hooks/`) and set `source` to
that path in `.github/plugin/marketplace.json`, as in the GitHub docs example. Update
`tests/test_release.py` if you move files.

Copilot has deprecated installing plugins directly from a path or URL; keep the marketplace route as
the documented one.

## 5. Release 0.1.0

```powershell
git tag v0.1.0
git push origin v0.1.0
```

Then on GitHub: Releases → Draft a new release → choose the tag → generate notes. Describe the
known limits (README "What it does not do yet"), the Windows-first testing history and the
data-handling note (conversation text is sent to the Copilot model provider for reflection).

For each later release bump the version in all four places (the test enforces this), run the tests,
update the README if behaviour changed, tag and release.

## 6. Optional: publish to PyPI so `pipx install copilot-harness` works

1. Check the name is free at pypi.org/project/copilot-harness (if taken, rename the distribution in
   `pyproject.toml`; the command stays `harness`). Avoid names that imply official GitHub endorsement.
2. Build: `py -m pip install build twine; py -m build` → `dist/`.
3. Test upload first: `py -m twine upload --repository testpypi dist/*`.
4. Prefer PyPI **trusted publishing** from a GitHub Actions workflow on tag push, so no API token is
   stored. Configure it on PyPI (project → Publishing) and add a workflow using
   `pypa/gh-action-pypi-publish`.
5. Update the README install step to `pipx install copilot-harness`.

## 7. Repository hygiene **(you)**

- Enable branch protection on `main` (require PR + passing CI) and 2FA on your account.
- Turn on secret scanning and push protection (free for public repos).
- Add a `SECURITY.md` with a private reporting route. This tool handles conversation text, so
  vulnerability reports matter.
- Do not attach `debug.log` or `reflection.log` to public issues without review.

## 8. Is it ready to announce?

Reasonable to share as a **0.1 preview** once CI is green, the remote marketplace install is
verified, and you have run it for a while on your own work. Say plainly that skill use is detected
from an undocumented transcript record, that the reflector is a deny-listed Copilot call, and that
only local trusted-skill export/hash verification is implemented; shared team/enterprise promotion
remains design-only (see [enterprise.md](enterprise.md)).
