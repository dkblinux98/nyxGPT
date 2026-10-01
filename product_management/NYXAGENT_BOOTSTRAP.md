# nyxAgent: bootstrapping the repository from Paperclip

**Drafted:** 2026-09-30, by the executive assistant, at the owner's direction:
*"instantiate nyxAgent in GitHub with a fork of Paperclip… I don't want
Paperclip to be an add-on to nyxAgent but rather a starting point for
nyxAgent."*

**Status:** procedure for the owner to run, plus the compliance findings behind
it. Nothing has been created; see §7 for why this session did not create it.

**Terminology.** Throughout, *the copy* means one full, independent copy of
Paperclip's codebase, developed from here on as nyxAgent's own product and never
merged back upstream. That is the general open-source sense of "forking a
project". It is **not** GitHub's **Fork** feature, which is a different thing
with its own properties — §1 is about why that feature is the wrong tool for
exactly this intent. (An earlier draft called this a "hard fork", which was
unhelpfully ambiguous next to §1.)

**Companion:** `NYXAGENT_CLAUDE_PRACTICES.md` §3b (the Paperclip assessment),
`NYXAGENT_SEPARATION_PLAN.md` (what nyxAgent is), `PHASE_7_PLAN.md`.

**Everything asserted below was checked on 2026-09-30** against a shallow clone
of `paperclipai/paperclip` at `0e58308` and against GitHub's own documentation.
Where a fact is quoted, it is quoted verbatim from the source named.

---

## 1. The headline: do not use GitHub's Fork button

The owner's own requirement — Paperclip as a *starting point*, not an *add-on* —
is the technical argument against forking, and GitHub's documented behavior makes
it decisive.

**A fork of a public repository can never be made private.** GitHub's
visibility documentation states flatly: *"Public forks are not made private."*
Changing visibility detaches forks rather than converting them. So a forked
nyxAgent is a permanently public repository, decided at creation.

**Anything committed to a public fork is permanently readable from Paperclip's
own repository network.** From GitHub's fork-permissions documentation:

> "Commits pushed to any repository in a network can be accessible from other
> repositories in that network, including the upstream repository."

> "Git data from any repository in a network may be accessed from any repository
> in the same network, including the upstream repository, even after a fork is
> deleted."

For a repository that will carry nyxAgent's customer-project profiles, board and
lane configuration, release-branch conventions and token *references*, that is
the wrong default — and it is not undoable. Deleting the fork does not retract it.

**A fork also encodes the relationship the owner explicitly does not want.**
GitHub renders a fork with a "forked from paperclipai/paperclip" banner, pull
requests opened from it default to the *upstream* base branch, and a fork cannot
be used as a template repository. Structurally and visually, a fork *is*
nyxAgent-as-an-add-on-to-Paperclip.

**The right mechanism is GitHub's own documented alternative: duplicating a
repository.** A bare clone mirror-pushed into a fresh, independent repository.
It is a genuine starting point: no fork network, no banner, no upstream default,
private if wanted, full history preserved, and an `upstream` remote only if and
when the owner chooses one. §5 gives the commands.

The word "fork" in the owner's instruction describes the *intent* — start from
their code and go our own way. That intent is served by **not** using the feature
called Fork.

---

## 2. What the MIT licence actually requires

Read from the repository's own `LICENSE` at `0e58308`:

- It is the **unmodified MIT licence**, with the copyright line
  **`Copyright (c) 2025 Paperclip AI`**.
- **Discrepancy worth knowing:** the README states *"MIT © 2026 Paperclip Labs,
  Inc."* The `LICENSE` file says `2025 Paperclip AI`. The `LICENSE` file is the
  operative text and is what must be reproduced — verbatim, including its year
  and holder name. Do not "correct" it to match the README.
- **There is no CLA, no DCO, no sign-off requirement, no NOTICE file and no
  trademark policy.** `CONTRIBUTING.md` (11.7 kB) contains no occurrence of
  *licence*, *copyright*, *trademark*, *CLA*, *DCO* or *sign-off*. There are no
  obligations beyond MIT's own.

MIT's single condition is this sentence:

> "The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software."

That is the whole compliance burden. Everything the owner asked about is
expressly permitted by the grant itself — *"to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies"* — with no obligation to publish
changes, no copyleft, and no requirement to keep the project open source.

**Do:**
- Keep `LICENSE` in place, byte-for-byte, with Paperclip AI's copyright line intact.
- Add the owner's copyright as an **additional** line or in a **separate** file.
- Keep every existing per-file copyright header that exists.
- Add an `ATTRIBUTION.md` recording the derivation (§4). Not required by MIT —
  worth it anyway, because it makes the intent auditable, which is this project's
  standing habit.

**Do not:**
- Delete `LICENSE`, overwrite it, or replace the holder name.
- Strip per-file headers during the rename pass. A bulk find-and-replace over
  5,070 files is exactly how that happens by accident.

---

## 3. The four nested licences the copy inherits

The root MIT is not the whole picture. Four nested licence/notice files travel
with the tree, and one of them is **not MIT**:

| Path | Licence | What it obliges |
|---|---|---|
| `ui/public/brands/adapters/LICENSE` | MIT © 2023 **LobeHub** | Keep this LICENSE too. These SVGs depict **third-party trademarks** — Claude, Codex, Cursor, Gemini, Grok, Kimi, OpenCode, Hermes, Pi. LobeHub's MIT covers the *files*; it conveys nothing about those marks. Using them to label which adapter is connected is ordinary nominative use and defensible; using any of them as nyxAgent's own branding is not. |
| `packages/shared/src/cliplab/LICENSE` | MIT, **two** holders: Jérémy Perret ("bloub", the upstream) and Tonio ("Cliplab", modifications) | Keep both copyright lines. Note this is already a fork-of-a-fork — the attribution chain is three deep before nyxAgent joins it, and it is a working precedent for how to do it. |
| `packages/adapters/hermes/LICENSE` | MIT © 2026 **Nous Research** | Keep. |
| `ui/public/fonts/NOTICE.md` and `packages/paperclip-runner/devtools/issue-thread/src/fonts/NOTICE.md` | **Not MIT.** Inter and Noto Sans Symbols 2 under **SIL Open Font License 1.1**; DejaVu Sans Mono/Sans under the **Bitstream Vera Fonts licence** | The genuinely different terms — see below. |

**The font licences are the ones to actually read.** Both notices must travel
with the fonts, and both carry a reserved-name clause plus a sale restriction:

- **SIL OFL 1.1** (Inter, Noto Sans Symbols 2): the licence text must accompany
  the fonts, and **a modified font must be renamed** away from the reserved font
  name. Paperclip already ships *subsets* of Inter and Noto — a modification —
  so this clause is live, not hypothetical, the moment the owner re-subsets.
- **Bitstream Vera** (DejaVu): the notice reproduced in Paperclip's own
  `NOTICE.md` requires that *"the above copyright and trademark notices and this
  permission notice shall be included in all copies"*; that modified fonts be
  **renamed to names not containing "Bitstream" or "Vera"**; and that *"no copy
  of one or more of the Font Software typefaces may be sold by itself"* —
  unproblematic inside a larger package, which is the case here.

None of this blocks anything. It means the font directories and their `NOTICE.md`
files are **not** candidates for the de-branding sweep, and a "remove all
Paperclip files" pass must not touch them.

---

## 4. Trademark is the part MIT does not grant

This is the most commonly missed half, and it happens to align with what the
owner wants anyway.

MIT is a **copyright** licence. It conveys **no trademark rights**. "Paperclip",
the `paperclipai` name and any Paperclip wordmark or logo are not licensed by it.
So renaming is not merely branding preference — it is the compliance step, and it
needs to be thorough:

- **`cli/package.json` is `"name": "paperclipai"` with
  `"publishConfig": {"access": "public"}`.** Publishing from an un-renamed copy
  would attempt to push to Paperclip's own npm package name. Rename before any
  `publish` runs anywhere.
- **5,070 of 8,072 files contain the string "paperclip"** (case-insensitive).
  That includes workspace package names (`packages/paperclip-runner`,
  `packages/paperclip-eval-kernel`), four `skills/paperclip*` directories, Docker
  image names, the docs tree and the CLI. This is a tracked project, not a
  `sed -i` — and see §2's warning about bulk replacement destroying copyright
  headers.
- Do not describe nyxAgent in a way that implies Paperclip endorsement or
  affiliation. "Derived from Paperclip" is accurate and fine; "Paperclip for
  software delivery" is not.

---

## 5. The procedure

Run in this order. The ordering matters — steps 1 and 2 exist to stop money and
mistakes.

### Step 0 — decide two things first

- **Visibility.** **Recommended: private to begin with.** It lets the rename and
  de-branding pass (§4) happen before anything is public, and it removes the
  fork-network question in §1 entirely. MIT's notice obligation attaches to
  *distribution*, so private work carries no additional duty — but keep `LICENSE`
  and `ATTRIBUTION.md` from the first commit regardless, because backfilling
  attribution later is how it gets forgotten. Going public later is one setting;
  the reverse is not true of a fork.
- **History.** **Recommended: keep the full upstream history.** Git history is
  the highest-quality attribution that exists — it names every author of every
  line — and it is what makes selective upstream merges possible later. The
  alternative (squash to one `vendor: import Paperclip at 0e58308` commit) is
  cleaner-looking and strictly worse on both counts.

### Step 1 — create the empty repository with Actions OFF

Create `nyxAgent` empty: **no README, no .gitignore, no licence template** (a
licence template would collide with the upstream `LICENSE`).

**Then, before pushing anything: Settings → Actions → General → "Disable
actions".**

This is not optional housekeeping. The tree carries **20 workflows** —
`release.yml`, `docker.yml`, `e2e.yml`, `runner-live-evals.yml`,
`agent-runtime-images.yml` among them. They fire on push. On first push they
would start consuming runner minutes immediately, and the release and image
workflows would attempt to publish under **Paperclip's** names. Re-enable
selectively once the rename pass is done. (First principle 1: cost.)

### Step 2 — duplicate, do not fork

GitHub's documented duplicate procedure:

```bash
# 1. Bare mirror clone of upstream
git clone --bare https://github.com/paperclipai/paperclip.git paperclip-bare
cd paperclip-bare

# 2. Mirror-push into the new, independent repository
git push --mirror https://github.com/<OWNER>/nyxAgent.git

# 3. Discard the bare clone
cd .. && rm -rf paperclip-bare
```

The result has no fork relationship, no upstream banner, and full history.

### Step 3 — working clone, and decide the upstream posture

```bash
git clone https://github.com/<OWNER>/nyxAgent.git
cd nyxAgent
git remote add upstream https://github.com/paperclipai/paperclip.git
git remote set-url --push upstream DISABLED   # never push to Paperclip by accident
git fetch upstream
```

**Upstream is at PR #14727 as of 2026-09-30, with HEAD committed that same day.**
That velocity cuts both ways: the project is alive and maintained, and a hard
fork starts diverging immediately. Two honest postures:

- **Track selectively.** Keep `upstream` fetched, merge specific fixes by
  cherry-pick or subtree merge. Upstream security fixes stay cheap; the cost is a
  standing merge obligation forever.
- **Sever at import.** Record `0e58308` as the import point in `ATTRIBUTION.md`
  and never merge again. Simpler, and upstream fixes become nyxAgent's problem.

Either is defensible. **If tracking: forward merges only.** No rebase, no
force-push, no history rewriting — `AGENTIC_SDLC_DESIGN.md` §6 and D-011 are
hard rules and they apply to this repository too.

### Step 4 — attribution, as the first nyxAgent commit

Add `ATTRIBUTION.md` (§6) and append the owner's copyright line to `LICENSE`
**below** Paperclip AI's, leaving theirs untouched:

```
MIT License

Copyright (c) 2025 Paperclip AI
Copyright (c) 2026 <owner / nyxAgent>
```

Commit these *before* the rename pass, so the record of what was inherited
predates the changes to it.

### Step 5 — rename and de-brand, as tracked work

This is §4's project, and it is where nyxAgent stops being a copy. Do it as
real issues through the SDLC, not as one sweep. Non-negotiables during it:
never touch `LICENSE`, the four nested licence files, or either fonts
`NOTICE.md`; never strip a per-file copyright header.

---

## 6. `ATTRIBUTION.md`, ready to use

Fill in the two bracketed values.

```markdown
# Attribution

nyxAgent is derived from **Paperclip** (<https://github.com/paperclipai/paperclip>),
used as the starting point for this project rather than as a dependency.

- **Imported at:** commit `0e58308`, 2026-09-30
- **Upstream licence:** MIT — see `LICENSE`, which retains
  `Copyright (c) 2025 Paperclip AI` unmodified, as that licence requires
- **Relationship:** nyxAgent is an independent project. It is not a fork in
  GitHub's sense, is not affiliated with or endorsed by Paperclip AI, and the
  Paperclip name and marks are not used to identify nyxAgent. MIT grants
  copyright permissions only and conveys no trademark rights.
- **Upstream changes since import:** [tracked selectively / not tracked]

## Third-party components inherited with the import

These carry their own licences, which are retained at the paths shown. None may
be removed or altered by renaming or de-branding work.

| Component | Path | Licence |
|---|---|---|
| Adapter brand icons | `ui/public/brands/adapters/` | MIT © 2023 LobeHub. The icons depict third-party trademarks (Claude, Codex, Cursor, Gemini, Grok, Kimi, OpenCode, Hermes, Pi), used nominatively to identify adapters. |
| Cliplab | `packages/shared/src/cliplab/` | MIT © 2026 Jérémy Perret (bloub) and © 2026 Tonio (Cliplab) |
| Hermes adapter | `packages/adapters/hermes/` | MIT © 2026 Nous Research |
| Inter, Noto Sans Symbols 2 | `ui/public/fonts/`, `packages/paperclip-runner/devtools/issue-thread/src/fonts/` | SIL Open Font License 1.1 — licence text must accompany the fonts; modified or re-subset fonts must be renamed |
| DejaVu Sans / Sans Mono | `packages/paperclip-runner/devtools/issue-thread/src/fonts/` | Bitstream Vera Fonts License — notices must be retained; modified fonts must be renamed away from "Bitstream"/"Vera"; no typeface may be sold by itself |
```

---

## 7. The concern, stated once, and what this session did not do

**The concern.** Taking the whole codebase means adopting **~2.76 million lines of TypeScript
excluding tests** (≈4.5M including), across **5,861 `.ts`/`.tsx` files** and
**8,072 files** total, written by someone else, into a project whose Definition
of Done requires executed verification of every behavior claim and whose review
agent reviews *all* changed files in a PR. There is no legal obstacle — MIT is as
permissive as licences get, and §§2–4 are a morning's work. The cost is
ownership: every one of those lines becomes nyxAgent's to understand, verify and
maintain, and upstream's pace means that bill starts immediately. That is the
owner's call to make, and this document is written to make it cleanly either way.

**Worth noting as the smaller, cheaper alternative if the scale gives pause:**
Paperclip's `skills/` directory and its adapter-plugin interface
(`adapter-plugin.md`) are the two pieces `NYXAGENT_CLAUDE_PRACTICES.md` §3b
identified as the parts nyxAgent genuinely lacks. Taking those two as design
input — the §3b(b) recommendation — costs no adoption at all. The fork buys the
other ten subsystems too, which is the point, but the ratio is worth a look.

**What this session did not do, and why.** It did not create the repository. The
`SessionStart` hook records that GitHub writes from a Claude Code web session are
authored as the **connected user, not as a nyxGPT agent**, because the egress
proxy overrides `Authorization` on `api.github.com`; project policy is therefore
to keep remote sessions read-only on GitHub and route writes through a local
session or the Actions triggers. Creating a public repository that names a
product, under the owner's identity, is also not a reversible administrative
tidy-up. Step 1's "Actions off before first push" additionally has to be done in
the GitHub UI between creation and push, which no script can sequence for the
owner.

The clone inspected for this document was read-only and lives in this session's
scratchpad; nothing was pushed anywhere.

---

## 8. Open for the owner

1. **Owner and name** — `dkblinux98/nyxAgent` assumed throughout. A separate org
   instead? (`NYXAGENT_SEPARATION_PLAN.md` §8 left repository topology open.)
2. **Visibility** — private first is recommended (§5 Step 0). Or public from
   birth, which §8 of the separation plan also raised as an open question?
3. **Upstream posture** — track selectively, or sever at `0e58308`?
4. **Nothing else.** `NYXAGENT_CLAUDE_PRACTICES.md` §3b has been updated to
   record the owner's direction; no further bookkeeping is owed for it.
