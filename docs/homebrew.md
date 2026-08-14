

# Homebrew Services

nyxGPT provides two persistent background services using **Homebrew services**:

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

This is the recommended way to keep both services running locally without keeping terminals open.

A Homebrew install has no repository checkout, so the documentation you would
otherwise read from `docs/` ships inside the package instead: once
`nyxgpt-web` is running, the whole tree is served in the web UI under
**Support → Docs**, offline and matching the installed version. The same menu's
**File an Issue** item reports a problem with your version and platform already
filled in. See [ui.md](ui.md#support-menu).

---

## Prerequisites

- macOS
- Homebrew installed

You do **not** need a Python environment of your own, and you must not try to
`pip install nyxgpt` on macOS: Homebrew's Python is
[PEP 668](https://peps.python.org/pep-0668/) externally managed, so pip refuses
the install outright. Each formula ships its own self-contained environment
(see [How the keg venv is built](#how-the-keg-venv-is-built)), which is the
whole point of the brew path.

---

## Trusting the tap (one-time, required)

Homebrew gates formulas from third-party taps behind an explicit trust step.
Until the tap is trusted, `brew install` stops instead of installing, so the
trust command belongs in every macOS install sequence, right after `brew tap`:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt
```

It is per tap and per machine, not per formula or per version: trust
`dkblinux98/nyxgpt` once and every formula in it — stable, release candidate,
api, web — installs and upgrades without repeating it. On a Homebrew old
enough not to gate third-party taps the step is simply unnecessary.

`dkblinux98/nyxgpt` is the tap's name everywhere Homebrew asks for one. The
repository behind it is `dkblinux98/homebrew-nyxgpt` — Homebrew's naming
convention is to strip the `homebrew-` prefix, and both spellings resolve to
the same tap in `brew tap`, so `HOMEBREW_TAP_REPO` naming the repo and the
commands here naming the tap are the same thing.

---

## Homebrew tap

The nyxGPT Homebrew formula lives in a custom tap:

```
dkblinux98/nyxgpt-local
```

Add the tap:

```bash
brew tap dkblinux98/nyxgpt-local
```

`nyxgpt ops install` generates this tap's formulas **locally from a repo
checkout** (it vendors `pyproject.toml`/`src/nyxgpt/` and `web/` into a
tarball and points the formula at it via a `file://` URL) -- see
[Remote tap](#remote-tap) below for the repo-less alternative.

Only source is vendored: gitignored build output (`node_modules`, `.next`,
...), VCS metadata, interpreter bytecode caches (`__pycache__`, `.pyc`,
`.pyo`) and `.DS_Store` are excluded from every tarball, so a checkout you
have been developing in produces the same artifact as a fresh one.

---

## Remote tap

For a machine that has never cloned nyxGPT, `nyxgpt ops install`'s local
`file://` tap above isn't an option -- there's no checkout to vendor a
tarball from. `.github/workflows/release-artifacts.yml` publishes a
**remote** tap instead (#3622): on every GitHub Release, it builds the same
`nyxgpt-api`/`nyxgpt-web` source tarballs, attaches them as release assets,
and pushes stamped formulas (real `url`/`sha256`/`version`, no placeholders)
to a separate tap repository the owner provisions once.

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time per machine -- see above
brew install nyxgpt-api nyxgpt-web
```

Everything past `brew install` (services, wrappers, config resolution) is
identical to the local tap above -- the remote formulas
(`homebrew/tap/*.rb.tmpl`) are near-duplicates of the local ones
(`homebrew/nyxgpt-api.rb` / `nyxgpt-web.rb`), differing only in where the
tarball's `url` points.

**Owner setup (one-time, required before this tap exists):**

1. Create a public GitHub repo named `homebrew-nyxgpt` (the `homebrew-`
   prefix is Homebrew's tap-naming convention).
2. Add a repo variable `HOMEBREW_TAP_REPO` = `dkblinux98/homebrew-nyxgpt`
   and a secret `HOMEBREW_TAP_TOKEN` (a PAT with push access to that repo)
   to this repo's Actions settings.

Until both exist, `release-artifacts.yml`'s `homebrew-tap` job still builds
and attaches the tarballs to each GitHub Release and uploads the stamped
formulas as a workflow artifact -- it just skips the tap push (logged as a
notice, not a failure) until the owner completes the two steps above.

**The tap serves stable releases from v2.1.0 onward.** The tap machinery
(#3622) postdates 2.1.0, so that release was published to the tap by the
backfill below (#3737) rather than by its own release run; releases from
3.0.0 on are pushed by their ceremony automatically. Earlier releases
(1.0.0, 2.0.0) are not in the tap and will not be added -- `brew install
nyxgpt-api` resolves to the latest stable release, which is what the tap
exists to serve.

### Backfilling a release that predates the tap

A tag cut before the tap machinery has no copy of it: 2.1.0's tree contains
neither `scripts/build_homebrew_artifacts.py` nor `homebrew/tap/*.rb.tmpl`,
which is why dispatching the workflow against it originally failed outright.
The job therefore takes **two checkouts** rather than one:

| Tree | Comes from | Provides |
| --- | --- | --- |
| workspace root | the ref the run started on (release commit, or the branch a backfill is dispatched from) | the release tooling: build script, formula templates, `nyxgpt.release_tarball` |
| `release-source/` | the target tag | the service source the tarballs vendor |

`scripts/build_homebrew_artifacts.py ... --source-root release-source` is
what joins them, so the published tarballs are the tag's real code while the
formulas are stamped from templates that tag never contained. For a normal
release the two checkouts are the same commit and nothing changes.

Both tap jobs (this one and the rc channel's `homebrew-tap-rc`) run the
script with **no `pip install` step**: a checkout and `setup-python` are the
whole setup. That is only true because the script imports the tarball
builder from `nyxgpt.release_tarball`, which is stdlib-only by design -- it
was split out of `nyxgpt.ops` after importing that module dragged in
`httpx`/`pynacl` and killed the rc tap job with a `ModuleNotFoundError`
*after* the candidate had already been published to PyPI (#3741). Adding a
third-party import to that module's closure means adding an install step to
every job that runs the script; `tests/unit/test_build_homebrew_artifacts.py`
fails if the two ever disagree.

To backfill a release, dispatch **Release Artifacts** from a branch that has
the tooling (e.g. the active release branch):

```
version:  2.1.0        # must already exist as a published GitHub Release
tap_only: true         # skip the images and the PyPI smokes -- tap only
```

The run builds `nyxgpt-api-2.1.0.tar.gz` / `nyxgpt-web-2.1.0.tar.gz` from
the 2.1.0 source, attaches them to the existing 2.1.0 release (`--clobber`),
and pushes stamped `nyxgpt-api.rb` / `nyxgpt-web.rb` to `HOMEBREW_TAP_REPO`.
Verify on a clean Mac:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt
brew install nyxgpt-api nyxgpt-web
brew services start nyxgpt-api && brew services start nyxgpt-web
```

Two guardrails make this safe to point at any tag:

* **Stable formulas only from real releases.** The job refuses a tag whose
  GitHub Release is a draft or a prerelease, and the build script refuses
  any version that is not a plain `X.Y.Z` -- a release candidate cannot
  reach the stable formulas, whatever is typed into the dispatch form.
* **`@<line>rc` formulas are never touched.** The stable channel writes only
  `nyxgpt-api.rb` / `nyxgpt-web.rb`; the job asserts exactly those two files
  were stamped before it pushes, and it copies nothing else into the tap.

---

## Release-candidate formulas (rc channel)

Acceptance testing has to be able to install *unreleased* code on macOS the
same repo-less way a release installs -- otherwise the brew path can only
ever be accepted one release behind. Cutting a release candidate therefore
stamps the tap too (#3727):

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # same one-time step as the stable formulas
brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc

brew services start nyxgpt-api@3.0.0rc
brew services start nyxgpt-web@3.0.0rc
```

The formula is named for the **release line** it is a candidate for, so
`3.1.0`'s candidates are `nyxgpt-api@3.1.0rc` -- a different formula that no
`brew upgrade` can slide a machine onto (#3735). Every RC of a line
(`3.0.0rc1`, `3.0.0rc2`, ...) restamps the same `@3.0.0rc` formula, which is
what makes `brew upgrade nyxgpt-api@3.0.0rc` the way to move to the round's
newest candidate.

The prerelease the candidate formulas point at is **created with its
tarballs attached**, in one `gh release create` call. Releases in this
repository are immutable -- a published release can never gain an asset, and
creating it first and uploading afterwards is what left `3.0.0rc3` published
with no tarballs and the tap empty (#3747). The job reads the release back
and refuses to stamp a formula unless both tarballs are on it, and it runs
`scripts/supersede_incomplete_rc_releases.sh <line>` first, which deletes any
incomplete candidate release of that line (or, when the platform refuses the
delete, marks it superseded in its notes). Run that script by hand the same
way if a leftover ever needs clearing:

```bash
scripts/supersede_incomplete_rc_releases.sh 3.0.0 --repo dkblinux98/nyxGPT --dry-run
```

Everything past `brew install` is identical to the stable formulas -- same
tarball contents, same self-contained Cellar keg, same wrappers, same
service names. `scripts/build_homebrew_artifacts.py --channel rc` derives
the candidate formulas from the same `homebrew/tap/*.rb.tmpl` templates, so
the two channels cannot drift about what the keg installs; only the class
name, the description and the conflict declaration differ.

### `brew install nyxgpt-api` is never affected

Homebrew has no pre-release semantics -- a tap serves whatever version its
formula names, and `brew install nyxgpt-api` takes it. So channel separation
lives in the **formula names**, not in a flag:

| | Stable | Release candidate |
| --- | --- | --- |
| Formulas | `nyxgpt-api`, `nyxgpt-web` | `nyxgpt-api@3.0.0rc`, `nyxgpt-web@3.0.0rc` |
| Written by | `release-artifacts.yml`, on a GitHub Release | `release-publish-pypi.yml`'s `homebrew-tap-rc` job, on an `rc` publish |
| Tarballs from | the release's GitHub Release | a GitHub **prerelease** for the RC (never "latest") |
| `brew install nyxgpt-api` resolves to | this | never this |

An `rc` publish never builds, copies or commits a stable formula file: the
job asserts none was produced, and the tap push refuses if a stable formula
would change. The `stable` channel never reaches the tap job at all -- the
stable formulas belong to the ceremony's `release-artifacts.yml` run.

### Why `@3.0.0rc` and not `@rc` or `-rc`

The version is in the name deliberately: a candidate formula is a candidate
for **one release line**, and naming it so means a machine on `@3.0.0rc` can
never be carried across to the next line's candidates by a `brew upgrade` --
those are a differently named formula, installed only on purpose. It is also
what lets the release ceremony retire a shipped line's candidates by name
(`scripts/retire_rc_formulas.sh`) while leaving a newer line's alone.

The spelling has to be digit-led. Homebrew's loader (`Formulary.class_s`)
translates `@` into `AT` only when a **digit** follows it: `python@3.12`
becomes the class `PythonAT312`, and `nyxgpt-api@3.0.0rc` becomes
`NyxgptApiAT300rc` -- both load. A bare `nyxgpt-api@rc` would become
`NyxgptApi@rc`, not a legal Ruby constant, so no class declaration inside
the file could satisfy the loader and `brew install nyxgpt-api@rc` would
fail with `Expected to find class NyxgptApi@rc`.
`scripts/build_homebrew_artifacts.py` refuses to stamp any formula name with
an unloadable `@`, so that mistake cannot come back.

### Switching a machine between channels

The candidate formulas declare `conflicts_with` their stable counterparts,
because both install the same `nyxgpt-api`/`nyxgpt-web` wrappers and the
same brew service names. Switching channels is an explicit uninstall, never
a silent swap:

```bash
# stable -> release candidate
brew services stop nyxgpt-api && brew uninstall nyxgpt-api
brew install nyxgpt-api@3.0.0rc && brew services start nyxgpt-api@3.0.0rc

# a newer candidate of the same line (same formula, restamped)
brew update && brew upgrade nyxgpt-api@3.0.0rc

# ...and back once the release is out
brew services stop nyxgpt-api@3.0.0rc && brew uninstall nyxgpt-api@3.0.0rc
brew install nyxgpt-api && brew services start nyxgpt-api
```

If the tap does not carry the stable formula the candidate names — a tap
whose stable formulas have not been published yet — `brew` warns that the
conflict refers to an unknown formula and **carries on installing**. The
warning is cosmetic and the declaration is deliberately left unconditional:
Homebrew resolves `conflicts_with` when it loads the formula and offers no
way to make one tolerant of a missing counterpart, so removing it to silence
the warning would trade a cosmetic message for the silent channel clobber it
exists to prevent. The name itself cannot go stale — it is derived from the
stable formula the same publishing script stamps
(`scripts/build_homebrew_artifacts.py`).

Candidate formulas are **acceptance-only**. They are not upgraded on a
schedule, carry no support expectation, and are removed from the tap by the
release ceremony the moment the line they are a candidate for ships. See
[docs/cloud.md](cloud.md#pypi-publishing-rc-and-stable) for cutting one
and for the equivalent pip/cloud flows.

---

## Installing the services

Install both service formulas:

```bash
# Add the tap (if not already added) and trust it once
brew tap dkblinux98/nyxgpt-local
brew tap-trust dkblinux98/nyxgpt-local

# Install both services
brew install nyxgpt-api
brew install nyxgpt-web
```

Each service installs a self-contained app into its own Cellar keg --
`nyxgpt-api` gets its own Python venv (`pip install`ed from the vendored
source, not the repo checkout's editable `.venv`), and `nyxgpt-web` gets its
own `npm ci && npm run build` output. Neither depends on the repo checkout
existing or staying in place afterwards. Each install also gets:
- A small wrapper script
- A Homebrew launch agent plist

`nyxgpt ops install` only re-runs `brew install`/`reinstall` when the
vendored source actually changed since the last install (checksum-compared);
otherwise it reports the existing install is already up to date and just
(re)starts the service.

### How the keg venv is built

The `nyxgpt-api` keg creates its venv with `python -m venv --without-pip` and
then has Homebrew's `python@3.12` install pip into it (`pip --python`). That
is deliberate: a plain `python -m venv` runs `ensurepip --upgrade
--default-pip`, which bootstraps pip from wheels vendored inside the
`python@3.12` keg — the only part of the install that depends on
Homebrew-managed keg state rather than on the tarball being installed. On a
stock Homebrew Mac that step exited 1 and took the whole `brew install` with
it, so it is no longer in the path at all.

### Why the build environment gets a `sitecustomize.py`

Before it creates the venv, the `nyxgpt-api` install block writes a
`sitecustomize.py` into `buildpath` and puts that directory on `PYTHONPATH`.
It repairs `platform.mac_ver()` when the OS lookup comes back empty.

That is not defensive decoration. On a stock Homebrew Mac `mac_ver()`
returned no release during the build, and pip parses that string **unguarded
in two separate places on every install** — `truststore`, its default TLS
backend since pip 24.2, and `packaging.tags.mac_platforms()`, which decides
which wheels are installable. Both do `int("")` and raise
`ValueError: invalid literal for int() with base 10: ''`, so pip could not
start at all and `brew install` died with it. There is no pip option that
avoids this: `InstallCommand.run` builds its session before it looks at
`--no-index`, and pip's own graceful-degradation guard catches `ImportError`
only, which a `ValueError` from a module body walks straight past.

A `sitecustomize` is the right shape because `site` imports it at interpreter
startup, so one file covers every interpreter the build starts — Homebrew's
python, the venv python that `pip --python` re-execs into, and pip's
build-isolation subprocesses. It asks `sw_vers` for the real release and only
falls back to a floor of `11.0` if that fails too; a Mac whose `mac_ver()`
already works is left completely alone, so the reported version never becomes
a lie that changes which wheels pip picks. The file lives in `buildpath` and
is gone once the keg is built — nothing ships it.

The shim is Python nested inside a Ruby heredoc, which no linter or import in
this repo would otherwise look at, so `build_homebrew_artifacts.py` extracts
and compiles it while stamping (a syntax error fails the release build), and
the unit suite executes the real extracted source rather than grepping it.

Both api formulas (the local one and the remote tap's template) carry this
same recipe, and a unit test asserts they cannot drift apart.
[`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml) installs
the formulas for real on a hosted `macos-15` runner — the working tree's
recipe on every formula change, and the published candidate from this tap
after every rc cut — and checks the keg's venv has a working pip, imports
`nyxgpt.app` and runs `nyxgpt --version`.

It also **injects** the empty-`mac_ver()` condition rather than waiting to
encounter it. A hosted runner answers `mac_ver()` normally, so an
install-only job goes green on the exact candidate that fails on a machine
that does not — which is what happened to rc5. The job now asserts the
recipe's pip bootstrap really does fail on the runner with the reported
`ValueError` when the fault is forced, and only then asserts that the
formula's own shim (read back out of the shipped formula, never a copy)
survives it.

---

## Managing the API service (nyxgpt-api)

### Start the API

Start the FastAPI backend as a background service:

```bash
brew services start nyxgpt-api
```

Verify status:

```bash
brew services info nyxgpt-api
```

### Restart and stop

Restart the API service:

```bash
brew services restart nyxgpt-api
```

Stop the API service:

```bash
brew services stop nyxgpt-api
```

### API logs

The application's own structured logs (requests, self-heal, canary, RAG, ...) are written to:

```
~/.nyxGPT/logs/api.log
```

Tail logs in real time:

```bash
tail -f ~/.nyxGPT/logs/api.log
```

If the service fails to start (e.g. a crash before Python logging is even
configured), check Homebrew's own service log instead -- it captures the
process's raw stdout/stderr from the moment it launches:

```bash
tail -f "$(brew --prefix)/var/log/nyxgpt-api.log"
tail -f "$(brew --prefix)/var/log/nyxgpt-api.err.log"
```

`nyxgpt ops logs api` (and its `GET /api/v1/self-heal/logs?service=api`
API equivalent) already tails all three as labeled sections, so a
pre-logging startup failure -- e.g. the `[api] host`/`[auth] enabled` bind
refusal -- shows up there too, without needing the raw paths above (#3629).

---

## Managing the Web UI service (nyxgpt-web)

### Start the Web UI

Start the Next.js web UI as a background service:

```bash
brew services start nyxgpt-web
```

Verify status:

```bash
brew services info nyxgpt-web
```

The web UI will be available at: `http://127.0.0.1:3000`

### Restart and stop

Restart the web service:

```bash
brew services restart nyxgpt-web
```

Stop the web service:

```bash
brew services stop nyxgpt-web
```

### Web UI logs

The web UI has no structured `nyxgpt.logging`-based logging yet (tracked by
#3430), so Homebrew's own service log is the only place to look. Written to:

```
$(brew --prefix)/var/log/nyxgpt-web.log
$(brew --prefix)/var/log/nyxgpt-web.err.log
```

Tail logs in real time:

```bash
# Standard output logs
tail -f "$(brew --prefix)/var/log/nyxgpt-web.log"

# Error logs
tail -f "$(brew --prefix)/var/log/nyxgpt-web.err.log"
```

---

## Managing both services together

### Start both services

```bash
brew services start nyxgpt-api
brew services start nyxgpt-web
```

Or use the `nyxgpt ops restart` command for a coordinated restart:

```bash
nyxgpt ops restart
```

### Stop both services

```bash
brew services stop nyxgpt-api
brew services stop nyxgpt-web
```

### Check status of all services

```bash
brew services list | grep nyxgpt
```

Example output:

```
nyxgpt-api  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist
nyxgpt-web  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web.plist
```

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **nyxgpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

Recommended startup order:

```bash
brew services start nyxgpt-api
# Wait a few seconds for API to be ready
brew services start nyxgpt-web
```

---

## Configuration

Both Homebrew services use the same configuration file as the CLI:

```
~/.nyxGPT/config.ini
```

### API configuration

API settings are in the `[api]` section:

```ini
[api]
host = 127.0.0.1
port = 8000
```

### Web UI configuration

Web UI settings are in the `[web]` section:

```ini
[web]
host = 127.0.0.1
port = 3000
api_base_url =  # Optional: override API URL
```

### Applying configuration changes

Some settings are hot-reloadable (take effect immediately):
- `default_model`
- `rag.enable_chat_context`
- `logging.level`
- `auth.enabled` and `auth.api_key`

Other changes require service restart:

```bash
# Restart API service
brew services restart nyxgpt-api

# Restart Web UI service
brew services restart nyxgpt-web

# Restart both
nyxgpt ops restart
```

---

## Ollama model store

`nyxgpt ops install` points native Ollama's model store at
`~/.nyxGPT/volumes/ollama/models` -- the same directory
Compose/Terraform's `ollama` container uses -- instead of Ollama's own
default `~/.ollama/models`, via the `OLLAMA_MODELS` environment variable
(never a symlink). This means a model pulled while running in any one local
launch mode (native, `nyxgpt ops install --terraform --local`, or Compose)
shows up in all of them, with no duplicate downloads.

This is applied via `launchctl setenv OLLAMA_MODELS ...`, plus a
`com.nyxgpt.ollama-env` LaunchAgent that reapplies it at every login (a bare
`launchctl setenv` only lasts for the current session). If you ever pulled
models natively before upgrading, `nyxgpt ops install` merges anything found
in the old `~/.ollama/models` into the shared store automatically, the first
time it runs, without overwriting anything already there (via a hardlink
when possible, so multi-GB blobs merge instantly with no extra disk use).

Because `com.nyxgpt.ollama-env` and Homebrew's own `ollama` LaunchAgent are
both `RunAtLoad`, launchd does not guarantee which runs first on a given
login. So the env-setting script also force-restarts the `ollama` brew
service every time it runs, closing the race regardless of ordering: if
Homebrew's agent already started `ollama serve` with the wrong env, this
restarts it with the right one; if it hasn't started yet, this just starts
it. `nyxgpt ops doctor` also compares the live `launchctl getenv
OLLAMA_MODELS` against the expected shared path, in case something else
(e.g. a manual `launchctl unsetenv`) causes drift between logins.

Kubernetes is out of scope for this unification (its own PVC, no bind-mount
to the host's home directory possible) -- tracked separately.

---

## Accessing the services

After starting both services:

- **API**: `http://127.0.0.1:8000`
- **Web UI**: `http://127.0.0.1:3000`
- **API docs**: `http://127.0.0.1:8000/docs`

---

## Troubleshooting

### Web UI can't connect to API

**Symptom**: Web UI shows "Connection Error" or "API Unavailable"

**Solutions**:

1. Verify API is running:
   ```bash
   brew services list | grep nyxgpt-api
   curl http://127.0.0.1:8000/health
   ```

2. Check API logs:
   ```bash
   tail -f ~/.nyxGPT/logs/api.log
   ```

3. Restart API service:
   ```bash
   brew services restart nyxgpt-api
   ```

### Service won't start

**Symptom**: `brew services start` succeeds but service isn't running

**Solutions**:

1. Check logs for errors -- Homebrew's own service logs capture a crash
   before the process even reaches Python/Node logging setup, so check
   these first regardless of which service failed:
   ```bash
   tail -f "$(brew --prefix)/var/log/nyxgpt-api.err.log"
   tail -f "$(brew --prefix)/var/log/nyxgpt-web.err.log"
   ```

2. Verify configuration is valid:
   ```bash
   cat ~/.nyxGPT/config.ini
   ```

3. Check port conflicts:
   ```bash
   lsof -i :8000  # API port
   lsof -i :3000  # Web UI port
   ```

4. Run `nyxgpt ops doctor` for health checks:
   ```bash
   nyxgpt ops doctor
   ```

### Node.js not found (Web UI)

**Symptom**: Web UI logs show "node: command not found"

**Solutions**:

1. Verify Node.js is installed via Homebrew (the `nyxgpt-web` formula
   `depends_on "node"` and finds it via `Formula["node"]`, not `[paths]` in
   config.ini):
   ```bash
   brew list node
   ```

2. Reinstall the `node` formula if it's missing, then reinstall `nyxgpt-web`:
   ```bash
   brew install node
   nyxgpt ops install
   ```

3. Restart web service:
   ```bash
   brew services restart nyxgpt-web
   ```

---

## Notes

- Both services run under your user account (not as root)
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- Homebrew services automatically restart both services on login
- Use `nyxgpt ops` commands for easier service management