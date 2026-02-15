# Known Limitations - Agent Intelligence

## Critical Gap: No Progress Detection in Retry Loops

### The Problem

Agents count retries mechanically without understanding whether actual work is being done.

**Current Behavior:**
```
Developer attempt 1 → Review fails
Developer attempt 2 → Review fails
Developer attempt 3 → Review fails
→ Escalate after 3 attempts
```

**Missing Intelligence:**
- Did the developer push new commits between attempts?
- Are the review issues the SAME each time?
- Is work actually happening, or just noise?

### Example: Issue #3117

**What happened:**
1. Review requested changes: "Add tests"
2. Developer retried 5+ times WITHOUT adding tests
3. Each retry counted toward escalation limit
4. No detection that the SAME issue persisted
5. No detection that no new commits were made

**What should have happened:**
1. Review requested changes: "Add tests"
2. Developer retries with NO new commits → Don't count as attempt
3. Developer pushes commits but SAME issues found → Count as failed attempt
4. Developer pushes commits with DIFFERENT issues → Reset counter (progress!)

### Root Cause

**No persistent state across workflow runs:**
- Each retry starts fresh
- No memory of what was tried before
- No comparison of current vs previous state
- No understanding of "same issue" vs "new issue"

**Mechanical counting instead of intelligent analysis:**
```yaml
# Current (dumb)
retry_count = count(comments with "RETRY_IMPLEMENTATION")
if retry_count >= 3: escalate()

# Needed (intelligent)
if no_new_commits():
  ignore_retry()  # Just noise
elif same_issues_as_last_time():
  increment_real_retry_count()  # Actually failed to fix
elif different_issues():
  reset_counter()  # Made progress, new problems
```

### Why This Matters

**Symptoms:**
- Agents pass work back and forth without actually doing anything
- Humans can see they're not working, agents cannot
- Retry limits are hit even when no real attempts were made
- Progress is not distinguished from busy work

**Impact:**
- False escalations (agents gave up too early)
- Wasted cycles (retrying without fixing)
- No learning from previous attempts
- Kludgy workarounds in workflow logic

### What Would Real Intelligence Require?

**1. Persistent State Storage**
```yaml
Store between runs:
- Commit SHA at each review
- Hash of review issues found
- List of what was tried
- History of changes made
```

**2. Content-Aware Comparison**
```yaml
Compare:
- Current commit vs last review commit
- Current issues vs previous issues
- File diffs between attempts
```

**3. Progress Detection**
```yaml
Determine:
- Are new commits present? (work happened)
- Are issues identical? (no progress)
- Are issues different? (progress, new problems)
- Is code converging toward solution? (learning)
```

**4. LLM-Based Analysis**
```yaml
Use LLM to:
- Compare review issues semantically (not just text match)
- Determine if "add tests" and "missing test coverage" are the same
- Assess whether commits actually address the review feedback
- Decide if this is real work or just going through motions
```

### Architectural Solutions (Not Implemented)

**Option 1: GitHub Artifacts for State**
```yaml
- name: Store review state
  uses: actions/upload-artifact@v4
  with:
    name: review-state-${{ github.run_id }}
    path: |
      current-commit-sha.txt
      review-issues-hash.txt
      attempt-history.json
```

**Option 2: Database/KV Store**
```yaml
- name: Check progress
  run: |
    LAST_COMMIT=$(redis-cli GET "issue:$ISSUE:last_reviewed_commit")
    CURRENT_COMMIT=$(git rev-parse HEAD)

    if [ "$LAST_COMMIT" == "$CURRENT_COMMIT" ]; then
      echo "No new commits - skipping retry count"
      exit 0
    fi
```

**Option 3: LLM Progress Analyzer**
```yaml
- name: Analyze if real work happened
  uses: anthropics/claude-code-action@v1
  with:
    prompt: |
      Compare these two review results:

      Previous review: $PREVIOUS_ISSUES
      Current review: $CURRENT_ISSUES

      Commits between reviews: $GIT_DIFF

      Question: Did the developer actually address the previous review issues,
      or is this the same problem happening again?

      Answer: PROGRESS | SAME_ISSUE | NEW_ISSUE
```

### Current Workarounds (Kludgy)

**What we're doing now:**
1. Intelligent error classification (recognizes error types)
2. Error-type specific retry counting (tracks per-error, not global)
3. Manual intervention resets counters (human can override)
4. Remove conditions that prevent review requests

**What's still missing:**
- Understanding if commits were made
- Detecting if issues are identical
- Knowing if agents are actually working

### Human vs Agent Intelligence

**Humans can immediately see:**
- "The developer didn't add any commits between retries"
- "The reviewer is finding the exact same issue every time"
- "They're just passing it back and forth without doing work"

**Agents currently cannot detect:**
- Whether work happened
- Whether progress was made
- Whether it's worth retrying

### Recommendation

This is a **fundamental architectural limitation**, not a bug that can be fixed with workflow tweaks.

**Short term:** Accept the limitation, use kludgy workarounds
**Long term:** Redesign agent architecture to include:
- Persistent state storage
- Progress detection logic
- Content-aware comparison
- LLM-based "did work happen?" analysis

### Related Issues

- Issue #3117: Developer retried 5+ times without fixing review issues
- Commit `78503a4`: Attempted fix that introduced new bugs
- Commit `46dce25`: Removed kludgy condition, didn't solve root cause

---

*Documented: 2026-02-14*
*Author: Claude Sonnet 4.5 (via user request)*
