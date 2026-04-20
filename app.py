import base64
import hmac
import re
import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_from_directory
import requests
from croniter import croniter

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
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
# Config path & helpers
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "data", "config.json"))
_config_lock = threading.Lock()
_cache_lock = threading.Lock()

def _ensure_config():
    """Auto-create config file on first run."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        default = {
            "org": ORG_NAME,
            "repos": {},
            "_scheduler": {
                "running": True,
                "last_triggers": {},
            },
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(default, f, indent=2)

def _load_config():
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
                    # Ensure _scheduler key exists with defaults
                    if "_scheduler" not in cfg:
                        cfg["_scheduler"] = {"running": True, "last_triggers": {}}
                    logger.info(f"CONFIG_LOAD: loaded from disk, _scheduler={cfg.get('_scheduler')}")
                    return cfg
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"CONFIG_LOAD: Failed to load config: {e}")
                return {"org": ORG_NAME, "repos": {}}
    logger.info(f"CONFIG_LOAD: file not found, returning defaults")
    return {"org": ORG_NAME, "repos": {}}

_ensure_config()

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
# Input validation
# ---------------------------------------------------------------------------
_GITHUB_NAME_RE = re.compile(r"^[a-zA-Z0-9_](?:[a-zA-Z0-9_-]*[a-zA-Z0-9])?$")
_GITHUB_ID_RE = re.compile(r"^\d+$")

def _validate_name(name, label="name"):
    """Validate a GitHub repo/user name."""
    if not name or not _GITHUB_NAME_RE.match(name):
        return f"Invalid {label}: {name!r}"
    return None

def _validate_id(val, label="id"):
    """Validate a GitHub numeric ID."""
    if not val or not _GITHUB_ID_RE.match(val):
        return f"Invalid {label}: {val!r}"
    return None

def _atomic_write(path, data):
    """Write JSON atomically (write to temp -> rename) to prevent corruption."""
    tmp_path = path + ".tmp"
    with _config_lock:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)

# ---------------------------------------------------------------------------
# GitHub PAT helper
# ---------------------------------------------------------------------------
_pat_cache = {"value": None, "ts": 0}
_PAT_TTL = 30  # seconds

def _get_pat():
    """Get PAT from env var or config file with short TTL cache."""
    pat = os.environ.get("GITHUB_PAT", "")
    if pat:
        return pat
    now = time.time()
    if _pat_cache["value"] and (now - _pat_cache["ts"]) < _PAT_TTL:
        return _pat_cache["value"]
    cfg = _load_config()
    _pat_cache["value"] = cfg.get("github_pat", "")
    _pat_cache["ts"] = now
    return _pat_cache["value"]

def _invalidate_pat_cache():
    """Invalidate PAT cache after config change."""
    _pat_cache["value"] = None
    _pat_cache["ts"] = 0

def _headers():
    return {"Authorization": f"token {_get_pat()}", "Accept": "application/vnd.github+json"}

def _get_org_name():
    """Get Org Name — always use env var as the authoritative source."""
    return os.environ.get("ORG_NAME", "Wii-Chef-Channel")

# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def _github_get(url, params=None):
    """GET with pagination and rate-limit awareness."""
    results = []
    page = 1
    query_params = params or {}
    while True:
        r = requests.get(url, headers=_headers(), params={**query_params, "per_page": 100, "page": page}, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("GitHub PAT is invalid or expired")
        if r.status_code == 403:
            raise RuntimeError("GitHub API rate limit exceeded. Retry in 60s")
        if r.status_code != 200:
            try:
                msg = r.json().get("message", r.text[:200])
            except:
                msg = r.text[:200]
            raise RuntimeError(f"GitHub API error {r.status_code}: {msg}")
        
        data = r.json()
        
        if isinstance(data, list):
            results.extend(data)
            link = r.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        else:
            return data
            
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
        if total_seconds < 0: return "in future"
        if total_seconds < 60: return f"{total_seconds}s ago"
        minutes = total_seconds // 60
        if minutes < 60: return f"{minutes}m ago" if minutes != 1 else "1m ago"
        hours = minutes // 60
        if hours < 24: return f"{hours}h ago" if hours != 1 else "1h ago"
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
    return jsonify({"status": "ok", "org": _get_org_name(), "pat_configured": bool(_get_pat())})

@app.route("/api/repos")
def get_repos():
    """List all org/user repos and their enabled status from config."""
    org = _get_org_name()
    repos_config = _load_config().get("repos", {})
    cache_key = f"repos:{org}"

    with _cache_lock:
        if cache_key in _cache and _cache[cache_key]["data"] and (time.time() - _cache[cache_key]["ts"]) < CACHE_TTL_REPOS:
            return jsonify(_cache[cache_key]["data"])

    try:
        try:
            repos = _github_get(f"https://api.github.com/orgs/{org}/repos", {"type": "all", "sort": "updated"})
        except RuntimeError:
            logger.info(f"Org {org!r} not found, trying user endpoint")
            repos = _github_get(f"https://api.github.com/users/{org}/repos", {"type": "all", "sort": "updated"})
    except RuntimeError as e:
        logger.error(f"Failed to fetch repos for {org!r}: {e}")
        return jsonify({"repos": [], "error": str(e)}), 502

    result = []
    for r in repos:
        if not isinstance(r, dict): continue
        key = r["name"]
        enabled = repos_config.get(key, {}).get("enabled", True)
        default_branch = r.get("default_branch", "main")
        workflows = (repos_config.get(key) or {}).get("workflows", {}) or {}
        schedule_enabled = any(
            isinstance(wf, dict) and wf.get("enabled_schedule", False)
            for wf in workflows.values()
        )
        result.append({
            "name": key,
            "full_name": r["full_name"],
            "default_branch": default_branch,
            "enabled": enabled,
            "schedule_enabled": schedule_enabled,
            "private": r.get("private", True),
            "updated_at": r.get("updated_at"),
        })

    result.sort(key=lambda x: (not x["enabled"], x["name"]))

    with _cache_lock:
        _cache[cache_key] = {"data": {"repos": result}, "ts": time.time()}
    return jsonify({"repos": result})

@app.route("/api/repos/<repo_name>/workflows")
def get_workflows(repo_name):
    """List workflows for a repo."""
    org = _get_org_name()
    cache_key = f"workflows:{org}:{repo_name}"

    with _cache_lock:
        if cache_key in _cache and _cache[cache_key]["data"] and (time.time() - _cache[cache_key]["ts"]) < CACHE_TTL_WORKFLOWS:
            return jsonify(_cache[cache_key]["data"])

    repos_config = _load_config().get("repos", {})
    repo_config = repos_config.get(repo_name, {})

    try:
        data = _github_get(f"https://api.github.com/repos/{org}/{repo_name}/actions/workflows")
        workflows = data.get("workflows", []) if isinstance(data, dict) else data
        result = []
        for w in workflows:
            wid = w["id"]
            wcfg = repo_config.get("workflows", {}).get(str(wid), {})
            cron_expr = wcfg.get("cron", wcfg.get("schedule", ""))
            result.append({
                "id": wid,
                "name": w["name"],
                "path": w["path"],
                "state": w["state"],
                "enabled_schedule": wcfg.get("enabled_schedule", False),
                "cron": cron_expr if cron_expr else "disabled",
                "branch": wcfg.get("branch", ""),
                "last_triggered": wcfg.get("last_triggered"),
                "last_run": None,
                "triggering": False,
                "trigger_error": None,
            })

        with _cache_lock:
            _cache[cache_key] = {"data": {"workflows": result}, "ts": time.time()}
        return jsonify({"workflows": result})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/last-run")
def get_last_run(repo_name, workflow_id):
    """Get the last run for a specific workflow."""
    org = _get_org_name()
    try:
        data = _github_get(
            f"https://api.github.com/repos/{org}/{repo_name}/actions/workflows/{workflow_id}/runs",
            {"per_page": 1},
        )
        runs = data.get("workflow_runs", []) if isinstance(data, dict) else data
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
    except Exception:
        pass
    return jsonify(None)

@app.route("/api/repos/<repo_name>/workflows/batch-last-run", methods=["POST"])
def get_batch_last_run(repo_name):
    """Get last run for multiple workflows in parallel."""
    org = _get_org_name()
    body = request.get_json()
    if body is None:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    workflow_ids = body.get("workflow_ids", [])
    if not workflow_ids:
        return jsonify({})
    for wid in workflow_ids:
        if not _GITHUB_ID_RE.match(str(wid)):
            return jsonify({"error": f"Invalid workflow_id: {wid!r}"}), 400

    def fetch_one(wid):
        try:
            data = _github_get(
                f"https://api.github.com/repos/{org}/{repo_name}/actions/workflows/{wid}/runs",
                {"per_page": 1},
            )
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else data
            if runs and isinstance(runs, list) and len(runs) > 0:
                r = runs[0]
                return str(wid), {
                    "id": r["id"],
                    "status": r["status"],
                    "conclusion": r.get("conclusion"),
                    "created_at": r["created_at"],
                    "display_time": _format_time(r["created_at"]),
                    "relative_time": _relative_time(r["created_at"]),
                    "event": r.get("event"),
                    "display_url": r.get("html_url", ""),
                }
        except Exception:
            pass
        return str(wid), None

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = dict(executor.map(fetch_one, workflow_ids))

    return jsonify(results)

@app.route("/api/repos/<repo_name>/branches")
def get_branches(repo_name):
    """List branches for a repo."""
    org = _get_org_name()
    err = _validate_name(repo_name, "repo")
    if err: return jsonify({"error": err}), 400

    try:
        data = _github_get(f"https://api.github.com/repos/{org}/{repo_name}/branches")
        branches = [b["name"] for b in data] if isinstance(data, list) else []
        return jsonify({"branches": branches})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502

@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/trigger", methods=["POST"])
def trigger_workflow(repo_name, workflow_id):
    """Trigger a workflow_dispatch on a workflow."""
    org = _get_org_name()
    # Validate inputs
    err = _validate_name(repo_name, "repo")
    if err: return jsonify({"error": err}), 400
    err = _validate_id(workflow_id, "workflow_id")
    if err: return jsonify({"error": err}), 400
    body = request.get_json() or {}
    branch = body.get("branch", "")
    if branch and not re.match(r"^[a-zA-Z0-9_./-]+$", branch):
        return jsonify({"error": "Invalid branch name"}), 400

    try:
        result = _github_post(
            f"https://api.github.com/repos/{org}/{repo_name}/actions/workflows/{workflow_id}/dispatches",
            {"ref": branch or "main"},
        )
        # Update last_triggered in config and in-memory state
        now = datetime.now(timezone.utc)
        wf_id_str = str(workflow_id)
        with _scheduler_lock:
            _scheduler_state["last_triggers"][f"{repo_name}:{wf_id_str}"] = now.timestamp()
        cfg = _load_config()
        repos = cfg.get("repos") or {}
        repo_cfg = repos.get(repo_name, {})
        workflows = repo_cfg.get("workflows", {})
        if wf_id_str not in workflows:
            workflows[wf_id_str] = {}
        workflows[wf_id_str]["last_triggered"] = now.timestamp()
        repo_cfg["workflows"] = workflows
        repos[repo_name] = repo_cfg
        cfg["repos"] = repos
        _atomic_write(CONFIG_PATH, cfg)
        # Invalidate workflow cache so last_triggered is fresh on next fetch
        with _cache_lock:
            _cache["workflows"][repo_name] = {"data": None, "ts": 0}
        return jsonify({"success": True, "message": "Triggered", "run_url": result.get("html_url", "")})
    except RuntimeError as e:
        return jsonify({"success": False, "message": str(e)}), 502

@app.route("/api/trigger-selected", methods=["POST"])
def trigger_selected():
    """Trigger multiple workflows at once."""
    org = _get_org_name()
    body = request.get_json()
    if body is None:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    items = body.get("items", [])
    results = []
    for item in items:
        repo = item.get("repo", "")
        wf_id = str(item.get("workflow_id", ""))
        branch = item.get("branch", "main")
        err = _validate_name(repo, "repo")
        if err:
            results.append({"repo": repo, "name": item.get("name", ""), "success": False, "message": err})
            continue
        err = _validate_id(wf_id, "workflow_id")
        if err:
            results.append({"repo": repo, "name": item.get("name", ""), "success": False, "message": err})
            continue
        try:
            result = _github_post(
                f"https://api.github.com/repos/{org}/{repo}/actions/workflows/{wf_id}/dispatches",
                {"ref": branch},
            )
            # Update last_triggered in config and in-memory state
            now = datetime.now(timezone.utc)
            with _scheduler_lock:
                _scheduler_state["last_triggers"][f"{repo}:{wf_id}"] = now.timestamp()
            cfg = _load_config()
            repos = cfg.get("repos") or {}
            repo_cfg = repos.get(repo, {})
            workflows = repo_cfg.get("workflows", {})
            if wf_id not in workflows:
                workflows[wf_id] = {}
            workflows[wf_id]["last_triggered"] = now.timestamp()
            repo_cfg["workflows"] = workflows
            repos[repo] = repo_cfg
            cfg["repos"] = repos
            _atomic_write(CONFIG_PATH, cfg)
            # Invalidate workflow cache so last_triggered is fresh on next fetch
            with _cache_lock:
                _cache["workflows"][repo] = {"data": None, "ts": 0}
            results.append({"repo": repo, "name": item.get("name", ""), "success": True, "message": "Triggered"})
        except RuntimeError as e:
            results.append({"repo": repo, "name": item.get("name", ""), "success": False, "message": str(e)})
    return jsonify({"results": results})

@app.route("/api/config", methods=["GET", "POST"])
def config():
    """Get or set config."""
    cfg = _load_config()

    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400
        try:
            if data.get("github_pat"):
                new_pat = data["github_pat"]
                r = requests.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"token {new_pat}", "Accept": "application/vnd.github+json"},
                    timeout=10,
                )
                if r.status_code != 200:
                    return jsonify({"error": "Invalid PAT"}), 400

            full_config = cfg
            if data.get("github_pat"): full_config["github_pat"] = data["github_pat"]
            # org is always determined by ORG_NAME env var — never save it from config
            if data.get("timezone"): full_config["timezone"] = data["timezone"]
            # Preserve _scheduler section (scheduler loop writes running/last_triggers)
            saved_scheduler = cfg.get("_scheduler")
            logger.info(f"CONFIG_SAVE: loaded _scheduler={saved_scheduler}")
            # Merge repos config instead of replacing — prevents wiping repos
            # that weren't included in a partial update
            if data.get("repos"):
                if not isinstance(full_config.get("repos"), dict):
                    full_config["repos"] = {}
                for rname, rcfg in data["repos"].items():
                    if not isinstance(full_config["repos"].get(rname), dict):
                        full_config["repos"][rname] = {}
                    if isinstance(rcfg, dict):
                        full_config["repos"][rname].update(rcfg)
            # Restore _scheduler section if it was present
            if saved_scheduler:
                full_config["_scheduler"] = saved_scheduler
            logger.info(f"CONFIG_SAVE: writing _scheduler={full_config.get('_scheduler')}")
            _atomic_write(CONFIG_PATH, full_config)

            with _cache_lock:
                _cache.clear()
                _cache["repos"] = {"data": None, "ts": 0}
                _cache["workflows"] = {}
            _invalidate_pat_cache()
            return jsonify({"message": "Config saved"})
        except Exception as e:
            logger.exception(f"Config save error: {e}")
            return jsonify({"error": str(e)}), 500

    pat_set = cfg.get("github_pat") or os.environ.get("GITHUB_PAT", "")
    return jsonify({
        "org": _get_org_name(),
        "refresh_interval": REFRESH_INTERVAL,
        "timezone": TIMEZONE,
        "pat_configured": bool(pat_set),
        "repos": cfg.get("repos", {}),
    })

@app.route("/api/scheduler/status", methods=["GET", "POST"])
def scheduler_status():
    """Get or set scheduler state."""
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400
        enabled = data.get("enabled", data.get("running"))
        logger.info(f"SCHEDULER POST: enabled={enabled}, current_in_memory={_scheduler_state.get('running')}")
        try:
            if enabled is True:
                # User explicitly enabled — force start regardless of disk state
                _scheduler_state["running"] = True
                _scheduler_state["thread"] = None  # Clear so _start_scheduler knows to create new thread
                logger.info("SCHEDULER: calling _start_scheduler() (forced by user)")
                _start_scheduler()
                logger.info(f"SCHEDULER: after _start_scheduler, running={_scheduler_state.get('running')}, thread={_scheduler_state.get('thread')}")
            elif enabled is False:
                # User explicitly disabled — stop and persist
                logger.info("SCHEDULER: calling _stop_scheduler()")
                _stop_scheduler()
                logger.info(f"SCHEDULER: after _stop_scheduler, running={_scheduler_state.get('running')}, thread={_scheduler_state.get('thread')}")
            else:
                return jsonify({"error": "Invalid enabled value"}), 400
        except RuntimeError as e:
            logger.error(f"Scheduler error: {e}")
            return jsonify({"error": str(e)}), 500
        except Exception as e:
            logger.exception(f"Unexpected scheduler error: {e}")
            return jsonify({"error": "Scheduler error: " + str(e)}), 500
    # GET: read from disk to be consistent across gunicorn workers
    try:
        cfg = _load_config()
        disk_running = (cfg.get("_scheduler") or {}).get("running", None)
        running = disk_running is not False
        logger.info(f"SCHEDULER GET: returning running={running} (from disk: {disk_running})")
        return jsonify({"running": running})
    except Exception as e:
        logger.error(f"Scheduler GET error: {e}")
        return jsonify({"running": False})

@app.route("/api/repos/<repo_name>/workflow-config", methods=["POST"])
def save_workflow_config(repo_name):
    """Save frequency and branch settings for workflows in a repo."""
    err = _validate_name(repo_name, "repo")
    if err:
        return jsonify({"error": err}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    workflows_cfg = data.get("workflows", {})
    if not workflows_cfg:
        return jsonify({"error": "No workflows config provided"}), 400

    cfg = _load_config()
    if repo_name not in cfg.get("repos", {}):
        return jsonify({"error": f"Repo '{repo_name}' not found in config"}), 404

    if "repos" not in cfg:
        cfg["repos"] = {}
    if repo_name not in cfg["repos"]:
        cfg["repos"][repo_name] = {}
    if "workflows" not in cfg["repos"][repo_name]:
        cfg["repos"][repo_name]["workflows"] = {}

    for wf_id, wf_settings in workflows_cfg.items():
        if not _GITHUB_ID_RE.match(str(wf_id)):
            continue
        if "cron" in wf_settings:
            cfg["repos"][repo_name]["workflows"][str(wf_id)] = cfg["repos"][repo_name]["workflows"].get(str(wf_id), {})
            cfg["repos"][repo_name]["workflows"][str(wf_id)]["cron"] = wf_settings["cron"]
        if "branch" in wf_settings:
            if str(wf_id) not in cfg["repos"][repo_name]["workflows"]:
                cfg["repos"][repo_name]["workflows"][str(wf_id)] = {}
            cfg["repos"][repo_name]["workflows"][str(wf_id)]["branch"] = wf_settings["branch"]
        if "enabled" in wf_settings:
            if str(wf_id) not in cfg["repos"][repo_name]["workflows"]:
                cfg["repos"][repo_name]["workflows"][str(wf_id)] = {}
            cfg["repos"][repo_name]["workflows"][str(wf_id)]["enabled_schedule"] = wf_settings["enabled"]

    _atomic_write(CONFIG_PATH, cfg)

    # Invalidate cache
    with _cache_lock:
        _cache["workflows"][repo_name] = {"data": None, "ts": 0}

    return jsonify({"message": "Config saved"})

@app.before_request
def enforce_auth():
    """Enforce Basic Auth if credentials are configured."""
    if BASIC_AUTH_USER and BASIC_AUTH_PASS:
        auth = request.headers.get("Authorization")
        if not auth or not _check_basic_auth(auth):
            return jsonify({"error": "Authentication required"}), 401

@app.before_request
def enforce_content_type():
    """Require application/json on all mutating endpoints."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        ct = request.content_type or ""
        if "application/json" not in ct:
            return jsonify({"error": "Content-Type must be application/json"}), 415

def _check_basic_auth(auth_header):
    """Validate Basic Auth header with constant-time comparison."""
    try:
        scheme, credentials = auth_header.split(None, 1)
        if scheme.lower() != "basic": return False
        decoded = base64.b64decode(credentials).decode("utf-8")
        user, password = decoded.split(":", 1)
        user_ok = hmac.compare_digest(user, BASIC_AUTH_USER)
        pass_ok = hmac.compare_digest(password, BASIC_AUTH_PASS)
        return user_ok and pass_ok
    except Exception: return False

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
_scheduler_state = {
    "running": False,
    "thread": None,
    "last_triggers": {},
}
_scheduler_lock = threading.Lock()
_scheduler_config = {"interval": 60}

def _get_last_trigger(key):
    with _scheduler_lock:
        return _scheduler_state["last_triggers"].get(key)

def _set_last_trigger(key, ts):
    with _scheduler_lock:
        _scheduler_state["last_triggers"][key] = ts

def _scheduler_load_config():
    """Reload config from disk each tick to stay in sync with API changes."""
    return _load_config()


def _scheduler_loop():
    """Background thread: check enabled workflows every 60s and trigger if due."""
    logger.info("Scheduler started")
    while _scheduler_state["running"]:
        try:
            config = _scheduler_load_config()
            repos_config = config.get("repos", {}) or {}
            org = config.get("org") or ORG_NAME
            try:
                tz = ZoneInfo(config.get("timezone", "UTC"))
            except Exception:
                tz = ZoneInfo("UTC")
            now = datetime.now(tz)

            triggered_any = False
            if not repos_config:
                repos_config = {}
            for repo_name, repo_cfg in repos_config.items():
                if not isinstance(repo_cfg, dict): continue
                if not repo_cfg.get("enabled", False): continue
                workflows = repo_cfg.get("workflows", {}) or {}
                for wf_id, wf_cfg in workflows.items():
                    if not isinstance(wf_cfg, dict): continue
                    if not wf_cfg.get("enabled_schedule", False): continue
                    cron_expr = wf_cfg.get("cron") or wf_cfg.get("schedule") or ""
                    if not cron_expr or cron_expr == "disabled": continue
                    key = f"{repo_name}:{wf_id}"
                    last = _get_last_trigger(key)
                    prev_trigger = croniter(cron_expr, now).get_prev(datetime)
                    is_due = 0 <= (now - prev_trigger).total_seconds() < 60

                    if is_due:
                        if last and (now.timestamp() - last) < 120: continue
                        branch = wf_cfg.get("branch") or "main"
                        try:
                            _github_post(
                                f"https://api.github.com/repos/{org}/{repo_name}/actions/workflows/{wf_id}/dispatches",
                                {"ref": branch},
                            )
                            _set_last_trigger(key, now.timestamp())
                            if repo_name not in config["repos"]: config["repos"][repo_name] = {}
                            if "workflows" not in config["repos"][repo_name]: config["repos"][repo_name]["workflows"] = {}
                            if str(wf_id) not in config["repos"][repo_name]["workflows"]: config["repos"][repo_name]["workflows"][str(wf_id)] = {}
                            config["repos"][repo_name]["workflows"][str(wf_id)]["last_triggered"] = now.timestamp()
                            triggered_any = True
                            logger.info(f"Scheduled trigger: {repo_name}/{wf_id} at {now.isoformat()}")
                        except RuntimeError as e:
                            logger.error(f"Scheduler failed {repo_name}/{wf_id}: {e}")

            # Always persist last_triggers (even if nothing triggered) so
            # the value survives restarts
            config.setdefault("_scheduler", {})["last_triggers"] = dict(_scheduler_state["last_triggers"])
            config.setdefault("_scheduler", {})["running"] = _scheduler_state["running"]
            _atomic_write(CONFIG_PATH, config)

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        for _ in range(_scheduler_config["interval"]):
            if not _scheduler_state["running"]: break
            time.sleep(1)
    logger.info("Scheduler stopped")


def _start_scheduler():
    """Start the scheduler loop. Defaults to running unless explicitly off in config."""
    logger.info(f"SCHEDULER_START: thread={_scheduler_state.get('thread')}, current_running={_scheduler_state.get('running')}")
    try:
        cfg = _load_config()
        logger.info(f"SCHEDULER_START: cfg loaded, _scheduler={cfg.get('_scheduler')}")
    except Exception as e:
        logger.error(f"Failed to load config for scheduler start: {e}")
        raise RuntimeError(f"Config error: {e}")

    # Restore last_triggers from disk
    disk_triggers = {}
    try:
        disk_triggers = cfg.get("_scheduler", {}).get("last_triggers", {}) or {}
    except Exception:
        pass
    if disk_triggers:
        with _scheduler_lock:
            _scheduler_state["last_triggers"].update(disk_triggers)

    # Determine running state:
    # - If a thread already exists, scheduler was already running, keep it running
    # - If no thread exists (fresh start or after stop):
    #   - If in-memory running is True, user just enabled it — start it
    #   - If in-memory running is False, check disk for saved state
    if _scheduler_state.get("thread") is not None:
        # Thread exists — scheduler was already running, keep it running
        logger.info("SCHEDULER_START: thread exists, keeping running=True")
        _scheduler_state["running"] = True
    else:
        # No thread — fresh start or was stopped.
        if _scheduler_state.get("running") is True:
            # In-memory is True (user just enabled or fresh start) — start it
            logger.info("SCHEDULER_START: in-memory=True, starting scheduler")
            _scheduler_state["running"] = True
        else:
            # In-memory is False — check disk for saved state
            saved_running = (cfg.get("_scheduler") or {}).get("running", None)
            logger.info(f"SCHEDULER_START: in-memory=False, disk_scheduled_running={saved_running}")
            _scheduler_state["running"] = saved_running is not False

    # Persist the determined state to disk
    cfg.setdefault("_scheduler", {})["running"] = _scheduler_state["running"]
    cfg.setdefault("_scheduler", {})["last_triggers"] = dict(_scheduler_state["last_triggers"])
    try:
        _atomic_write(CONFIG_PATH, cfg)
        logger.info(f"SCHEDULER_START: persisted running={_scheduler_state['running']} to disk")
    except Exception as e:
        logger.error(f"Failed to persist scheduler state: {e}")

    if _scheduler_state["running"]:
        _scheduler_state["thread"] = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_state["thread"].start()
        logger.info("SCHEDULER_START: thread started")
    else:
        logger.info("SCHEDULER_START: not starting (running=False)")


def _stop_scheduler():
    """Stop the scheduler and persist state to disk."""
    logger.info("SCHEDULER_STOP: stopping scheduler")
    _scheduler_state["running"] = False
    try:
        cfg = _load_config()
        cfg.setdefault("_scheduler", {})["running"] = False
        cfg.setdefault("_scheduler", {})["last_triggers"] = dict(_scheduler_state["last_triggers"])
        _atomic_write(CONFIG_PATH, cfg)
        logger.info("SCHEDULER_STOP: persisted running=False to disk")
    except Exception as e:
        logger.error(f"Failed to persist scheduler state: {e}")
    if _scheduler_state["thread"]:
        _scheduler_state["thread"].join(timeout=10)
        _scheduler_state["thread"] = None
        logger.info("SCHEDULER_STOP: thread joined")

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
