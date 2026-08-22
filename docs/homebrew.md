

# Homebrew Services

nyxGPT provides two persistent background services using **Homebrew services**:

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

This is how both stay running locally without keeping terminals open.

> **Start the stack with `nyxgpt up`, not `brew services start`.**
>
> The two kegs above are two of the stack's parts. `nyxgpt up` reconciles and
> starts all of them — the API, the web UI, Ollama, the `nyxgpt-cassandra`
> container and the observability services — waits for health and prints the
> web UI URL. It also starts the two Homebrew services for you, so they still
> restart at login.
>
> `brew services start nyxgpt-api` starts that one service and nothing else.
> Homebrew prints that command after `brew install` because the formulas
> declare a `service` block; on its own it produces a stack that reports
> `ollama: unreachable` and `cassandra: unreachable`, with observability
> absent and the web UI unable to load sessions (#3854). The formulas' own
> caveats now say so at install time. Use `brew services` to control an
> individual service **after** `nyxgpt up` has set the stack up — the
> per-service sections below are written for that case, not for first start.

A Homebrew install has no repository checkout, so the documentation you would
otherwise read from `docs/` ships inside the package instead: once
`nyxgpt-web` is running, the product documentation is served in the web UI
under **Support → Docs**, offline and matching the installed version. The same menu's
**File an Issue** entry reports a problem, requests a feature or asks a
question without leaving the app: you answer on a nyxGPT page, it files the
ticket with your version and platform attached, and shows you a link to it. See [ui.md](ui.md#support-menu).

---

## Prerequisites

- macOS, up to date within its major version — see below
- Homebrew installed

**Keep macOS current within its major release.** Homebrew tags bottles by
macOS *major* version, so a machine running an older minor release can be
served a `python@3.12` bottle built against a newer one, and that bottle's
`pyexpat` cannot resolve the system `libexpat`. The formulas detect it and
refuse to build rather than failing obscurely later — see
[When the install refuses to build](#when-the-install-refuses-to-build).

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

Homebrew spells this subcommand `brew tap-trust <tap>` on some builds and
`brew trust <tap>` on others; if one is rejected as an unknown command, run
the other — the error `brew install` prints names the spelling your Homebrew
wants. Take the whole-tap form either way. Homebrew also offers a grant
scoped to a single formula, and it is not enough here: resolving a
`conflicts_with` **loads** the formula it names, and a grant scoped to the
formula on the command line leaves that counterpart untrusted, so the install
aborts (#3770). That applies to both channels since #3853 — installing a
candidate loads the stable formula, and installing the stable loads its
line's candidate.

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
tarball and points the formula at it via a `file://` URL). Run on a machine
with no checkout, it installs from the published
[remote tap](#remote-tap) instead -- same formulas, published source (#3759).

Only source is vendored: gitignored build output (`node_modules`, `.next`,
...), VCS metadata, interpreter bytecode caches (`__pycache__`, `.pyc`,
`.pyo`) and `.DS_Store` are excluded from every tarball, so a checkout you
have been developing in produces the same artifact as a fresh one.

### Both taps on one machine

A machine that has tested the published tap alongside the locally built one
carries `dkblinux98/nyxgpt` **and** `dkblinux98/nyxgpt-local`, and both define
`nyxgpt-api`/`nyxgpt-web`. Homebrew refuses a bare name in that state rather
than picking one:

```
Error: Formulae found in multiple taps:
         dkblinux98/nyxgpt-local/nyxgpt-api
         dkblinux98/nyxgpt/nyxgpt-api
Please use the fully-qualified name to refer to the formula.
```

So `nyxgpt ops` never names a formula bare (#3861). Install sites always
passed `<tap>/<formula>`; the lookup and lifecycle calls (`brew list`, `brew
services start`/`stop`/`restart`) now qualify it too, reading the owning tap
from the installed keg's own `INSTALL_RECEIPT.json` rather than guessing one.
Nothing here asks the operator to run brew directly — the qualification
matters because the wrapped commands are the recovery path, and a wrapped
command that cannot resolve its own formula is not one.

---

## Remote tap

For a machine that has never cloned nyxGPT, `nyxgpt ops install`'s local
`file://` tap above isn't an option -- there's no checkout to vendor a
tarball from, which is why `nyxgpt ops install` falls back to this tap
there. `.github/workflows/release-artifacts.yml` publishes a
**remote** tap instead (#3622): on every GitHub Release, it builds the same
`nyxgpt-api`/`nyxgpt-web` source tarballs, publishes them as the assets of a
`<version>-homebrew` release (see [below](#where-the-tarballs-are-published)),
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
and publishes the tarballs for each release and uploads the stamped
formulas as a workflow artifact -- it just skips the tap push (logged as a
notice, not a failure) until the owner completes the two steps above.

### Where the tarballs are published

Not on the release named by the version -- on a release of its own, tagged
**`<version>-homebrew`** (e.g. `2.1.0-homebrew`), created with both tarballs
attached in the same `gh release create` call. That is what the stable
formulas' `url` points at.

Releases in this repository are **immutable**: once published, a release can
never gain or change an asset. The release ceremony publishes `X.Y.Z` before
these tarballs are built, so uploading them onto it afterwards can only
return `HTTP 422: Cannot upload assets to an immutable release` -- which is
exactly how both 2.1.0 backfill runs died, and how every later release's tap
push would have died too (#3763, the same immutability class as #3747). The
sidecar release is marked prerelease and never "latest", so it is not a
second release of nyxGPT and cannot re-trigger `release-artifacts.yml`.

A release that already carries both tarballs is served from that release
instead, so nothing already published moves. `nyxgpt ops install`'s artifact
path looks in both places for the same reason (`_release_asset_urls` in
`src/nyxgpt/ops.py`), and the job refuses to push formulas it has not read
the assets back for.

When the tarballs are already published -- a re-run, or a release from before
this machinery -- the formulas are stamped against the **downloaded assets**,
not against a fresh build (`--tarballs-from`). Tarball builds are not
byte-reproducible: gzip embeds a build timestamp and the tar members carry the
checkout's mtimes, so two builds of identical source have different `sha256`s.
Re-stamping from a rebuild would therefore publish formulas whose checksum
cannot match the bytes brew downloads, on every machine that taps -- and
immutability means the served bytes could never be corrected. The verify step
downloads each asset and hard-fails unless its digest is what the formula was
stamped with, so this cannot regress silently.

**The tap serves stable releases from v2.1.0 onward.** The tap machinery
(#3622) postdates 2.1.0, so that release reaches the tap through the
backfill below (#3737) rather than through its own release run; releases from
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
release the two checkouts are the same commit and nothing changes. (A run
whose tarballs are already published stamps `--tarballs-from` those assets
instead and vendors nothing -- see [above](#where-the-tarballs-are-published);
the `release-source/` checkout is then unused.)

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
the 2.1.0 source, publishes them on the `2.1.0-homebrew` release
([above](#where-the-tarballs-are-published) -- the 2.1.0 release itself is
immutable and can never take them), and pushes stamped `nyxgpt-api.rb` /
`nyxgpt-web.rb` to `HOMEBREW_TAP_REPO`. Verify on a clean Mac:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt
brew install nyxgpt-api nyxgpt-web
nyxgpt up
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

<a id="candidate-channel"></a>

## Release-candidate formulas (rc channel)

Acceptance testing has to be able to install *unreleased* code on macOS the
same repo-less way a release installs -- otherwise the brew path can only
ever be accepted one release behind. Cutting a release candidate therefore
stamps the tap too (#3727):

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # same one-time step as the stable formulas
brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc

nyxgpt up
```

`nyxgpt up` is the same command on either channel — the candidate kegs install
the same `nyxgpt` CLI, and it starts whichever of the two formulas is present
along with the rest of the stack.

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
tarball contents, same self-contained Cellar keg, same wrappers.
`scripts/build_homebrew_artifacts.py --channel rc` derives the candidate
formulas from the same `homebrew/tap/*.rb.tmpl` templates, so the two
channels cannot drift about what the keg installs; only the class name, the
description and the conflict declaration differ.

### The service is named after the formula

Homebrew names a service after the formula that installed it, so a candidate
install registers **`nyxgpt-api@3.0.0rc`**, not `nyxgpt-api`:

```
$ brew services list
Name               Status
nyxgpt-api@3.0.0rc started
nyxgpt-web@3.0.0rc started
```

That is normal and correct — it is what lets both channels be installed and
started independently. nyxGPT resolves a component's service name from
`brew services list` rather than assuming the stable name, so
`nyxgpt ops status`, `nyxgpt up`'s health wait, `nyxgpt ops restart/stop`
and the self-heal watchdog all act on whichever formula is actually
registered (`src/nyxgpt/brew_services.py`). When a machine carries both — an
older release's keg alongside a candidate — the one that is **running** is
the one reported.

Log files are unchanged: both channels write
`$(brew --prefix)/var/log/nyxgpt-api.log` and `nyxgpt-web.log`, so
`nyxgpt ops logs api` needs no channel-specific path.

### `brew install nyxgpt-api` is never affected

Homebrew has no pre-release semantics -- a tap serves whatever version its
formula names, and `brew install nyxgpt-api` takes it. So channel separation
lives in the **formula names**, not in a flag:

| | Stable | Release candidate |
| --- | --- | --- |
| Formulas | `nyxgpt-api`, `nyxgpt-web` | `nyxgpt-api@3.0.0rc`, `nyxgpt-web@3.0.0rc` |
| Written by | `release-artifacts.yml`, on a GitHub Release | `release-publish-pypi.yml`'s `homebrew-tap-rc` job, on an `rc` publish |
| Tarballs from | the release's `<version>-homebrew` release (never "latest") | a GitHub **prerelease** for the RC (never "latest") |
| `brew install nyxgpt-api` resolves to | this | never this |

An `rc` publish never builds, copies or commits a stable formula file: the
job asserts none was produced, and the tap push refuses if a stable formula
would change. The `stable` channel never reaches the tap job at all -- the
stable formulas belong to the ceremony's `release-artifacts.yml` run.

### A candidate keg reports the candidate version

A keg installed from `nyxgpt-api@3.0.0rc` reports the **candidate** version
everywhere the product names itself -- `nyxgpt --version`, `GET
/api/v1/info`, the web UI badge and `nyxgpt ops status` all print e.g.
`3.0.0rc13`, not `3.0.0`.

That is not cosmetic. An artifact install has no repo checkout above the
package, so the installed distribution's metadata is the **only** record of
which channel the keg belongs to, and `nyxgpt up` reads it to decide which
formulas to reconcile: a version carrying an `rc` marker routes to
`nyxgpt-api@<line>rc`/`nyxgpt-web@<line>rc`, and one without it routes to the
stable pair. Through rc13 the tarball vendored the release branch's
`pyproject.toml` verbatim, so a candidate keg declared the stable version it
was a candidate *for* -- and `nyxgpt up`, run from that keg, installed the
stable pair beside the candidate and started it, leaving a released web tier
in front of an unreleased API (#3850). The version is stamped into the
vendored `pyproject.toml` at tarball-build time now
(`nyxgpt.release_tarball._vendor_pyproject`), and both API formulas'
`brew test` blocks assert the keg reports its own version.

`web/package.json` is deliberately **not** stamped: npm versions are semver
(`3.0.0rc13` is not one), `package.json` and `package-lock.json` must agree
or the formula's `npm ci` refuses, and the web keg's channel already comes
from its formula name.

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

Each channel's formulas declare `conflicts_with` the other's, in **both**
directions, because both install the same `nyxgpt-api`/`nyxgpt-web`
wrappers. Switching channels is an explicit uninstall, never a silent swap:

```bash
# stable -> release candidate
brew services stop nyxgpt-api && brew uninstall nyxgpt-api
brew install nyxgpt-api@3.0.0rc && nyxgpt up

# a newer candidate of the same line (same formula, restamped)
brew update && brew upgrade nyxgpt-api@3.0.0rc && nyxgpt up

# ...and back once the release is out
brew services stop nyxgpt-api@3.0.0rc && brew uninstall nyxgpt-api@3.0.0rc
brew install nyxgpt-api && nyxgpt up
```

`brew services stop` before the uninstall is the right command there — it is
stopping one keg's service, not starting a stack. `nyxgpt up` afterwards is
what re-reconciles everything the new keg needs.

Those two are channel *swaps*, not removals: the machine keeps a nyxGPT
install throughout, so the `com.nyxgpt.*` agents and the containers are meant
to stay. Removing nyxGPT altogether is a different sequence — see
[Removing nyxGPT](#removing-nyxgpt), and run `nyxgpt ops uninstall` first.

**Both directions, deliberately.** `conflicts_with` is *directional*: it is
checked when the formula that declares it is being installed, and not
otherwise. Until #3853 only the candidate formula declared one, so
`brew install nyxgpt-api@3.0.0rc` onto an installed `nyxgpt-api` was refused
(`Cannot install ... because conflicting formulae are installed`) while the
opposite order was checked by nothing at all — brew built the stable keg to
completion and only then failed `brew link` on the symlink collision, which
is not a guard, because the keg stays installed. The machine ends with two
complete stacks for one component, each registering a `keep_alive true`
service on the same port; launchd relaunches the loser of the port race
forever. This is measured behaviour, not a reading of the docs:
`macos-brew-smoke.yml`'s `stable-over-candidate` job runs both orders on a
clean macOS runner and asserts each one.

If the counterpart named by a conflict is **not in the tap** — a tap whose
stable formulas have not been published yet, or a line whose candidates the
release ceremony has already retired — `brew` warns that the conflict refers
to an unknown formula and **carries on installing**. That is the absent-name
case only, and it is cosmetic; it is not the case above, where the
counterpart is installed. The declaration is deliberately left
unconditional: Homebrew resolves `conflicts_with` when it loads the formula
and offers no way to make one tolerant of a missing counterpart, so removing
it to silence the warning would trade a cosmetic message for the silent
channel clobber it exists to prevent. Neither name can go stale — each is
derived from the formula the same publishing script stamps for the other
channel (`scripts/build_homebrew_artifacts.py`).

Because a formula can only name counterparts that exist when it is stamped,
the stable formula names **its own release line's** candidate. A candidate
from a *different* line left on the machine is caught at install time
instead: `nyxgpt ops install` / `nyxgpt up` run a `superseded brew services`
step that stops any api/web service belonging to a different formula than the
one this install owns, before starting its own — so the port is free and
nothing is left crash-looping. The keg is not removed; `nyxgpt ops uninstall`
and `brew uninstall` are what remove software.

Candidate formulas are **acceptance-only**. They are not upgraded on a
schedule, carry no support expectation, and are removed from the tap by the
release ceremony the moment the line they are a candidate for ships. See
[docs/cloud.md](cloud.md#pypi-publishing-rc-and-stable) for cutting one
and for the equivalent pip/cloud flows.

---

## Installing the services

> Homebrew is the **artifact path** on macOS — the default, and what a
> release is accepted on. If you are iterating on a checkout and want the
> stack to run the working tree with no keg build at all, use
> [`nyxgpt up --dev`](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)
> instead; it bypasses the tap, the formulas and the Cellar entirely and
> runs api/web under its own LaunchAgents.

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

### What the kegs put on your PATH

| Command | Comes from | What it is |
|---|---|---|
| `nyxgpt` | `nyxgpt-api` | The CLI — `nyxgpt up`, `nyxgpt down`, `nyxgpt ops …`. Linked from the keg venv's console script, so it is the version you installed. |
| `nyxgpt-api` | `nyxgpt-api` | The service wrapper `brew services` runs; it execs uvicorn. Not something you normally type. |
| `nyxgpt-web` | `nyxgpt-web` | The service wrapper `brew services` runs; it execs `npm run start`. Not something you normally type. |

`nyxgpt` is what makes the install operable: every documented operation is a
`nyxgpt` command, so a keg that omitted it left an operator with nothing to
run (#3850). `nyxgpt-web` deliberately exposes no other command — the CLI it
would need is the one `nyxgpt-api` installs.

If you also have a development checkout with `pip install -e .`, both
`nyxgpt` commands exist and your shell picks whichever comes first on PATH.
An activated dev venv wins, which is the intended behavior; `nyxgpt ops
status` prints the install mode it is operating in, so you can always see
which one you are talking to.

`nyxgpt ops install` only re-runs `brew install`/`reinstall` when the
vendored source actually changed since the last install (checksum-compared);
otherwise it reports the existing install is already up to date and just
(re)starts the service.

### How the keg venv is built

The `nyxgpt-api` keg creates its venv with `python -m venv --without-pip`,
then bootstraps pip into it from a wheel:

1. Homebrew's `python@3.12` runs `pip download` to fetch a pip wheel.
2. The **venv's** interpreter runs pip *out of that wheel* — a wheel is a zip
   whose root is the `pip` package, so `python pip-X.whl/pip install
   pip-X.whl` works by zipimport, which is pip's own documented bootstrap.
3. Everything after that installs through the venv's own pip.

Two separate defects shaped this. `--without-pip` is there because a plain
`python -m venv` runs `ensurepip --upgrade --default-pip`, which bootstraps
pip from wheels vendored inside the `python@3.12` keg — the only part of the
install that depended on Homebrew-managed keg state rather than on the
tarball being installed. On a stock Homebrew Mac that exited 1 and took the
whole `brew install` with it.

The wheel bootstrap replaced an earlier spelling that had the keg's own pip
perform the install (`pip --python <venv-python> install --upgrade pip`).
That died on `python@3.12` 3.12.14 with

```
ImportError: No module named 'pip._internal.operations.install.wheel'
```

raised from `_prevent_import_hook`. pip 26.2 pre-imports its lazily-imported
modules just before it writes anything, so a distribution it is about to
install cannot shadow them; when that pre-import fails it records the name
and its audit hook turns the real import, moments later, into the error
above.

**That module was never missing.** This page used to say the fault was a pip
installation that could not import its own wheel installer; that reading was
wrong, and it is what sent three release candidates after pip. The
pre-import chain runs `pip._internal.operations.install.wheel` →
`pip._vendor.distlib.scripts` → `distlib.compat` → `import xmlrpc.client` →
`xml.parsers.expat`, and it was **pyexpat** that would not load. pip
discarded that `ImportError` and its audit hook re-raised the generic name.
See [When the install refuses to build](#when-the-install-refuses-to-build)
for the cause and what to do about it.

The wheel bootstrap stays regardless, because it is the right shape: the
keg's pip is no longer allowed to install anything; `download` is the only
subcommand the recipe uses. The venv is populated by a pip freshly unpacked
from a wheel, which is complete by construction.

### When the install refuses to build

Before it does anything, the `nyxgpt-api` install block runs a **preflight**
against the interpreter it is about to build on — the resolved
`python@3.12`, checked from inside `install`, which Homebrew calls only once
dependencies are installed, so it is the interpreter the build will really
use. It imports the compiled parts of the standard library the build and the
product depend on (`xml.parsers.expat`, `plistlib`, `ssl`, `zlib`, `lzma`,
`bz2`, `ctypes`, `sqlite3`). If any of them will not load, the install stops
there with the keg path, the loader's own error quoted verbatim, the
measured macOS and SDK versions, and what to do:

```
nyxgpt-api@3.0.0rc: refusing to build against /opt/homebrew/opt/python@3.12/bin/python3.12.

nyxgpt-preflight: this interpreter cannot import part of its own
standard library, so nyxGPT will not build a venv on it.

  interpreter: /opt/homebrew/opt/python@3.12/bin/python3.12
  keg:         /opt/homebrew/Cellar/python@3.12/3.12.14/Frameworks/…/3.12
  macOS:       26.2
  SDK:         26.5
  mac_ver():   ('', ('', '', ''), '')
  …
```

**Why an interpreter breaks this way.** Homebrew tags bottles by macOS
*major* version, so a `python@3.12` bottle built against a newer minor
release is served to a machine running an older one. Its
`pyexpat.cpython-312-darwin.so` then resolves the system
`/usr/lib/libexpat.1.dylib`, which does not export the newer expat's
`_XML_SetAllocTrackerActivationThreshold`, and every import that reaches XML
fails. Nothing nyxGPT does imports pyexpat directly, which is why the fault
surfaced twice as something else: `plistlib` needs it, so
`platform.mac_ver()` answered empty, and pip's vendored distlib needs it, so
pip reported a missing module that was present all along.

**What to do.** If the macOS version in the report is behind the SDK version,
that is this fault: update macOS, then `brew update && brew upgrade
python@3.12`. If the two already match, the keg is damaged rather than
skewed and `brew reinstall python@3.12` is the repair. On the machine that
reported this, `brew reinstall python@3.12` re-fetched the same bottle and
`brew reinstall --build-from-source python@3.12` could not build pyexpat
against the newer SDK either — updating macOS is what fixed it.

nyxGPT cannot repair a Homebrew bottle, and does not try. What it does is
refuse to build on one and name it, instead of failing several minutes later
inside pip with a message about something unrelated.

The check is Python nested in a Ruby heredoc, like the shim below, so
`build_homebrew_artifacts.py` compiles it while stamping (and refuses to
publish a formula that starts the brewed interpreter without one), the unit
suite runs the extracted source against a deliberately broken interpreter,
and [`macos-brew-smoke.yml`](../.github/workflows/macos-brew-smoke.yml)
injects an unloadable `pyexpat` into a real `macos-15` runner and proves both
halves: with the preflight stripped out the install walks straight past the
fault the way it used to, and with it in place `brew install` refuses before
the venv exists.

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
python running `pip download`, the venv python that runs pip out of the
downloaded wheel, and pip's build-isolation subprocesses. It asks `sw_vers` for the real release and only
falls back to a floor of `11.0` if that fails too; a Mac whose `mac_ver()`
already works is left completely alone, so the reported version never becomes
a lie that changes which wheels pip picks. The file lives in `buildpath` and
is gone once the keg is built — nothing ships it.

**What it deliberately does not repair.** An empty `mac_ver()` is a known
*symptom* of a broken interpreter: `platform._mac_ver_xml()` reads
SystemVersion.plist through `plistlib`, which imports pyexpat. Repairing the
value in that case makes pip start on a keg that cannot work, and carries the
install several minutes further before it dies of something else — which is
exactly what happened. So the shim first asks whether `plistlib` imports at
all; when it does not, it says so, names the cause, and leaves `mac_ver()`
alone. The [preflight](#when-the-install-refuses-to-build) has already
refused that build, so what is left for the shim is the case it was written
for: an interpreter that is fine and an OS lookup that is not.

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

The same treatment covers #3788: the job pulls `python@3.12` up to current
(after a `brew update`, without which `brew upgrade` can only reach versions
the runner image already knew about), then injects the owner's machine state
— a keg pip that cannot import `pip._internal.operations.install.wheel` —
and requires the retired `pip --python … install` bootstrap to die on it
before accepting that the current one survives it.

The condition is injected by **taking the module away** — `mv`-ing
`pip/_internal/operations/install/wheel.py` out of the keg **at its
realpath**, running both controls, and restoring it from a shell `trap` on
every exit path (the steps that follow install the formulas with that same
pip, so it must go back).

The realpath is the load-bearing word. Homebrew's prefix tree
(`/opt/homebrew/lib/python3.12/...`) links into the Cellar, and pip's
`--python` child re-execs through `get_runnable_pip()`, which is
`Path(pip.__file__).resolve().parent` — so moving the *prefix* entry leaves
the copy that child imports untouched. That is not hypothetical: it is what
the third red round of this step did, firing the self-check through a
dangling prefix symlink while the negative control installed successfully
underneath it. Removed at its realpath, one hole serves every route — the keg
pip, its `--python` re-exec child, and `brew install`.

That is worth stating, because two earlier spellings *emulated* the
condition instead, with a meta-path finder in a `sitecustomize` on
`PYTHONPATH`, and both failed on the vehicle rather than on the recipe. The
first scoped itself to the keg with `abspath`, and `pip --python` re-execs
via `get_runnable_pip()`, which resolves symlinks — so it matched in the
parent and never in the child that actually installs. The second compared
realpaths, but python imports exactly one `sitecustomize` and `PYTHONPATH`
precedes `site-packages`, so the fault file shadowed the one the keg ships
and moved which pip was under test.

The general lesson is worth more than either fix: **an emulated condition
runs in an interpreter environment `brew install` does not have, so the
emulation becomes the thing being tested.** Removing the file is the machine
state itself — no `PYTHONPATH`, no import hooks — and every process that
resolves to it sees it, which is why it has to be removed where they all
resolve *to*.

All three mistakes had the same symptom: the fault does not reach the child
that installs, the negative control passes, and the log reads exactly like
"the bug is gone". So the step asserts the condition exists before
concluding anything from it — and asserts it by **both** routes pip can be
imported here, the prefix one this process uses and the resolved one the
`--python` child re-execs into. Checking only the first is precisely how a
moved symlink read as a created condition; that self-check, not a passing
control, is the arbiter.

---

## Managing the API service (nyxgpt-api)

### Start the API

`nyxgpt up` starts this service along with the rest of the stack, and is what
you want on a machine that is not already running. The command below starts
this one service on its own — useful when the rest of the stack is already up
and only the API is down:

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

As with the API, `nyxgpt up` starts this service along with everything else it
needs. The command below starts this one service on its own, for when the rest
of the stack is already up:

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
nyxgpt up
```

That is the whole stack, not just these two services — see the note at the top
of this document for what `brew services start` leaves out. `nyxgpt down` stops
it again.

To restart the services that are already installed, without reconciling
anything:

```bash
nyxgpt ops restart
```

Starting the two kegs individually is still available, and is the right tool
only when the rest of the stack is already running:

```bash
brew services start nyxgpt-api
brew services start nyxgpt-web
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

## Removing nyxGPT

**Run the wrapped teardown first, before any `brew uninstall`:**

```bash
nyxgpt ops uninstall
```

Then, and only then, remove the artifacts:

```bash
brew uninstall $(brew list --formula | grep '^nyxgpt')
brew untap dkblinux98/nyxgpt
```

`nyxgpt ops uninstall` stops **and deregisters** everything the install put
on the machine:

| Population | Why `brew uninstall` cannot do it |
|---|---|
| The `nyxgpt-api`/`nyxgpt-web` brew services | Homebrew has no uninstall hook and does not stop a service before deleting its keg |
| The `com.nyxgpt.*` LaunchAgents (Ollama logs/env, Cassandra logs) | nyxGPT installed these itself; Homebrew never knew they existed |
| The `nyxgpt-cassandra` container and the observability Compose tier | not Homebrew's at all |

Your data is preserved: `~/.nyxGPT` (config.ini, volumes, logs) is left
alone. Delete that directory by hand if you want it gone too.

**Why the order matters.** `brew uninstall` deletes a keg's files without
stopping its service first, and a running process survives deletion of its
executable — so the api and web services keep serving :8000 and :3000 from
software that is no longer installed. `brew untap` then removes the formula
definitions, so `brew services stop nyxgpt-api@3.0.0rc` has nothing left to
act on: the services are orphaned from the tool that created them. The
formulas' `caveats` say the same thing at the point of install.

`nyxgpt ops uninstall` is idempotent — it is meant to be run against
half-removed machines, so an already-stopped service or an already-deleted
plist is reported and skipped, not treated as a failure. If you have already
uninstalled the kegs, run it now: it finds the leftover launchd jobs by their
plists rather than by asking brew to resolve a formula that is gone.

`nyxgpt ops install` reports the same condition from the other side: a
launchd job left loaded against deleted files is named at install time,
before anything tries to bind the ports it is holding.

`nyxgpt ops uninstall --volumes --yes-really` additionally deletes the
Docker volumes (Cassandra/Postgres/Grafana data). Without both flags it
refuses.

To stop the stack *without* uninstalling it, use `nyxgpt down` — that leaves
every service installed and registered to come back at the next login, which
is exactly why it is not a substitute for the teardown above.

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **nyxgpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

`nyxgpt up` handles the ordering, and waits for health rather than for a
guessed number of seconds — which is the other reason it is the recommended
way to start:

```bash
nyxgpt up
```

Starting them by hand means starting the API first and waiting for it:

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

Others are read once at service start and stay at their old value until that
service restarts -- including `auth.enabled` and `auth.api_key`, which the API
honours immediately but the web UI does not (its wrapper reads `[auth]` once
and exports the key into a Node process, so every proxied call 401s with the
old key until `web` restarts). Each such key is annotated in
`example.config.ini` with an `# Activation: restart required (<service>)`
line, and saving one from the Configuration Wizard, the Admin Dashboard or
`nyxgpt secrets setup` raises a persistent "saved, but not yet in effect"
notice that offers the restart (#3806).

Apply those changes with:

```bash
# Restart API service
nyxgpt ops restart api

# Restart Web UI service
nyxgpt ops restart web

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
launch mode (native, `nyxgpt ops install --terraform`, or Compose)
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

### `command not found: nyxgpt`

**Symptom**: `brew install` succeeded, the services start, and `nyxgpt up`
answers `zsh: command not found: nyxgpt`.

**Cause**: a keg built before the CLI was linked into `bin/` (#3850). The
console script was installed inside the keg's venv, where nothing on your
PATH reaches it.

**Solutions**:

1. Upgrade to a keg that exposes it, then confirm:
   ```bash
   brew update && brew upgrade nyxgpt-api
   command -v nyxgpt && nyxgpt --version
   ```
   For a release candidate, name its formula instead:
   `brew upgrade nyxgpt-api@3.0.0rc`.

2. If `command -v nyxgpt` still finds nothing, the keg may simply be
   unlinked (another formula owns the name, or a link step was skipped):
   ```bash
   brew link --overwrite nyxgpt-api
   ```

3. Until either lands, the CLI is still runnable by its full path -- it is
   reachable, just not on PATH:
   ```bash
   "$(brew --prefix nyxgpt-api)/libexec/venv/bin/nyxgpt" ops status
   ```

### A candidate install pulled in the stable formulas too

**Symptom**: after `brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc` and
`nyxgpt up`, `brew list --versions` shows **four** kegs -- the candidate pair
*and* `nyxgpt-api`/`nyxgpt-web` at the last stable release -- and
`brew services list` shows the stable pair running. The UI looks like an
older nyxGPT than the one you installed, typically reported as a feature
that has gone missing.

**Cause**: a candidate keg built before the tarball carried the candidate
version (#3850). It reported the stable version, so `nyxgpt up` read that as
"this machine is on the stable channel" and reconciled the stable formulas.
Confirm with `nyxgpt --version`: on an affected keg it prints the release
line (`3.0.0`) rather than the candidate (`3.0.0rc13`).

**Solution**: remove the stable pair, then upgrade the candidate to a keg
that reports itself correctly.

```bash
nyxgpt down
brew uninstall nyxgpt-api nyxgpt-web
brew update && brew upgrade nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc
# Confirm the keg now names the candidate before starting anything:
nyxgpt --version
nyxgpt up
```

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
- Start the stack with `nyxgpt up`; `brew services start` covers only the one
  service it names (see the note at the top of this document)
- Use `nyxgpt ops` commands for service management
