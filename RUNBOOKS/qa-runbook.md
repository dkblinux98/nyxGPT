# QA Runbook (qa-agent)

## 0) Preconditions
- PR has passed initial code review
- CI checks are green
- PR is assigned to qa-agent for QA verification

## 1) Setup
```bash
# Checkout PR branch
gh pr checkout <PR_NUMBER>

# Install dependencies
pip install -e .
cd web && npm install && cd ..

# Verify services are running
nyxgpt ops doctor
```

## 2) Automated Test Suite

### Unit Tests
```bash
python -m pytest tests/unit -v --tb=short
```
- Expected: All tests pass
- If failures: Document which tests failed and why

### Integration Tests
```bash
python -m pytest tests/integration -v -m integration --tb=short
```
- Expected: All tests pass
- Requires: Ollama and Cassandra running
- If failures: Check service health, document failures

### E2E Tests (WebUI)
```bash
cd web
npm run test:e2e
```
- Expected: All Playwright tests pass
- If failures: Capture screenshots, document failures

## 3) Manual Smoke Tests

### TUI Smoke Test
Only required if PR modifies TUI code (`src/nyxgpt/tui.py` or related).

```bash
nyxgpt tui
```

**Checklist:**
- [ ] App starts without errors
- [ ] Ctrl+H: Help overlay displays
- [ ] Ctrl+S: Session picker opens and can switch sessions
- [ ] Ctrl+M: Models manager opens
- [ ] Ctrl+F: Message search opens
- [ ] Ctrl+P: Command palette opens and commands execute
- [ ] Chat message sends and receives response
- [ ] No crashes or exceptions in logs

**Pass criteria:** All items work without errors
**Skip criteria:** PR doesn't modify TUI code

### WebUI Smoke Test
Only required if PR modifies WebUI code (`web/` directory).

```bash
# Start services
nyxgpt ops restart web api

# Open browser
open http://localhost:3000
```

**Checklist:**
- [ ] Page loads without console errors
- [ ] Can create new chat session
- [ ] Can send message and receive response
- [ ] Can upload file (if RAG enabled)
- [ ] Can search messages
- [ ] Can switch sessions
- [ ] Can rename session
- [ ] Session state persists on reload
- [ ] No network errors in dev tools

**Pass criteria:** All items work without errors
**Skip criteria:** PR doesn't modify WebUI code

### API Smoke Test
Only required if PR modifies API code (`src/nyxgpt/app.py`).

```bash
# Health check
curl http://localhost:8000/health

# Info endpoint
curl http://localhost:8000/api/v1/info

# Chat endpoint (streaming)
curl -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"session": "test", "prompt": "Hello"}'

# RAG upload (if RAG code changed)
curl -X POST http://localhost:8000/api/v1/rag/upload \
  -F "file=@README.md"
```

**Pass criteria:** All endpoints return expected responses
**Skip criteria:** PR doesn't modify API code

## 4) Collect Results

### Automated Test Results
```bash
# Run full suite and save output
./scripts/qa_run_full_suite.sh <PR_NUMBER> > qa_results.txt 2>&1
```

### Manual Test Results
```bash
# Fill out checklist
./scripts/qa_manual_checklist.sh <PR_NUMBER>
```

## 5) Generate QA Report

```bash
# Create structured report
./scripts/qa_report.sh <PR_NUMBER> qa_results.txt
```

Report includes:
- Overall status (PASS/FAIL)
- Test results summary
- Failure details with stack traces
- Manual test checklist results
- Recommendation

## 6) Handle Failures

### Critical Failures (block merge)
For each critical failure:
1. Create QA Failure sub-issue using `gh issue create`
2. Title: `QA Failure: <brief description> (PR #<number>)`
3. Body template:
```markdown
Parent PR: #<number>
Test: <test name or manual step>
Severity: Critical

## Description
[What failed]

## Steps to Reproduce
[How to reproduce the failure]

## Expected Behavior
[What should happen]

## Actual Behavior
[What actually happened]

## Stack Trace / Logs
```
[Error details]
```

## Impact
[Why this blocks merge]
```
4. Labels: `QA Failure`, `bug`
5. Assign to: developer-agent
6. Set status: In Progress
7. Link to parent PR

### Non-Critical Issues (document but don't block)
- Add to QA report as warnings
- Create follow-up issues if needed
- Allow merge to proceed

## 7) Post QA Report

```bash
# Post report as PR comment
gh pr comment <PR_NUMBER> --body-file qa_report.md

# Add label
gh pr edit <PR_NUMBER> --add-label "qa-pass"  # or "qa-fail"
```

## 8) Escalation

Notify @dkblinux98 if:
- Critical infrastructure issues prevent QA execution
- Flaky tests need investigation
- Multiple PRs failing the same tests (regression pattern)
- QA process improvements needed

## 9) QA Pass Criteria

**PASS:**
- All automated tests pass (100%)
- No critical failures in applicable smoke tests
- Minor issues documented but non-blocking
- Add label: `qa-pass`
- Comment: "QA APPROVED - All tests pass"

**FAIL:**
- Any automated test failures
- Critical smoke test failures
- Runtime errors or crashes
- Broken user-facing functionality
- Add label: `qa-fail`
- Create QA Failure sub-issues
- Comment: "QA BLOCKED - See failures above"

## 10) Tools

### Test Execution
- `pytest` - Unit and integration tests
- `playwright` - WebUI E2E tests
- `curl` - API smoke tests

### QA Scripts
- `qa_run_full_suite.sh` - Run all automated tests
- `qa_manual_checklist.sh` - Interactive manual test checklist
- `qa_report.sh` - Generate structured QA report

### Utilities
- `gh pr checks` - View CI status
- `gh pr view` - View PR details
- `gh issue create` - Create QA Failure sub-issues
- `nyxgpt ops doctor` - Check service health
