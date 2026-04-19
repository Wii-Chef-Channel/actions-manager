# Actions Manager

A web UI to manage GitHub Actions workflows across your organization. Trigger workflows, set scheduling frequencies, and monitor status — no cron literacy required.

## Features

| Feature | How |
|---------|-----|
| **Org & User Support** | Automatically detects if the name is a GitHub Org or User account |
| **Enable/disable repo** | Toggle switch next to repo name |
| **Set workflow frequency** | Dropdown: Every 5/10/15/30 min, 1/2/4/6/12 hours, Daily, or Custom cron |
| **Parallel Execution** | Fetches workflow statuses in parallel for lightning-fast UI loading |
| **Trigger single workflow** | ▶ Run button per row |
| **Trigger selected workflows** | Check boxes → "Trigger Selected" |
| **Trigger all workflows** | "Trigger All" in header |
| **Built-in scheduler** | Toggle in ⚙ Config — triggers enabled workflows automatically |
| **Auto-refresh** | Configurable (default 30s) |
| **Branch selection** | Dropdown per workflow (main, master, develop) |
| **State persistence** | Sidebar selection and expanded repos persist across page refreshes |

## Architecture

```
Browser ──http──> Gunicorn (WSGI) ──GitHub API──> GitHub
                         │
                         └── Scheduler thread (singleton)
```

### Components

1. **Flask app** (`app.py`) — Production-hardened REST API
2. **Web UI** (`index.html`) — Hardened zero-framework SPA with defensive rendering
3. **Config** (`data/config.json`) — Atomic JSON storage in the `am-config` volume
4. **Scheduler thread** — Timezone-aware singleton background thread

### PAT Resolution

The app resolves the GitHub PAT in this order:

1. **Environment variable** `GITHUB_PAT` (highest priority)
2. **Config file** `data/config.json` → `github_pat` field (set via ⚙ Config UI)

## Security & Reliability

- **Enforced Authentication**: Basic Auth is mandatory if credentials are provided.
- **Non-Root Execution**: The container runs as a non-privileged `appuser`.
- **Atomic Writes**: Configuration is saved via write-to-temp + atomic rename to prevent corruption.
- **CSRF Protection**: All mutating API endpoints require `application/json` content-type.
- **Constant-Time Comparison**: Auth verification uses `hmac.compare_digest` to prevent timing attacks.
- **Production Server**: Uses **Gunicorn** with a stable 1-worker, 4-thread configuration.
- **Input Sanitization**: All repository, workflow, and branch names are regex-validated.

## Deploy via Portainer

### 1. Get a GitHub PAT
1. Go to [GitHub Settings](https://github.com/settings/tokens) → **Classic token**
2. Check the `repo` scope and copy the token.

### 2. Add the stack in Portainer
1. **Stacks** → **Add stack** → **Repository** tab.
2. **Repository URL**: `https://github.com/Wii-Chef-Channel/actions-manager.git`
3. **Branch**: `master`
4. Add the following **Environment variables**:

| Variable | Value | Required |
|----------|-------|----------|
| `GITHUB_PAT` | `ghp_your_token_here` | **Yes** |
| `ORG_NAME` | `YourOrgOrUsername` | No (default) |
| `TIMEZONE` | `America/New_York` | No (default UTC) |
| `BASIC_AUTH_USER` | `admin` | No (Highly Recommended) |
| `BASIC_AUTH_PASS` | `yourpassword` | No (Highly Recommended) |

5. **Deploy the stack**

### 3. Configure in the UI
Click **⚙ Config** to:
- Set/refresh your GitHub PAT
- Change the target Org/User name
- Set your local timezone (e.g., `America/Los_Angeles`)
- **Enable the scheduler** (toggle button)

## Scheduler Presets

| Dropdown label | Cron expression | Meaning |
|----------------|-----------------|---------|
| Disabled | — | Scheduler won't trigger this workflow |
| Every 5-30 min | `*/X * * * *` | Triggers every X minutes |
| Every 1-12 hours | `0 */X * * *` | Triggers at minute 0 every X hours |
| Daily | `0 0 * * *` | Triggers at midnight in your **configured timezone** |
| Custom cron | (user input) | Any valid cron expression |

## Local Development

```bash
cd actions-manager
pip install -r requirements.txt
python app.py
```

## Updating
To apply updates from GitHub:
1. In Portainer, go to your stack → **Editor**.
2. Click **Update the stack**.
3. Toggle **"Pull latest image"** (or **"Re-pull image"**) to **ON**.
4. Click **Update**.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No workflows showing | Check PAT has `repo` scope and is valid |
| Scheduler not triggering | Verify scheduler is enabled in ⚙ Config |
| Repos disappear on refresh | Check that `ORG_NAME` or your UI config matches exactly |
| Error: HTTP 401 | Check your Basic Auth credentials or PAT validity |
| "Invalid JSON" in UI | Force refresh (`Ctrl+F5`) to clear browser cache |
