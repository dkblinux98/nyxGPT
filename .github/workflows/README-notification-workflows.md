# Notification Workflows

## Overview

Notification workflows send alerts to external systems (Slack, email, etc.) when specific events occur in the repository. These workflows are passive observers that don't modify repository state.

## Merge Conflict Notifications

**Workflow**: `notify-merge-conflicts.yml`

Automatically notifies when a pull request has merge conflicts with its base branch.

### Architecture

```
PR opened/updated/synchronized
    ↓
┌──────────────────────────────────────┐
│ notify-merge-conflicts.yml           │
│ - Check PR mergeable status          │
│ - Extract PR metadata                │
│                                      │
│ If mergeable === false:              │
│   → Send Slack notification          │
│   → Include PR details               │
│   → Provide "View PR" button         │
│                                      │
│ If mergeable === true/null:          │
│   → No notification sent             │
└──────────────────────────────────────┘
```

### Trigger Events

- `pull_request.opened` - New PR created
- `pull_request.synchronize` - PR updated with new commits
- `pull_request.reopened` - Closed PR reopened

### How It Works

1. **Check Merge Status**: Uses GitHub Script API to check `pr.mergeable`:
   - `true` = No conflicts, can merge
   - `false` = Has conflicts, cannot merge
   - `null` = GitHub still computing (no notification sent)

2. **Extract Metadata**: If conflicts detected, extracts:
   - PR number, title, URL
   - Author username
   - Head branch → Base branch
   - Repository name

3. **Send Notification**: Posts formatted Slack message with:
   - Header: "⚠️ Merge Conflict in Pull Request"
   - PR details in structured fields
   - Explanation text
   - "View PR" button linking to PR

### Notification Format

```
⚠️ Merge Conflict in Pull Request

PR: #123 - Fix authentication bug
Author: developer-agent
Branches: feat/auth-fix → v1.0.0
Repository: owner/repo

This PR has merge conflicts with the base branch and
cannot be merged until they are resolved.

[View PR]
```

### Configuration

**Required GitHub Secrets**:
- `SLACK_WEBHOOK_URL` - Incoming webhook URL from Slack app

**Setup Steps**:

1. Create or configure Slack app with Incoming Webhooks
2. Configure webhook channel (can be a DM channel)
3. Copy webhook URL
4. Add to GitHub secrets: `gh secret set SLACK_WEBHOOK_URL`

**Permissions**:
```yaml
permissions:
  pull-requests: read
  contents: read
```

### Why Webhooks?

The workflow uses Slack incoming webhooks instead of bot tokens because:

1. **Simplicity**: Only requires single webhook URL, no user ID management
2. **Reusability**: Same workflow can be copied to other repos with different webhook configs
3. **Channel Flexibility**: Webhook can target any channel or DM configured in Slack
4. **No Scope Creep**: Limited to posting messages, can't read or modify Slack data

### Testing

To test the notification:

1. Create a PR against the base branch
2. Make conflicting changes to base branch
3. Push to base branch (creates conflict)
4. Observe Slack notification with PR details

Alternative: Update PR by rebasing on conflicted base branch.

### Limitations

- **Null mergeable status**: GitHub may return `null` while computing merge status - no notification sent until status is definitively `false`
- **Conflict resolution**: Notification is informational only - doesn't auto-resolve conflicts
- **Rate limits**: Slack webhook rate limits apply (~1 message per second)

### Related Files

- `notify-merge-conflicts.yml` - Workflow implementation
- No associated agent scripts (passive notification only)
- No related runbooks (not part of agent workflows)

### Future Enhancements

Potential additions:
- Support for other notification channels (email, Discord, Teams)
- Conflict resolution suggestions in notification
- Auto-assignment to PR author when conflicts detected
- Integration with project management tools
