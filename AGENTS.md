# Agent System (nyxGPT)

This project uses the **nyxAgent framework** for multi-agent SDLC automation.

**Package**: `nyxagent`
**Repository**: https://github.com/dkblinux98/nyxAgent
**Configuration**: `.nyxagent/config.yaml`

---

## Framework Documentation

For complete agent details, see the nyxAgent framework documentation:

```bash
nyxagent docs
```

Or visit: https://github.com/dkblinux98/nyxAgent/tree/v1.0.0/src/nyxagent/docs

---

## Project Status Semantics (nyxGPT)

Backlog      – approved, unscheduled
In Progress  – active development
In Review    – awaiting review (agent review OR human stakeholder acceptance after merge)
For Release  – stakeholder accepted, ready for release (human sets this)
Closed       – released (human only)

**Important**: After merge, issues remain in "In Review" status (CLOSED in GitHub, but "In Review" in project) until human stakeholder acceptance. The human owner moves accepted issues to "For Release".

---

## Project-Specific Rules

### Global Rules

- Do not merge to main/master
- Leave an auditable comment for every state change
- Do not improvise workflow

### Project Hygiene (All Agents)

Every agent is responsible for verifying project hygiene before reassigning issues/PRs:
- PRs must be linked to issues via `Closes #ISSUE` in PR body
- Issues must have required project fields populated (Status, Priority, etc.)
- Merged PRs without linked issues must be corrected before handoff
- Project fields must be accurate and up-to-date before state transitions

---

## Agent Roles

The nyxAgent framework provides the following agents:

- **scrummaster-agent** - Backlog management and issue selection
- **developer-agent** - Implementation and PR creation
- **review-agent** - Code review and merge operations
- **qa-agent** - Quality assurance and testing
- **executive assistant** (Claude for ad-hoc tasks) - Supports human owner with administrative tasks

For detailed responsibilities, permissions, and scripts, see:
- nyxAgent agent charters: `nyxagent docs`
- nyxAgent GitHub: https://github.com/dkblinux98/nyxAgent

---

## Configuration

This project's agent configuration is in `.nyxagent/config.yaml`:

```yaml
nyxagent:
  version: "1.0.0"

project:
  name: "nyxGPT"
  github_org: "dkblinux98"
  github_repo: "nyxGPT"
  project_number: 1

branches:
  release_branch: "v2.0.0"
  main_branch: "v2.0.0"

agents:
  developer:
    enabled: true
  review:
    enabled: true
  scrummaster:
    enabled: true
  qa:
    enabled: true
  stakeholder:
    enabled: false
```

---

Final rule:
Follow the nyxAgent framework documentation and this project's configuration exactly.
