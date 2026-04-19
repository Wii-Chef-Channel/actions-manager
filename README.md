# Actions Manager

A web UI to manage GitHub Actions workflows across your organization. Trigger workflows, set scheduling frequencies, and monitor status — no cron literacy required.

## Features

| Feature | How |
|---------|-----|
| **Enable/disable repo** | Toggle switch next to repo name |
| **Set workflow frequency** | Dropdown: Every 5/10/15/30 min, 1/2/4/6/12 hours, Daily, or Custom cron |
| **Trigger single workflow** | ▶ Run button per row |
| **Trigger selected workflows** | Check boxes → "Trigger Selected" |
| **Trigger all workflows** | "Trigger All" in header |
| **Built-in scheduler** | Toggle in ⚙ Config — triggers enabled workflows automatically |
| **Auto-refresh** | Configurable (default 30s) |
| **Branch selection** | Dropdown per workflow (main, master, develop) |
| **Last triggered** | Shows when each workflow was last triggered (manually or by scheduler) |
| **State persistence** | Sidebar selection and expanded repos persist across page refreshes (localStorage) |

## Architecture

```
Browser ──http──> Flask app ──GitHub API──> GitHub
                         │
                         └── Scheduler thread (background)
```

### Components

1. **Flask app** (`app.py`) — REST API + web server
2. **Web UI** (`index.html`) — single-page app, no framework dependencies
3. **Config** (`data/config.json`) — stored in the `am-config` Docker volume
4. **Scheduler thread** — runs in-process, checks every 60s for due workflows

### PAT Resolution

The app resolves the GitHub PAT in this order:

1. **Environment variable** `GITHUB_PAT` (highest priority)
2. **Config file** `data/config.json` → `github_pat` field (set via ⚙ Config UI)

This allows you to store your PAT in the UI as a fallback when the env var is not set.

### How the scheduler works

The scheduler runs as a background thread inside the Flask app:

1. **Wakes up** every 60 seconds
2. **Reads** `data/config.json` from disk
3. **Iterates** through all repos → workflows
4. **Checks** each workflow:
   - Is the repo enabled? (`repos.<repo>.enabled`)
   - Is the workflow schedule on? (`enabled_schedule: true`)
   - What frequency is set? (`cron` field)
5. **Parses** the cron expression using `croniter`
6. **Triggers** the workflow via GitHub API if it's due
7. **Saves** `last_triggered` timestamp to config for UI display
8. **Prevents** duplicate triggers within 2 minutes

### Frequency presets

The dropdown maps user-friendly labels to cron expressions:

| Dropdown label | Cron expression | Meaning |
|----------------|-----------------|---------|
| Disabled | — | Scheduler won't trigger this workflow |
| Every 5 min | `*/5 * * * *` | Every 5 minutes |
| Every 10 min | `*/10 * * * *` | Every 10 minutes |
| Every 15 min | `*/15 * * * *` | Every 15 minutes |
| Every 30 min | `*/30 * * * *` | Every 30 minutes |
| Every hour | `0 * * * *` | At minute 0 of every hour |
| Every 2 hours | `0 */2 * * *` | At minute 0 every 2 hours |
| Every 4 hours | `0 */4 * * *` | At minute 0 every 4 hours |
| Every 6 hours | `0 */6 * * *` | At minute 0 every 6 hours |
| Every 12 hours | `0 */12 * * *` | At minute 0 every 12 hours |
| Daily at midnight | `0 0 * * *` | Once per day at 00:00 |
| Custom cron | (user input) | Any valid cron expression |

### Config file structure

`data/config.json` (stored in the `am-config` Docker volume):

```json
{
  "org": "Wii-Chef-Channel",
  "refresh_interval": 30,
  "timezone": "America/New_York",
  "github_pat": "ghp_...",
  "repos": {
    "cruise-tracker": {
      "enabled": true,
      "workflows": {
        "12345678": {
          "enabled_schedule": true,
          "cron": "*/10 * * * *",
          "branch": "main",
          "last_triggered": 1713500000
        }
      }
    }
  }
}
```

| Field | Description |
|-------|-------------|
| `org` | GitHub organization name |
| `refresh_interval` | Auto-refresh interval in seconds |
| `timezone` | Display timezone (e.g., `America/New_York`) |
| `github_pat` | GitHub Personal Access Token with `repo` scope |
| `repos.<name>.enabled` | Toggle this repo on/off |
| `repos.<name>.workflows.<id>.enabled_schedule` | Enable/disable scheduling for this workflow |
| `repos.<name>.workflows.<id>.cron` | Cron expression or preset key |
| `repos.<name>.workflows.<id>.branch` | Branch to trigger from |
| `repos.<name>.workflows.<id>.last_triggered` | Unix timestamp of last trigger |

## Deploy via Portainer (no SSH needed)

### 1. Get a GitHub PAT

1. Go to https://github.com/settings/tokens → **Classic token**
2. Check the `repo` scope
3. Copy the token

### 2. Add the stack in Portainer

1. **Stacks** → **Add stack**
2. **Name**: `actions-manager`
3. Switch to the **Repository** tab (not Web editor)
4. **Repository URL**: `https://github.com/Wii-Chef-Channel/actions-manager.git`
5. **Branch**: `master`
6. Portainer will clone the repo and auto-load `docker-compose.yml`
7. Go to the **Environment variables** tab and add:

| Variable | Value | Required |
|----------|-------|----------|
| `GITHUB_PAT` | `ghp_your_token_here` | **Yes** |
| `ORG_NAME` | `Wii-Chef-Channel` | No (default) |
| `REFRESH_INTERVAL` | `30` | No (seconds) |
| `TIMEZONE` | `America/New_York` | No (default UTC) |
| `BASIC_AUTH_USER` | (leave empty) | No |
| `BASIC_AUTH_PASS` | (leave empty) | No |

8. **Deploy the stack**

### 3. Open the UI

```
http://<banaNAS-ip>:5000
```

### 4. Configure in the UI

Click **⚙ Config** to:
- Set/refresh your GitHub PAT
- Change the org name
- Adjust auto-refresh interval
- Set timezone
- **Enable the scheduler** (toggle button)

### 5. Set workflow frequencies

1. Select a repo from the sidebar
2. For each workflow, use the **Frequency** dropdown to pick a schedule
3. Toggle the **schedule toggle** (circle) to enable/disable scheduling
4. Click **Save** in the ⚙ Config panel

### How this works

Portainer's Repository tab:
1. Clones the git repo onto BanaNAS at `/var/lib/docker/volumes/portainer_portainer-data/_data/compose/<hash>/actions-manager`
2. Builds the Docker image from `Dockerfile`
3. Runs the container with your env vars

No SSH needed — Portainer handles everything from the web UI.

## Local Development

```bash
cd actions-manager
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

Set environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_PAT` | (required) | GitHub Personal Access Token with `repo` scope |
| `ORG_NAME` | `Wii-Chef-Channel` | GitHub org to scan |
| `REFRESH_INTERVAL` | `30` | Auto-refresh interval in seconds |
| `TIMEZONE` | `UTC` | Timezone for display |
| `BASIC_AUTH_USER` | (empty) | Optional basic auth username |
| `BASIC_AUTH_PASS` | (empty) | Optional basic auth password |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `5000` | Bind port |

## Docker

```bash
docker build -t actions-manager .
docker run -d -p 5000:5000 \
  -e GITHUB_PAT=ghp_your_token_here \
  -e ORG_NAME=Wii-Chef-Channel \
  -v am-config:/app/data \
  actions-manager
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/api/status` | Health check |
| GET | `/api/repos` | List all repos in the org |
| GET | `/api/repos/<name>/workflows` | Get workflows for a repo |
| GET | `/api/repos/<name>/workflows/<id>/last-run` | Get last run for a workflow |
| POST | `/api/repos/<name>/workflows/<id>/trigger` | Trigger a single workflow |
| POST | `/api/repos/trigger-selected` | Trigger all selected workflows |
| GET | `/api/config` | Get current config (PAT hidden) |
| POST | `/api/config` | Save config |
| GET | `/api/scheduler/status` | Get scheduler running status |
| POST | `/api/scheduler/status` | Enable/disable scheduler (`{"enabled": true}`) |

## Security Notes

- **PAT is stored in the `am-config` Docker volume** — never in git
- **Bound to `0.0.0.0:5000`** — use your reverse proxy for HTTPS
- Add `BASIC_AUTH_USER/PASS` for basic authentication
- The PAT is never exposed in API responses or the UI
- If `GITHUB_PAT` env var is not set, the app falls back to the PAT stored in `data/config.json`

## Updating

In Portainer:
1. Go to your stack → **Stack settings** (or the ⚙ gear icon)
2. Click **Recalculate configuration** — Portainer pulls latest commits
3. Click **Recreate** — Portainer rebuilds and restarts
4. Config is preserved in the `am-config` volume

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No workflows showing | Check PAT has `repo` scope and is valid |
| Scheduler not triggering | Verify scheduler is enabled in ⚙ Config |
| Workflow triggers but nothing happens | Check the workflow file exists in the target branch |
| Config not persisting | Ensure `am-config` volume is mounted correctly |
| Timezone wrong | Set `TIMEZONE` env var or in ⚙ Config |
| Sidebar selection lost on refresh | State persists via localStorage — clear browser cache if needed |
