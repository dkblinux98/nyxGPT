#!/usr/bin/env bash
set -euo pipefail

# QA Agent: Run Full Test Suite
# Usage: qa_run_full_suite.sh <PR_NUMBER>

PR_NUMBER="$1"

echo "=================================================="
echo "QA Full Test Suite - PR #${PR_NUMBER}"
echo "=================================================="
echo ""

# Get PR details
echo "[INFO] Fetching PR details..."
PR_BRANCH=$(gh pr view "$PR_NUMBER" --json headRefName -q '.headRefName')
echo "[INFO] PR Branch: $PR_BRANCH"
echo ""

# Checkout PR branch
echo "[INFO] Checking out PR branch..."
git fetch origin "$PR_BRANCH"
git checkout "$PR_BRANCH"
echo ""

# Install dependencies
echo "[INFO] Installing dependencies..."
pip install -e . > /dev/null 2>&1
echo "[INFO] Python dependencies installed"
echo ""

# Run unit tests
echo "=================================================="
echo "UNIT TESTS"
echo "=================================================="
python -m pytest tests/unit -v --tb=short --no-header 2>&1
UNIT_EXIT=$?
echo ""

# Run integration tests
echo "=================================================="
echo "INTEGRATION TESTS"
echo "=================================================="
python -m pytest tests/integration -v -m integration --tb=short --no-header 2>&1
INTEGRATION_EXIT=$?
echo ""

# Run E2E tests if web directory exists
if [ -d "web" ]; then
    echo "=================================================="
    echo "E2E TESTS (WebUI)"
    echo "=================================================="

    # Check if Playwright is installed
    if [ -f "web/package.json" ] && grep -q "playwright" web/package.json; then
        cd web

        # Install node modules if needed
        if [ ! -d "node_modules" ]; then
            echo "[INFO] Installing Node.js dependencies..."
            npm install > /dev/null 2>&1
        fi

        # Run E2E tests if they exist
        if npm run | grep -q "test:e2e"; then
            npm run test:e2e 2>&1
            E2E_EXIT=$?
        else
            echo "[SKIP] No E2E tests configured"
            E2E_EXIT=0
        fi

        cd ..
    else
        echo "[SKIP] Playwright not configured"
        E2E_EXIT=0
    fi
    echo ""
fi

# Summary
echo "=================================================="
echo "TEST SUMMARY"
echo "=================================================="
echo "Unit Tests:        $([ $UNIT_EXIT -eq 0 ] && echo 'PASS ✓' || echo 'FAIL ✗')"
echo "Integration Tests: $([ $INTEGRATION_EXIT -eq 0 ] && echo 'PASS ✓' || echo 'FAIL ✗')"
echo "E2E Tests:         $([ ${E2E_EXIT:-0} -eq 0 ] && echo 'PASS ✓' || echo 'FAIL ✗')"
echo ""

# Overall result
OVERALL_EXIT=$((UNIT_EXIT + INTEGRATION_EXIT + ${E2E_EXIT:-0}))
if [ $OVERALL_EXIT -eq 0 ]; then
    echo "Overall: PASS ✓"
    exit 0
else
    echo "Overall: FAIL ✗"
    exit 1
fi
