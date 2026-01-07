#!/bin/bash
# Automated PR creation script that enforces all GitHub project field requirements
# Usage: ./scripts/create-pr.sh <issue-number> <pr-title> <pr-body-file>

set -e

ISSUE_NUM=$1
PR_TITLE=$2
PR_BODY_FILE=$3

if [ -z "$ISSUE_NUM" ] || [ -z "$PR_TITLE" ] || [ -z "$PR_BODY_FILE" ]; then
    echo "Usage: ./scripts/create-pr.sh <issue-number> <pr-title> <pr-body-file>"
    echo "Example: ./scripts/create-pr.sh 2600 'feat(api): implement session export (#2600)' pr-body.txt"
    exit 1
fi

echo "🔍 Getting parent issue fields..."

# Get parent issue milestone and project fields
ISSUE_DATA=$(gh api graphql -f query="
  query {
    repository(owner: \"dkblinux98\", name: \"myGPT\") {
      issue(number: $ISSUE_NUM) {
        milestone { title id }
        projectItems(first: 5) {
          nodes {
            project { number }
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  field {
                    ... on ProjectV2SingleSelectField {
                      name
                      id
                    }
                  }
                  name
                  optionId
                }
                ... on ProjectV2ItemFieldIterationValue {
                  field {
                    ... on ProjectV2IterationField {
                      name
                      id
                    }
                  }
                  title
                  iterationId
                }
              }
            }
          }
        }
      }
    }
  }
")

# Extract values
MILESTONE_TITLE=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.milestone.title')
MILESTONE_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.milestone.id')

# Get project field values
PRIORITY_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Priority") | .field.id')
PRIORITY_OPTION=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Priority") | .optionId')

MODULE_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Module") | .field.id')
MODULE_OPTION=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Module") | .optionId')

SPRINT_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Sprint") | .field.id')
SPRINT_ITERATION=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Sprint") | .iterationId')

EFFORT_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Effort") | .field.id')
EFFORT_OPTION=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Effort") | .optionId')

STATUS_ID=$(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Status") | .field.id')
IN_REVIEW_OPTION="be1a4979"  # "In Review" status option ID

echo "✅ Parent issue fields:"
echo "   Milestone: $MILESTONE_TITLE"
echo "   Priority: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Priority") | .name')"
echo "   Module: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Module") | .name')"
echo "   Sprint: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Sprint") | .title')"
echo "   Effort: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Effort") | .name')"

# Create PR
echo ""
echo "📝 Creating PR..."
PR_BODY=$(cat "$PR_BODY_FILE")
PR_URL=$(gh pr create --base v1.0.0 --title "$PR_TITLE" --body "$PR_BODY")
PR_NUMBER=$(echo "$PR_URL" | grep -oE '[0-9]+$')

echo "✅ Created PR #$PR_NUMBER"

# Assign milestone
echo ""
echo "🏷️  Assigning milestone..."
PR_ID=$(gh api graphql -f query="
  query {
    repository(owner: \"dkblinux98\", name: \"myGPT\") {
      pullRequest(number: $PR_NUMBER) {
        id
      }
    }
  }
" | jq -r '.data.repository.pullRequest.id')

gh api graphql -f query="
  mutation {
    updatePullRequest(input: {
      pullRequestId: \"$PR_ID\"
      milestoneId: \"$MILESTONE_ID\"
    }) {
      pullRequest {
        number
      }
    }
  }
" > /dev/null

echo "✅ Assigned milestone: $MILESTONE_TITLE"

# Add to project
echo ""
echo "📊 Adding to project..."
gh project item-add 2 --owner dkblinux98 --url "$PR_URL"

# Wait for GitHub to sync
echo "⏳ Waiting for GitHub sync..."
sleep 5

# Get PR project item ID
PR_ITEM_ID=$(gh api graphql -f query="
  query {
    repository(owner: \"dkblinux98\", name: \"myGPT\") {
      pullRequest(number: $PR_NUMBER) {
        projectItems(first: 5) {
          nodes {
            id
            project { number }
          }
        }
      }
    }
  }
" | jq -r '.data.repository.pullRequest.projectItems.nodes[] | select(.project.number == 2) | .id')

echo "✅ Added to project (item ID: $PR_ITEM_ID)"

# Set all project fields
PROJECT_ID="PVT_kwHOAEElec4BLnao"

echo ""
echo "⚙️  Setting project fields..."

# Status (In Review)
gh api graphql -f query="
  mutation {
    updateProjectV2ItemFieldValue(input: {
      projectId: \"$PROJECT_ID\"
      itemId: \"$PR_ITEM_ID\"
      fieldId: \"$STATUS_ID\"
      value: {singleSelectOptionId: \"$IN_REVIEW_OPTION\"}
    }) {
      projectV2Item { id }
    }
  }
" > /dev/null
echo "  ✅ Status: In Review"

# Priority
if [ "$PRIORITY_OPTION" != "null" ]; then
  gh api graphql -f query="
    mutation {
      updateProjectV2ItemFieldValue(input: {
        projectId: \"$PROJECT_ID\"
        itemId: \"$PR_ITEM_ID\"
        fieldId: \"$PRIORITY_ID\"
        value: {singleSelectOptionId: \"$PRIORITY_OPTION\"}
      }) {
        projectV2Item { id }
      }
    }
  " > /dev/null
  echo "  ✅ Priority: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Priority") | .name')"
fi

# Module
if [ "$MODULE_OPTION" != "null" ]; then
  gh api graphql -f query="
    mutation {
      updateProjectV2ItemFieldValue(input: {
        projectId: \"$PROJECT_ID\"
        itemId: \"$PR_ITEM_ID\"
        fieldId: \"$MODULE_ID\"
        value: {singleSelectOptionId: \"$MODULE_OPTION\"}
      }) {
        projectV2Item { id }
      }
    }
  " > /dev/null
  echo "  ✅ Module: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Module") | .name')"
fi

# Sprint
if [ "$SPRINT_ITERATION" != "null" ]; then
  gh api graphql -f query="
    mutation {
      updateProjectV2ItemFieldValue(input: {
        projectId: \"$PROJECT_ID\"
        itemId: \"$PR_ITEM_ID\"
        fieldId: \"$SPRINT_ID\"
        value: {iterationId: \"$SPRINT_ITERATION\"}
      }) {
        projectV2Item { id }
      }
    }
  " > /dev/null
  echo "  ✅ Sprint: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Sprint") | .title')"
fi

# Effort
if [ "$EFFORT_OPTION" != "null" ]; then
  gh api graphql -f query="
    mutation {
      updateProjectV2ItemFieldValue(input: {
        projectId: \"$PROJECT_ID\"
        itemId: \"$PR_ITEM_ID\"
        fieldId: \"$EFFORT_ID\"
        value: {singleSelectOptionId: \"$EFFORT_OPTION\"}
      }) {
        projectV2Item { id }
      }
    }
  " > /dev/null
  echo "  ✅ Effort: $(echo "$ISSUE_DATA" | jq -r '.data.repository.issue.projectItems.nodes[0].fieldValues.nodes[] | select(.field.name == "Effort") | .name')"
fi

echo ""
echo "🎉 PR #$PR_NUMBER created successfully with all fields configured!"
echo "   URL: $PR_URL"
