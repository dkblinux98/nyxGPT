#!/usr/bin/env bash
set -euo pipefail

# QA Agent: Manual Smoke Test Checklist
# Usage: qa_manual_checklist.sh <PR_NUMBER>

PR_NUMBER="$1"
RESULTS_FILE="qa_manual_results_${PR_NUMBER}.txt"

echo "=================================================="
echo "QA Manual Smoke Test Checklist - PR #${PR_NUMBER}"
echo "=================================================="
echo ""

# Determine what needs testing based on changed files
echo "[INFO] Analyzing changed files..."
CHANGED_FILES=$(gh pr view "$PR_NUMBER" --json files -q '.files[].path')

TUI_CHANGED=false
WEBUI_CHANGED=false
API_CHANGED=false

if echo "$CHANGED_FILES" | grep -q "src/nyxgpt/tui.py"; then
    TUI_CHANGED=true
fi

if echo "$CHANGED_FILES" | grep -q "web/"; then
    WEBUI_CHANGED=true
fi

if echo "$CHANGED_FILES" | grep -q "src/nyxgpt/app.py"; then
    API_CHANGED=true
fi

echo ""
echo "Test Requirements:"
echo "- TUI: $([ "$TUI_CHANGED" = true ] && echo 'REQUIRED' || echo 'SKIP')"
echo "- WebUI: $([ "$WEBUI_CHANGED" = true ] && echo 'REQUIRED' || echo 'SKIP')"
echo "- API: $([ "$API_CHANGED" = true ] && echo 'REQUIRED' || echo 'SKIP')"
echo ""

# Initialize results
> "$RESULTS_FILE"

# Function to prompt for test result
check_item() {
    local prompt="$1"
    local result

    while true; do
        read -r -p "$prompt [pass/fail/skip]: " result
        case "$result" in
            pass|p) echo "PASS"; return 0 ;;
            fail|f) echo "FAIL"; return 1 ;;
            skip|s) echo "SKIP"; return 0 ;;
            *) echo "Invalid input. Use pass/fail/skip" ;;
        esac
    done
}

# TUI Smoke Test
if [ "$TUI_CHANGED" = true ]; then
    echo "=================================================="
    echo "TUI SMOKE TEST"
    echo "=================================================="
    echo "Please run: nyxgpt tui"
    echo ""
    echo "Testing checklist:"

    TUI_PASS=true

    result=$(check_item "1. App starts without errors")
    echo "TUI: App starts - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "2. Ctrl+H (Help overlay displays)")
    echo "TUI: Ctrl+H Help - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "3. Ctrl+S (Session picker works)")
    echo "TUI: Ctrl+S Sessions - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "4. Ctrl+M (Models manager works)")
    echo "TUI: Ctrl+M Models - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "5. Ctrl+F (Message search works)")
    echo "TUI: Ctrl+F Search - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "6. Ctrl+P (Command palette works)")
    echo "TUI: Ctrl+P Palette - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    result=$(check_item "7. Chat message sends/receives")
    echo "TUI: Chat - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && TUI_PASS=false

    echo "TUI Overall: $([ "$TUI_PASS" = true ] && echo 'PASS' || echo 'FAIL')" >> "$RESULTS_FILE"
    echo ""
else
    echo "TUI: SKIP (no TUI changes)" >> "$RESULTS_FILE"
fi

# WebUI Smoke Test
if [ "$WEBUI_CHANGED" = true ]; then
    echo "=================================================="
    echo "WEBUI SMOKE TEST"
    echo "=================================================="
    echo "Please ensure services are running:"
    echo "  nyxgpt ops restart web api"
    echo "  open http://localhost:3000"
    echo ""
    echo "Testing checklist:"

    WEBUI_PASS=true

    result=$(check_item "1. Page loads without errors")
    echo "WebUI: Page load - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "2. Can create new chat session")
    echo "WebUI: New chat - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "3. Can send/receive messages")
    echo "WebUI: Chat - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "4. Can upload file (if RAG enabled)")
    echo "WebUI: File upload - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "5. Can search messages")
    echo "WebUI: Search - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "6. Can switch sessions")
    echo "WebUI: Switch session - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    result=$(check_item "7. No console errors in DevTools")
    echo "WebUI: Console - $result" >> "$RESULTS_FILE"
    [ "$result" = "FAIL" ] && WEBUI_PASS=false

    echo "WebUI Overall: $([ "$WEBUI_PASS" = true ] && echo 'PASS' || echo 'FAIL')" >> "$RESULTS_FILE"
    echo ""
else
    echo "WebUI: SKIP (no WebUI changes)" >> "$RESULTS_FILE"
fi

# API Smoke Test
if [ "$API_CHANGED" = true ]; then
    echo "=================================================="
    echo "API SMOKE TEST"
    echo "=================================================="
    echo "Testing API endpoints..."
    echo ""

    API_PASS=true

    # Health check
    if curl -s http://localhost:8000/health | grep -q "ok"; then
        echo "API: Health check - PASS" | tee -a "$RESULTS_FILE"
    else
        echo "API: Health check - FAIL" | tee -a "$RESULTS_FILE"
        API_PASS=false
    fi

    # Info endpoint
    if curl -s http://localhost:8000/api/v1/info | grep -q "version"; then
        echo "API: Info endpoint - PASS" | tee -a "$RESULTS_FILE"
    else
        echo "API: Info endpoint - FAIL" | tee -a "$RESULTS_FILE"
        API_PASS=false
    fi

    echo "API Overall: $([ "$API_PASS" = true ] && echo 'PASS' || echo 'FAIL')" >> "$RESULTS_FILE"
    echo ""
else
    echo "API: SKIP (no API changes)" >> "$RESULTS_FILE"
fi

# Summary
echo "=================================================="
echo "MANUAL TEST RESULTS"
echo "=================================================="
cat "$RESULTS_FILE"
echo ""
echo "Results saved to: $RESULTS_FILE"
