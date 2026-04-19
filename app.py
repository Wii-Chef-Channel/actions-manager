import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
import requests
from croniter import croniter

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
ORG_NAME = os.environ.get("ORG_NAME", "Wii-Chef-Channel")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "30"))
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("actions-manager")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=".", static_url_path="/")

# ---------------------------------------------------------------------------
# Simple cache
# ---------------------------------------------------------------------------
_cache = {
    "repos": {"data": None, "ts": 0},
    "workflows": {},
}
CACHE_TTL_REPOS = 300   # 5 min
CACHE_TTL_WORKFLOWS = 60 # 1 min

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def _get_pat():
    """Get PAT from env var or config file."""
    pat = os.environ.get("GITHUB_PAT", "")
    if pat:
        return pat
    cfg = _load_config()
    return cfg.get("github_pat", "")

def _headers():
    return {"Authorization": f"token {_get_pat()}", "Accept": "application/vnd.github+json"}

def _github_get(url, params=None):
    """GET with pagination and rate-limit awareness."""
    results = []
    page = 1
    while True:
        r = requests.get(url, headers=_headers(), params={**params, "per_page": 100, "page": page}, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("GitHub PAT is invalid or expired")
        if r.status_code == 403:
            raise RuntimeError(f"GitHub API rate limit exceeded. Retry in 60s")
        if r.status_code != 200:
            raise RuntimeError(f"GitHub API error {r.status_code}: {r.text[:200]}")
        data = r.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        # Check if there are more pages
        link = r.headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        page += 1
    return results

def _github_post(url, body=None):
    r = requests.post(url, json=body or {}, headers=_headers(), timeout=15)
    if r.status_code == 401:
        raise RuntimeError("GitHub PAT is invalid or expired")
    if r.status_code == 403:
        raise RuntimeError(f"GitHub API rate limit exceeded. Retry in 60s")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text[:200]}")
    if r.status_code == 204:
        return {"message": "success"}
    return r.json()

# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------
def _format_time(utc_str):
    """Convert ISO UTC string to local time string."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str

def _relative_time(utc_str):
    """Return relative time string like '2m ago', '3h ago'."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "in future"
        if total_seconds < 60:
            return f"{total_seconds}s ago"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m ago" if minutes != 1 else "1m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago" if hours != 1 else "1h ago"
        days = hours // 24
        return f"{days}d ago"
    except Exception:
        return utc_str

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "org": ORG_NAME, "pat_configured": bool(GITHUB_PAT)})

@app.route("/api/repos")
def get_repos():
    """List all org repos and their enabled status from config."""
    repos_config = _load_config().get("repos", {})
    cache_key = "repos"

    if _cache[cache_key]["data"] and (time.time() - _cache[cache_key]["ts"]) < CACHE_TTL_REPOS:
        return jsonify(_cache[cache_key]["data"])

    repos = _github_get(f"https://api.github.com/orgs/{ORG_NAME}/repos", {"type": "all", "sort": "updated"})
    # Filter to only repos that have workflows
    result = []
    for r in repos:
        key = r["name"]
        enabled = repos_config.get(key, {}).get("enabled", True)
        default_branch = r.get("default_branch", "main")
        result.append({
            "name": key,
            "full_name": r["full_name"],
            "default_branch": default_branch,
            "enabled": enabled,
            "private": r.get("private", True),
            "updated_at": r.get("updated_at"),
        })

    # Sort: enabled first, then by name
    result.sort(key=lambda x: (not x["enabled"], x["name"]))

    _cache[cache_key] = {"data": result, "ts": time.time()}
    return jsonify({"repos": result})

@app.route("/api/repos/<repo_name>/workflows")
def get_workflows(repo_name):
    """List workflows for a repo."""
    cache_key = f"workflows:{repo_name}"

    if _cache[cache_key]["data"] and (time.time() - _cache[cache_key]["ts"]) < CACHE_TTL_WORKFLOWS:
        return jsonify(_cache[cache_key]["data"])

    repos_config = _load_config().get("repos", {})
    repo_config = repos_config.get(repo_name, {})

    workflows = _github_get(f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows")
    result = []
    for w in workflows:
        wid = w["id"]
        wcfg = repo_config.get("workflows", {}).get(str(wid), {})
        # Read cron from config (set by UI) or fall back to old 'schedule' key
        cron_expr = wcfg.get("cron", wcfg.get("schedule", ""))
        result.append({
            "id": wid,
            "name": w["name"],
            "path": w["path"],
            "state": w["state"],  # active or disabled_manually
            "enabled_schedule": wcfg.get("enabled_schedule", False),
            "cron": cron_expr if cron_expr else "disabled",
            "branch": wcfg.get("branch", ""),
            "last_triggered": wcfg.get("last_triggered"),
            "last_run": None,
            "triggering": False,
            "trigger_error": None,
        })

    _cache[cache_key] = {"data": result, "ts": time.time()}
    return jsonify({"workflows": result})

@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/last-run")
def get_last_run(repo_name, workflow_id):
    """Get the last run for a workflow."""
    runs = _github_get(
        f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/runs",
        {"head_branch": "", "event": "schedule", "per_page": 1},
    )
    if runs and isinstance(runs, list) and len(runs) > 0:
        r = runs[0]
        return jsonify({
            "id": r["id"],
            "status": r["status"],
            "conclusion": r.get("conclusion"),
            "created_at": r["created_at"],
            "display_time": _format_time(r["created_at"]),
            "relative_time": _relative_time(r["created_at"]),
            "event": r.get("event"),
            "display_url": r.get("html_url", ""),
        })
    return jsonify(None)

@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/trigger", methods=["POST"])
def trigger_workflow(repo_name, workflow_id):
    """Trigger a workflow_dispatch on a workflow."""
    body = request.get_json() or {}
    branch = body.get("branch", "")

    try:
        result = _github_post(
            f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows/{workflow_id}/dispatches",
            {"ref": branch or "main"},
        )
        return jsonify({"success": True, "message": "Triggered", "run_url": result.get("html_url", "")})
    except RuntimeError as e:
        return jsonify({"success": False, "message": str(e)}), 502

@app.route("/api/trigger-selected", methods=["POST"])
def trigger_selected():
    """Trigger multiple workflows at once."""
    items = request.get_json().get("items", [])
    results = []
    for item in items:
        try:
            result = _github_post(
                f"https://api.github.com/repos/{ORG_NAME}/{item['repo']}/actions/workflows/{item['workflow_id']}/dispatches",
                {"ref": item.get("branch", "main")},
            )
            results.append({"repo": item["repo"], "workflow": item["name"], "success": True, "message": "Triggered"})
        except RuntimeError as e:
            results.append({"repo": item["repo"], "workflow": item["name"], "success": False, "message": str(e)})
    return jsonify({"results": results})

@app.route("/api/config", methods=["GET", "POST"])
def config():
    """Get or set config."""
    cfg = _load_config()

    if request.method == "POST":
        data = request.get_json()
        # Validate PAT
        if data.get("github_pat"):
            r = requests.get("https://api.github.com/user", headers=_headers(), timeout=10)
            if r.status_code == 401:
                return jsonify({"error": "Invalid PAT"}), 400
        # Save config
        full_config = cfg
        if data.get("github_pat"):
            full_config["github_pat"] = data["github_pat"]
        if data.get("org"):
            full_config["org"] = data["org"]
        if data.get("repos"):
            full_config["repos"] = data["repos"]
        with open(CONFIG_PATH, "w") as f:
            json.dump(full_config, f, indent=2)
        # Invalidate cache
        _cache["repos"] = {"data": None, "ts": 0}
        _cache["workflows"] = {}
        # Restart scheduler with new config
        _stop_scheduler()
        _start_scheduler()
        return jsonify({"message": "Config saved"})

    # Return config without PAT for display
    pat_set = cfg.get("github_pat") or os.environ.get("GITHUB_PAT", "")
    return jsonify({
        "org": cfg.get("org", ORG_NAME),
        "refresh_interval": REFRESH_INTERVAL,
        "timezone": TIMEZONE,
        "pat_configured": bool(pat_set),
        "repos": cfg.get("repos", {}),
    })

@app.route("/api/scheduler/status", methods=["GET", "POST"])
def scheduler_status():
    """Get or set scheduler state."""
    if request.method == "POST":
        data = request.get_json()
        if data.get("enabled"):
            _start_scheduler()
        else:
            _stop_scheduler()
        return jsonify({"running": _scheduler_state["running"]})
    return jsonify({"running": _scheduler_state["running"]})

@app.before_request
def remove_pat_from_responses():
    """Never leak PAT in API responses."""
    pass

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
_scheduler_state = {
    "running": False,
    "thread": None,
    "last_triggers": {},  # key: "repo:workflow_id" -> timestamp
}
_scheduler_lock = threading.Lock()
_scheduler_config = {"interval": 60}  # check every 60 seconds

def _get_last_trigger(key):
    with _scheduler_lock:
        return _scheduler_state["last_triggers"].get(key)

def _set_last_trigger(key, ts):
    with _scheduler_lock:
        _scheduler_state["last_triggers"][key] = ts

def _scheduler_loop():
    """Background thread: check enabled workflows every 60s and trigger if due."""
    logger.info("Scheduler started")
    while _scheduler_state["running"]:
        try:
            config = _load_config()
            repos_config = config.get("repos", {})
            now = datetime.now(timezone.utc)

            for repo_name, repo_cfg in repos_config.items():
                if not repo_cfg.get("enabled", False):
                    continue

                workflows = repo_cfg.get("workflows", {})
                for wf_id, wf_cfg in workflows.items():
                    if not wf_cfg.get("enabled_schedule", False):
                        continue

                    cron_expr = wf_cfg.get("cron", wf_cfg.get("schedule", ""))
                    if not cron_expr or cron_expr == "disabled":
                        continue

                    key = f"{repo_name}:{wf_id}"
                    last = _get_last_trigger(key)

                    # Check if this cron expression is due right now
                    next_trigger = croniter(cron_expr, now).get_next(datetime)
                    prev_trigger = croniter(cron_expr, now).get_prev(datetime)
                    is_due = prev_trigger.replace(second=0, microsecond=0) <= now <= next_trigger.replace(second=0, microsecond=0)

                    if is_due:
                        if last and (now.timestamp() - last) < 120:
                            continue  # triggered within last 2 min, skip

                        branch = wf_cfg.get("branch", "main")
                        try:
                            _github_post(
                                f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows/{wf_id}/dispatches",
                                {"ref": branch},
                            )
                            _set_last_trigger(key, now.timestamp())
                            
                            # Update config with last_triggered timestamp
                            config = _load_config()
                            if repo_name not in config.get("repos", {}):
                                config["repos"][repo_name] = {}
                            if "workflows" not in config["repos"][repo_name]:
                                config["repos"][repo_name]["workflows"] = {}
                            if str(wf_id) not in config["repos"][repo_name]["workflows"]:
                                config["repos"][repo_name]["workflows"][str(wf_id)] = {}
                            config["repos"][repo_name]["workflows"][str(wf_id)]["last_triggered"] = now.timestamp()
                            with open(CONFIG_PATH, "w") as f:
                                json.dump(config, f, indent=2)
                            
                            logger.info(f"Scheduled trigger: {repo_name}/{wf_id} at {now.isoformat()}")
                        except RuntimeError as e:
                            logger.error(f"Scheduler failed {repo_name}/{wf_id}: {e}")
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        # Sleep in small increments so we can stop quickly
        for _ in range(_scheduler_config["interval"]):
            if not _scheduler_state["running"]:
                break
            time.sleep(1)

    logger.info("Scheduler stopped")

def _start_scheduler():
    if not _scheduler_state["running"]:
        _scheduler_state["running"] = True
        _scheduler_state["thread"] = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_state["thread"].start()

def _stop_scheduler():
    _scheduler_state["running"] = False
    if _scheduler_state["thread"]:
        _scheduler_state["thread"].join(timeout=10)
        _scheduler_state["thread"] = None

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "data", "config.json")

def _ensure_config():
    """Auto-create config file on first run."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        default = {
            "org": ORG_NAME,
            "repos": {},
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(default, f, indent=2)

_ensure_config()

def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Starting actions-manager on {host}:{port}")
    _start_scheduler()
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        _stop_scheduler()
