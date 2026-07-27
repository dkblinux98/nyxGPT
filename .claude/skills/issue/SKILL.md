# Create GitHub Issue with Intelligence and Project Hygiene

This skill creates a well-formed GitHub issue with proper title, body, acceptance criteria, and ensures all required project fields are set and verified.

## Phase 1: Understand What Issue to Create

1. **Analyze context** from conversation history:
   - What problem needs to be solved?
   - What feature is being requested?
   - What bug was discovered?
   - Are there related issues or PRs to reference?

2. **If context is unclear**, ask the user:
   - "What issue should I create? Please describe the problem/feature/bug."
   - Gather enough information to write a clear, actionable issue

## Phase 2: Craft a Proper Issue

### Title
- Clear, concise, actionable (under 80 characters)
- Format: `[type]: [action/problem] - [component if relevant]`
- Examples:
  - `feat: Add metrics dashboard to web UI settings`
  - `bug: RAG playground crashes on epub upload`
  - `fix: Developer agent ignoring review requests`

### Body Structure
```markdown
## Problem / Motivation
[Why is this issue needed? What problem does it solve?]

## Acceptance Criteria
- [ ] [Specific, testable criterion 1]
- [ ] [Specific, testable criterion 2]
- [ ] [Specific, testable criterion 3]

## Technical Details (if applicable)
- Files affected: [list]
- Dependencies: [if any]
- Architecture considerations: [if any]

## Related Issues/PRs
- Related to #[number]
- Blocks #[number]
- Blocked by #[number]
```

### Determine Module from Content
- Scan title/body for keywords:
  - `web|ui|frontend|react|vue` → web-ui
  - `api|endpoint|rest|graphql` → api
  - `rag|embedding|vector|semantic` → rag
  - `cli|command.line|terminal` → cli
  - `tui|terminal.ui` → tui
  - `test|testing|pytest` → testing
  - `doc|documentation|readme` → documentation
  - `security|auth|permission` → security
  - `observ|monitor|metric|log|self.heal|sre|dashboard|deploy` → sre
  - Fallback: api

**Valid Module options are fixed** (do NOT invent new ones — creating field
options is forbidden): `api`, `cli`, `documentation`, `sre`, `rag`,
`security`, `testing`, `tui`, `web-ui`. Note there is **no** `observability`
option — observability/monitoring/self-heal work maps to `sre`. If a mapping
produces a value not in this list, fall back to `api` and note it.

## Phase 3: Create Issue and Set Project Fields

1. **Create the issue**
   ```bash
   gh issue create --title "[title]" --body "[body]"
   # Capture issue number
   ```

2. **Load project configuration**
   ```bash
   source scripts/agents/lib/gh_project.sh
   load_config
   require_gh_auth
   ```

3. **Add to project and set all required fields**
   ```bash
   # Add to project
   ITEM_ID=$(ensure_issue_in_project "$ISSUE")

   # Set Status to Backlog (REQUIRED)
   set_issue_status "$ISSUE" "Backlog"

   # Set Priority to P1 - High (REQUIRED default)
   set_project_field_value "$ITEM_ID" "Priority" "P1 - High"

   # Set Effort to XS (REQUIRED default)
   set_project_field_value "$ITEM_ID" "Effort" "XS"

   # Set Label to Feature (or "Acceptance Failure" if bug/fix)
   gh issue edit "$ISSUE" --add-label "Feature"

   # Set Module (from auto-detection above)
   set_project_field_value "$ITEM_ID" "Module" "$MODULE"

   # Set Sprint to the currently-active iteration (REQUIRED).
   # The lib resolves the active iteration when the value is "ACTIVE".
   set_project_field_value "$ITEM_ID" "Sprint" "ACTIVE"

   # Set Milestone to the soonest-due OPEN milestone (REQUIRED).
   # Do NOT create a milestone if none is open — stop and ask the user.
   MILESTONE_TITLE=$(gh api "repos/${REPO_OWNER}/${REPO_NAME}/milestones?state=open" \
     --jq 'sort_by(.due_on // "9999") | .[0].title // empty')
   if [[ -n "$MILESTONE_TITLE" ]]; then
     gh issue edit "$ISSUE" --milestone "$MILESTONE_TITLE"
   else
     echo "[warn] No open milestone found — ask the user which milestone to use (do NOT create one)."
   fi
   ```

   **Note on repo automation:** project-hygiene workflows on this repo may
   also stamp Sprint/Milestone on issue creation. Setting them explicitly
   here is still required — never rely on the workflow having run, and always
   confirm the final values in Phase 4.

## Phase 4: VERIFY Everything

**CRITICAL: Never assume success from non-error response!**

```bash
# Re-query to confirm all fields
VERIFICATION=$(gh issue view "$ISSUE" --json number,title,body,projectItems,labels,milestone)

# Parse and report actual values:
echo "Created issue #$ISSUE"
echo "Title: [actual]"
echo "Status: [actual]"
echo "Priority: [actual]"
echo "Effort: [actual]"
echo "Module: [actual]"
echo "Label: [actual]"
echo "Sprint: [actual]"
echo "Milestone: [actual]"
```

If ANY field is not set correctly, report the discrepancy and fix it.

## Usage

User runs: `/issue`

Claude responds:
1. Analyzes conversation for context OR asks "What issue should I create?"
2. Drafts title and body (shows user for approval)
3. Creates issue with all project fields
4. Verifies and reports actual field values
5. Provides issue URL
