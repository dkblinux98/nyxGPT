#!/usr/bin/env python3
"""
Watch workflow runs in the repository.
Displays live-updating table showing all non-skipped workflow runs by default.
Can optionally monitor a specific issue and auto-exit when complete.

Usage:
  watch_agents.py [issue_number] [--poll-interval SECONDS] [--include-skipped]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GRAY = "\033[90m"


def load_config() -> Dict[str, str]:
    """Load configuration from .nyxGPT/config.ini"""
    config_path = os.path.expanduser("~/.nyxGPT/config.ini")
    config = {}

    with open(config_path, 'r') as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#') and not line.startswith('['):
                key, value = line.split('=', 1)
                # Normalize keys to uppercase for consistency with bash scripts
                config[key.strip().upper()] = value.strip()

    return config


def run_gh_command(args: List[str]) -> str:
    """Run gh CLI command and return output"""
    try:
        result = subprocess.run(
            ['gh'] + args,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running gh command: {e.stderr}", file=sys.stderr)
        return ""


def get_project_id(config: Dict[str, str]) -> Optional[str]:
    """Get Project V2 ID"""
    project_owner = config.get('PROJECT_OWNER', '')
    project_number = config.get('PROJECT_NUMBER', '')

    if not project_owner or not project_number:
        return None

    query = f'''
    query {{
      user(login: "{project_owner}") {{
        projectV2(number: {project_number}) {{
          id
        }}
      }}
    }}
    '''

    try:
        result = run_gh_command(['api', 'graphql', '-f', f'query={query}'])
        if result:
            data = json.loads(result)
            return data.get('data', {}).get('user', {}).get('projectV2', {}).get('id')
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        pass

    return None


def get_issue_info(issue: str, config: Dict[str, str]) -> Tuple[str, str]:
    """Get issue status and assignee from GitHub Project"""
    project_id = get_project_id(config)
    if not project_id:
        return ("unknown", "unknown")

    query = '''
    query($project:ID!) {
      node(id:$project) {
        ... on ProjectV2 {
          items(first:100) {
            nodes {
              content {
                __typename
                ... on Issue { number }
              }
              fieldValues(first:50) {
                nodes {
                  __typename
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    field { ... on ProjectV2SingleSelectField { name } }
                    name
                  }
                  ... on ProjectV2ItemFieldUserValue {
                    field { ... on ProjectV2Field { name } }
                    users(first:10) { nodes { login } }
                  }
                }
              }
            }
          }
        }
      }
    }
    '''

    try:
        result = run_gh_command([
            'api', 'graphql',
            '-f', f'query={query}',
            '-F', f'project={project_id}'
        ])

        if not result:
            return ("unknown", "unknown")

        data = json.loads(result)
        items = data.get('data', {}).get('node', {}).get('items', {}).get('nodes', [])

        status_field = config.get('STATUS_FIELD', 'Status')

        for item in items:
            content = item.get('content', {})
            if content.get('__typename') == 'Issue' and content.get('number') == int(issue):
                status = "unknown"
                assignee = "unknown"

                for field_value in item.get('fieldValues', {}).get('nodes', []):
                    if field_value.get('__typename') == 'ProjectV2ItemFieldSingleSelectValue':
                        field_name = field_value.get('field', {}).get('name', '')
                        if field_name == status_field:
                            status = field_value.get('name', 'unknown')
                    elif field_value.get('__typename') == 'ProjectV2ItemFieldUserValue':
                        field_name = field_value.get('field', {}).get('name', '')
                        if field_name == 'Assignees':
                            users = field_value.get('users', {}).get('nodes', [])
                            if users:
                                assignee = users[0].get('login', 'unknown')

                return (status, assignee)

        return ("unknown", "unknown")
    except Exception as e:
        print(f"Error getting issue info: {e}", file=sys.stderr)
        return ("unknown", "unknown")


def get_workflow_agent(workflow_name: str, config: Dict[str, str]) -> str:
    """Determine which agent is responsible for a workflow"""
    # Pattern-based agent detection for flexibility
    name_lower = workflow_name.lower()

    if "developer" in name_lower or "implement" in name_lower:
        return config.get('DEV_AGENT', 'developer-agent')
    elif "review" in name_lower:
        return config.get('REVIEW_AGENT', 'review-agent')
    elif "scrummaster" in name_lower or "scrum" in name_lower or "backlog" in name_lower:
        return config.get('SCRUM_AGENT', 'scrummaster-agent')
    elif "claude code" in name_lower and workflow_name == "Claude Code":
        return "varies"  # Can be triggered by any agent or human
    elif any(keyword in name_lower for keyword in ["merge conflict", "validate", "check"]):
        return "automation"

    return "N/A"


def format_duration(seconds: int) -> str:
    """Format duration in human readable form"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def get_status_icon(status: str, conclusion: Optional[str] = None) -> str:
    """Get emoji icon for status"""
    if status in ("in_progress", "IN_PROGRESS"):
        return "🔄"
    elif status in ("queued", "QUEUED"):
        return "⏳"
    elif status in ("completed", "COMPLETED"):
        if conclusion in ("success", "SUCCESS"):
            return "✅"
        elif conclusion in ("failure", "FAILURE"):
            return "❌"
        elif conclusion in ("cancelled", "CANCELLED"):
            return "🚫"
        elif conclusion in ("skipped", "SKIPPED"):
            return "⏭️"
    return "❓"


def get_workflow_runs(repo: str, issue: Optional[str] = None, include_skipped: bool = False) -> List[Dict]:
    """Get workflow runs (all non-skipped by default, optionally filter by issue)"""
    result = run_gh_command([
        'run', 'list',
        '--repo', repo,
        '--limit', '100',
        '--json', 'databaseId,workflowName,status,conclusion,createdAt,updatedAt,url,headBranch'
    ])

    if not result:
        return []

    runs = json.loads(result)

    # Filter runs
    filtered_runs = []
    for run in runs:
        workflow_name = run.get('workflowName', '')
        head_branch = run.get('headBranch', '')
        conclusion = run.get('conclusion', '')

        # Skip workflows with 'skipped' conclusion unless requested
        if not include_skipped and conclusion == 'skipped':
            continue

        # If issue specified, only include runs related to that issue
        if issue is not None:
            # Include if branch matches issue pattern (e.g., feat/2678-auto)
            if f"/{issue}-" not in head_branch and f"-{issue}-" not in head_branch:
                continue

        filtered_runs.append(run)

    return filtered_runs[:20]  # Limit to 20 most recent


def print_table_header():
    """Print table header"""
    print(f"\n{'Agent':<20} {'Status':<12} {'Workflow':<35} {'Duration':<10} {'Link'}")
    print("─" * 120)


def print_run_row(run: Dict, config: Dict[str, str]):
    """Print a single run row"""
    workflow_name = run.get('workflowName', 'Unknown')
    agent = get_workflow_agent(workflow_name, config)
    status = run.get('status', 'unknown')
    conclusion = run.get('conclusion', '')
    url = run.get('url', '')

    # Calculate duration
    created_at = run.get('createdAt', '')
    updated_at = run.get('updatedAt', '')
    duration = ""

    if created_at and updated_at:
        try:
            created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            updated = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
            duration_seconds = int((updated - created).total_seconds())
            if duration_seconds > 0:
                duration = format_duration(duration_seconds)
        except (ValueError, TypeError, AttributeError):
            pass

    # Get icon and display status
    icon = get_status_icon(status, conclusion)
    if status == "completed":
        display_status = f"{icon} {conclusion}"
    else:
        display_status = f"{icon} {status}"

    # Truncate workflow name if too long
    if len(workflow_name) > 33:
        workflow_name = workflow_name[:30] + "..."

    # Color code by status
    if status in ("in_progress", "IN_PROGRESS"):
        color = CYAN
    elif conclusion == "success":
        color = GREEN
    elif conclusion == "failure":
        color = RED
    elif conclusion == "cancelled":
        color = YELLOW
    else:
        color = GRAY

    print(f"{color}{agent:<20} {display_status:<12} {workflow_name:<35} {duration:<10} {url}{RESET}")


def clear_screen():
    """Clear terminal screen"""
    os.system('clear' if os.name == 'posix' else 'cls')


def main():
    parser = argparse.ArgumentParser(
        description="Watch GitHub Actions workflow runs (shows all non-skipped workflows by default)"
    )
    parser.add_argument(
        'issue',
        type=int,
        nargs='?',
        help='Issue number to monitor (optional - if omitted, shows all workflows)'
    )
    parser.add_argument(
        '--poll-interval',
        type=int,
        default=15,
        help='Polling interval in seconds (default: 15)'
    )
    parser.add_argument(
        '--include-skipped',
        action='store_true',
        help='Include skipped workflow runs in the output'
    )

    args = parser.parse_args()

    # Load config
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    repo = f"{config.get('REPO_OWNER', 'unknown')}/{config.get('REPO_NAME', 'unknown')}"
    status_in_review = config.get('STATUS_IN_REVIEW', 'In Review')
    human_owner = config.get('HUMAN_OWNER', 'unknown')

    if args.issue:
        print(f"🔍 Initializing watch for issue #{args.issue}...")
    else:
        skip_note = " (excluding skipped)" if not args.include_skipped else ""
        print(f"🔍 Initializing watch for all workflows{skip_note}...")
    print(f"Repository: {repo}")
    print(f"Poll interval: {args.poll_interval}s")
    time.sleep(2)

    first_run = True

    try:
        while True:
            if not first_run:
                clear_screen()
            first_run = False

            # Get current issue status if monitoring specific issue
            if args.issue:
                issue_status, issue_assignee = get_issue_info(str(args.issue), config)

                # Check exit condition
                if issue_status == status_in_review and issue_assignee == human_owner:
                    print("\n" + "═" * 80)
                    print(f"{GREEN}{BOLD}✅ Issue #{args.issue} has reached completion!{RESET}")
                    print()
                    print(f"   Status: {issue_status}")
                    print(f"   Assigned to: {issue_assignee}")
                    print()
                    print("   The issue is now ready for your stakeholder acceptance.")
                    print(f"   View at: https://github.com/{repo}/issues/{args.issue}")
                    print("═" * 80)
                    sys.exit(0)

            # Display header
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print("\n" + "═" * 80)
            if args.issue:
                print(f"{BOLD}🔍 Monitoring Issue #{args.issue}{RESET}")
                print("═" * 80)
                print(f"Last updated: {current_time}")
                print()
                print(f"📋 Issue Status: {YELLOW}{issue_status}{RESET}")
                print(f"👤 Assigned to: {CYAN}{issue_assignee}{RESET}")
                print()
                print(f"🎯 Exit Condition: Status='{status_in_review}' AND Assignee='{human_owner}'")
            else:
                print(f"{BOLD}🔍 Monitoring All Workflow Runs{RESET}")
                print("═" * 80)
                print(f"Last updated: {current_time}")
                if not args.include_skipped:
                    print(f"{GRAY}(Skipped workflows hidden - use --include-skipped to show all){RESET}")
            print("─" * 80)

            # Get and display workflow runs
            runs = get_workflow_runs(
                repo,
                str(args.issue) if args.issue else None,
                include_skipped=args.include_skipped
            )

            if runs:
                # Separate active and completed runs
                active_runs = [r for r in runs if r.get('status') != 'completed']
                completed_runs = [r for r in runs if r.get('status') == 'completed']

                if active_runs:
                    print(f"\n{BOLD}▶️  Active Runs:{RESET}")
                    print_table_header()
                    for run in active_runs:
                        print_run_row(run, config)

                if completed_runs:
                    print(f"\n{BOLD}✓  Recent Completed Runs (last 10):{RESET}")
                    print_table_header()
                    for run in completed_runs[:10]:
                        print_run_row(run, config)
            else:
                if args.issue:
                    print(f"\n{GRAY}📊 No workflow runs found for issue #{args.issue}.{RESET}")
                else:
                    suffix = " (excluding skipped)" if not args.include_skipped else ""
                    print(f"\n{GRAY}📊 No workflow runs found{suffix}.{RESET}")

            print("\n" + "─" * 80)
            print(f"⏱️  Next update in {args.poll_interval}s... (Ctrl+C to exit)")
            print()

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}Monitoring stopped by user.{RESET}")
        sys.exit(0)
    except Exception as e:
        print(f"\n{RED}Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
