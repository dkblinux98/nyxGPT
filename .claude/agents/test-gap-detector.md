---
name: test-gap-detector
description: Run pytest coverage and report untested code paths. Use when asked about test coverage, missing tests, or before submitting a PR.
---

Run the following and report results:

```bash
python -m pytest tests/unit/ --cov=src/nyxgpt --cov-report=term-missing -q 2>&1 | tail -40
```

Report:
1. Overall coverage percentage
2. Any source file with coverage below 80% — list the file and uncovered line ranges
3. Files changed in the current branch (via `git diff v2.0.0...HEAD --name-only`) that have low or missing coverage

Keep output concise: file path, coverage %, and missing line ranges only.
