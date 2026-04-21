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
# Scheduler state — defined here so routes can reference it safely
# ---------------------------------------------------------------------------
_scheduler_state = {
    "running": False,
    "thread": None,
    "last_triggers": {},
}
_scheduler_lock = threading.Lock()
_scheduler_config = {"interval": 60}

# ---------------------------------------------------------------------------
# Config path & helpers
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "data", "config.json"),
)
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
                if "_scheduler" not in cfg:
                    cfg["_scheduler"] = {"running": True, "last_triggers": {}}
                return cfg
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Failed to load config: {e}")
                return {"org": ORG_NAME, "repos": {}}
    return {"org": ORG_NAME, "repos": {}}


def _atomic_write(path, data):
    """Write JSON atomically (write to temp -> rename) to prevent corruption."""
    tmp_path = path + ".tmp"
    with _config_lock:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)


def _deep_merge(base, overlay):
    """Recursively merge overlay into base (in-place). Returns base."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


_ensure_config()

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=".", static_url_path="/")

# ---------------------------------------------------------------------------
# Simple cache
# ---------------------------------------------------------------------------
_cache = {}
CACHE_TTL_REPOS = 300    # 5 min
CACHE_TTL_WORKFLOWS = 60  # 1 min


def _cache_get(key):
    entry = _cache.get(key)
    return entry if entry and entry.get("data") is not None else None


def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}


def _cache_fresh(key, ttl):
    entry = _cache.get(key)
    return bool(entry and entry.get("data") is not None and (time.time() - entry["ts"]) < ttl)


def _cache_invalidate(key):
    _cache.pop(key, None)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
_GITHUB_NAME_RE = re.compile(r"^[a-zA-Z0-9_](?:[a-zA-Z0-9_.-]*[a-zA-Z0-9])?$")
_GITHUB_ID_RE = re.compile(r"^\d+$")


def _validate_name(name, label="name"):
    if not name or not _GITHUB_NAME_RE.match(name):
        return f"Invalid {label}: {name!r}"
    return None


def _validate_id(val, label="id"):
    if not val or not _GITHUB_ID_RE.match(str(val)):
        return f"Invalid {label}: {val!r}"
    return None


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
    _pat_cache["value"] = None
    _pat_cache["ts"] = 0


def _headers():
    return {"Authorization": f"token {_get_pat()}", "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
_session = requests.Session()

def _github_get(url, params=None):
    """GET with pagination and rate-limit awareness."""
    results = []
    page = 1
    query_params = params or {}
    while True:
        r = _session.get(
            url,
            headers=_headers(),
            params={**query_params, "per_page": 100, "page": page},
            timeout=15,
        )
        if r.status_code == 401:
            raise RuntimeError("GitHub PAT is invalid or expired")
        if r.status_code == 403:
            raise RuntimeError("GitHub API rate limit exceeded. Retry in 60s")
        if r.status_code != 200:
            try:
                msg = r.json().get("message", r.text[:200])
            except Exception:
                msg = r.text[:200]
            raise RuntimeError(f"GitHub API error {r.status_code}: {msg}")

        data = r.json()
        if isinstance(data, list):
            results.extend(data)
            if 'rel="next"' not in r.headers.get("Link", ""):
                break
            page += 1
        else:
            return data
    return results


def _github_post(url, body=None):
    r = _session.post(url, json=body or {}, headers=_headers(), timeout=15)
    if r.status_code == 401:
        raise RuntimeError("GitHub PAT is invalid or expired")
    if r.status_code == 403:
        raise RuntimeError("GitHub API rate limit exceeded. Retry in 60s")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text[:200]}")
    if r.status_code == 204:
        return {"message": "success"}
    return r.json()


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------
def _format_time(utc_str):
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str


def _relative_time(utc_str):
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        total_seconds = int((now - dt).total_seconds())
        if total_seconds < 0:
            return "in future"
        if total_seconds < 60:
            return f"{total_seconds}s ago"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return utc_str


def _run_dict(r):
    """Normalise a GitHub workflow run object to our API shape."""
    return {
        "id": r["id"],
        "status": r["status"],
        "conclusion": r.get("conclusion"),
        "created_at": r["created_at"],
        "display_time": _format_time(r["created_at"]),
        "relative_time": _relative_time(r["created_at"]),
        "event": r.get("event"),
        "display_url": r.get("html_url", ""),
    }


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
# Before-request hooks
# ---------------------------------------------------------------------------
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
    try:
        scheme, credentials = auth_header.split(None, 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(credentials).decode("utf-8")
        user, password = decoded.split(":", 1)
        return hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(
            password, BASIC_AUTH_PASS
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "org": ORG_NAME, "pat_configured": bool(_get_pat())})


@app.route("/api/repos")
def get_repos():
    """List all org/user repos and their enabled status from config."""
    repos_config = _load_config().get("repos", {})
    cache_key = f"repos:{ORG_NAME}"

    with _cache_lock:
        if _cache_fresh(cache_key, CACHE_TTL_REPOS):
            return jsonify(_cache_get(cache_key)["data"])

    try:
        try:
            repos = _github_get(
                f"https://api.github.com/orgs/{ORG_NAME}/repos",
                {"type": "all", "sort": "updated"},
            )
        except RuntimeError:
            logger.info(f"Org {ORG_NAME!r} not found, trying user endpoint")
            repos = _github_get(
                f"https://api.github.com/users/{ORG_NAME}/repos",
                {"type": "all", "sort": "updated"},
            )
    except RuntimeError as e:
        logger.error(f"Failed to fetch repos for {ORG_NAME!r}: {e}")
        return jsonify({"repos": [], "error": str(e)}), 502

    result = []
    for r in repos:
        if not isinstance(r, dict):
            continue
        key = r["name"]
        repo_cfg = repos_config.get(key) or {}
        enabled = repo_cfg.get("enabled", True)
        workflows = repo_cfg.get("workflows") or {}
        schedule_enabled = any(
            isinstance(wf, dict) and wf.get("enabled_schedule", False)
            for wf in workflows.values()
        )
        result.append({
            "name": key,
            "full_name": r["full_name"],
            "default_branch": r.get("default_branch", "main"),
            "enabled": enabled,
            "schedule_enabled": schedule_enabled,
            "private": r.get("private", True),
            "updated_at": r.get("updated_at"),
        })

    result.sort(key=lambda x: (not x["enabled"], x["name"]))
    payload = {"repos": result}

    with _cache_lock:
        _cache_set(cache_key, payload)
    return jsonify(payload)


@app.route("/api/repos/<repo_name>/workflows")
def get_workflows(repo_name):
    """List workflows for a repo."""
    cache_key = f"workflows:{ORG_NAME}:{repo_name}"

    with _cache_lock:
        if _cache_fresh(cache_key, CACHE_TTL_WORKFLOWS):
            return jsonify(_cache_get(cache_key)["data"])

    repos_config = _load_config().get("repos", {})
    repo_config = repos_config.get(repo_name) or {}

    try:
        data = _github_get(
            f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows"
        )
        workflows = data.get("workflows", []) if isinstance(data, dict) else data
        result = []
        for w in workflows:
            wid = w["id"]
            wcfg = (repo_config.get("workflows") or {}).get(str(wid)) or {}
            cron_expr = wcfg.get("cron") or wcfg.get("schedule") or ""
            result.append({
                "id": wid,
                "name": w["name"],
                "path": w["path"],
                "state": w["state"],
                "enabled_schedule": wcfg.get("enabled_schedule", False),
                "cron": cron_expr or "disabled",
                "branch": wcfg.get("branch", ""),
                "last_triggered": wcfg.get("last_triggered"),
                "last_run": None,
                "triggering": False,
                "trigger_error": None,
            })

        payload = {"workflows": result}
        with _cache_lock:
            _cache_set(cache_key, payload)
        return jsonify(payload)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/last-run")
def get_last_run(repo_name, workflow_id):
    """Get the last run for a specific workflow."""
    try:
        data = _github_get(
            f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows/{workflow_id}/runs",
            {"per_page": 1},
        )
        runs = data.get("workflow_runs", []) if isinstance(data, dict) else data
        if runs:
            return jsonify(_run_dict(runs[0]))
    except Exception:
        pass
    return jsonify(None)


@app.route("/api/repos/<repo_name>/workflows/batch-last-run", methods=["POST"])
def get_batch_last_run(repo_name):
    """Get last run for multiple workflows in parallel."""
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
                f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows/{wid}/runs",
                {"per_page": 1},
            )
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else data
            if runs:
                return str(wid), _run_dict(runs[0])
        except Exception:
            pass
        return str(wid), None

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = dict(executor.map(fetch_one, workflow_ids))
    return jsonify(results)


@app.route("/api/repos/<repo_name>/branches")
def get_branches(repo_name):
    """List branches for a repo."""
    err = _validate_name(repo_name, "repo")
    if err:
        return jsonify({"error": err}), 400
    try:
        data = _github_get(
            f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/branches"
        )
        branches = [b["name"] for b in data] if isinstance(data, list) else []
        return jsonify({"branches": branches})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/repos/<repo_name>/workflows/<workflow_id>/trigger", methods=["POST"])
def trigger_workflow(repo_name, workflow_id):
    """Trigger a workflow_dispatch on a workflow."""
    err = _validate_name(repo_name, "repo")
    if err:
        return jsonify({"error": err}), 400
    err = _validate_id(workflow_id, "workflow_id")
    if err:
        return jsonify({"error": err}), 400
    body = request.get_json() or {}
    branch = body.get("branch", "")
    if branch and not re.match(r"^[a-zA-Z0-9_./-]+$", branch):
        return jsonify({"error": "Invalid branch name"}), 400

    try:
        _github_post(
            f"https://api.github.com/repos/{ORG_NAME}/{repo_name}/actions/workflows/{workflow_id}/dispatches",
            {"ref": branch or "main"},
        )
        now = datetime.now(timezone.utc)
        wf_id_str = str(workflow_id)
        trigger_key = f"{repo_name}:{wf_id_str}"

        with _scheduler_lock:
            _scheduler_state["last_triggers"][trigger_key] = now.timestamp()

        cfg = _load_config()
        wf_entry = (
            cfg.setdefault("repos", {})
               .setdefault(repo_name, {})
               .setdefault("workflows", {})
               .setdefault(wf_id_str, {})
        )
        wf_entry["last_triggered"] = now.timestamp()
        _atomic_write(CONFIG_PATH, cfg)

        # Invalidate workflow cache with correct key
        with _cache_lock:
            _cache_invalidate(f"workflows:{ORG_NAME}:{repo_name}")

        return jsonify({"success": True, "message": "Triggered"})
    except RuntimeError as e:
        return jsonify({"success": False, "message": str(e)}), 502


@app.route("/api/trigger-selected", methods=["POST"])
def trigger_selected():
    """Trigger multiple workflows at once."""
    body = request.get_json()
    if body is None:
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    items = body.get("items", [])
    results = []
    for item in items:
        repo = item.get("repo", "")
        wf_id = str(item.get("workflow_id", ""))
        branch = item.get("branch", "main")
        name = item.get("name", "")

        err = _validate_name(repo, "repo")
        if err:
            results.append({"repo": repo, "name": name, "success": False, "message": err})
            continue
        err = _validate_id(wf_id, "workflow_id")
        if err:
            results.append({"repo": repo, "name": name, "success": False, "message": err})
            continue
        if branch and not re.match(r"^[a-zA-Z0-9_./-]+$", branch):
            results.append({"repo": repo, "name": name, "success": False, "message": "Invalid branch name"})
            continue

        try:
            _github_post(
                f"https://api.github.com/repos/{ORG_NAME}/{repo}/actions/workflows/{wf_id}/dispatches",
                {"ref": branch},
            )
            now = datetime.now(timezone.utc)
            with _scheduler_lock:
                _scheduler_state["last_triggers"][f"{repo}:{wf_id}"] = now.timestamp()

            cfg = _load_config()
            (
                cfg.setdefault("repos", {})
                   .setdefault(repo, {})
                   .setdefault("workflows", {})
                   .setdefault(wf_id, {})
            )["last_triggered"] = now.timestamp()
            _atomic_write(CONFIG_PATH, cfg)

            with _cache_lock:
                _cache_invalidate(f"workflows:{ORG_NAME}:{repo}")

            results.append({"repo": repo, "name": name, "success": True, "message": "Triggered"})
        except RuntimeError as e:
            results.append({"repo": repo, "name": name, "success": False, "message": str(e)})
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
                    headers={
                        "Authorization": f"token {new_pat}",
                        "Accept": "application/vnd.github+json",
                    },
                    timeout=10,
                )
                if r.status_code != 200:
                    return jsonify({"error": "Invalid PAT"}), 400
                cfg["github_pat"] = new_pat

            if data.get("timezone"):
                cfg["timezone"] = data["timezone"]

            # Deep-merge repos so partial updates don't wipe nested workflow keys
            if data.get("repos"):
                if not isinstance(cfg.get("repos"), dict):
                    cfg["repos"] = {}
                for rname, rcfg in data["repos"].items():
                    if not isinstance(cfg["repos"].get(rname), dict):
                        cfg["repos"][rname] = {}
                    if isinstance(rcfg, dict):
                        _deep_merge(cfg["repos"][rname], rcfg)

            # Always preserve _scheduler section — the scheduler loop owns it
            # (it's already in cfg from _load_config, nothing to do)

            _atomic_write(CONFIG_PATH, cfg)

            with _cache_lock:
                # Wipe all cached data so changes are visible immediately
                _cache.clear()
            _invalidate_pat_cache()
            return jsonify({"message": "Config saved"})
        except Exception as e:
            logger.exception(f"Config save error: {e}")
            return jsonify({"error": str(e)}), 500

    pat_set = cfg.get("github_pat") or os.environ.get("GITHUB_PAT", "")
    return jsonify({
        "org": ORG_NAME,
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
        if enabled is None:
            return jsonify({"error": "Invalid enabled value"}), 400
        try:
            if enabled:
                # Stop any existing thread first to avoid spawning doubles
                if _scheduler_state["thread"] is not None:
                    _stop_scheduler()
                _scheduler_state["running"] = True
                _start_scheduler()
            else:
                _stop_scheduler()
        except Exception as e:
            logger.exception(f"Scheduler toggle error: {e}")
            return jsonify({"error": str(e)}), 500
        return jsonify({"running": _scheduler_state["running"]})

    # GET — read from disk for consistency across gunicorn workers
    try:
        disk_running = (_load_config().get("_scheduler") or {}).get("running", None)
        return jsonify({"running": disk_running is not False})
    except Exception as e:
        logger.error(f"Scheduler GET error: {e}")
        return jsonify({"running": False})


@app.route("/api/scheduler/check-now", methods=["POST"])
def scheduler_check_now():
    """Manually trigger a scheduler check cycle immediately."""
    try:
        count = _check_and_trigger_all()
        return jsonify({"message": f"Check complete. {count} workflows triggered."})
    except Exception as e:
        logger.exception(f"Manual check error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------
def _get_last_trigger(key):
    with _scheduler_lock:
        return _scheduler_state["last_triggers"].get(key)


def _set_last_trigger(key, ts):
    with _scheduler_lock:
        _scheduler_state["last_triggers"][key] = ts


def _check_and_trigger_all():
    """
    Core scheduler logic: iterate all repos/workflows and trigger if due.
    Returns the number of workflows triggered.
    """
    cfg = _load_config()
    repos_config = cfg.get("repos") or {}
    org = cfg.get("org") or ORG_NAME
    try:
        tz = ZoneInfo(cfg.get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    triggered_count = 0

    to_trigger = [] # List of (repo_name, wf_id, branch)

    for repo_name, repo_cfg in repos_config.items():
        if not isinstance(repo_cfg, dict) or not repo_cfg.get("enabled", True):
            continue
        
        workflows = repo_cfg.get("workflows") or {}
        for wf_id, wf_cfg in workflows.items():
            if not isinstance(wf_cfg, dict) or not wf_cfg.get("enabled_schedule", False):
                continue
            
            cron_expr = wf_cfg.get("cron") or wf_cfg.get("schedule") or ""
            if not cron_expr or cron_expr == "disabled":
                continue

            key = f"{repo_name}:{wf_id}"
            last = _get_last_trigger(key)
            
            try:
                prev_trigger = croniter(cron_expr, now).get_prev(datetime)
            except Exception as e:
                logger.error(f"Invalid cron {cron_expr} for {key}: {e}")
                continue

            # Due if previous trigger was in the last 60s and we haven't triggered in the last 120s
            is_due = 0 <= (now - prev_trigger).total_seconds() < 60
            if is_due and (not last or (now.timestamp() - last) >= 120):
                to_trigger.append((repo_name, wf_id, wf_cfg.get("branch") or "main"))

    if not to_trigger:
        return 0

    def do_trigger(item):
        rname, wid, branch = item
        try:
            _github_post(
                f"https://api.github.com/repos/{org}/{rname}/actions/workflows/{wid}/dispatches",
                {"ref": branch},
            )
            logger.info(f"Scheduled trigger: {rname}/{wid}")
            return rname, wid, True
        except Exception as e:
            logger.error(f"Scheduler failed {rname}/{wid}: {e}")
            return rname, wid, False

    # Parallel trigger to avoid blocking the loop
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(do_trigger, to_trigger))
    
    # Update state and config for successful triggers
    for rname, wid, success in results:
        if success:
            triggered_count += 1
            ts = now.timestamp()
            _set_last_trigger(f"{rname}:{wid}", ts)
            (
                cfg.setdefault("repos", {})
                   .setdefault(rname, {})
                   .setdefault("workflows", {})
                   .setdefault(str(wid), {})
            )["last_triggered"] = ts

    # Persist state
    cfg.setdefault("_scheduler", {})["last_triggers"] = dict(_scheduler_state["last_triggers"])
    cfg["_scheduler"]["running"] = _scheduler_state["running"]
    _atomic_write(CONFIG_PATH, cfg)
    
    return triggered_count


def _scheduler_loop():
    """Background thread: check enabled workflows periodically."""
    logger.info("Scheduler loop started")
    while _scheduler_state["running"]:
        try:
            _check_and_trigger_all()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        # Sleep in small increments to respond quickly to stop signals
        for _ in range(_scheduler_config["interval"]):
            if not _scheduler_state["running"]:
                break
            time.sleep(1)

    logger.info("Scheduler loop stopped")


def _start_scheduler():
    """Start the scheduler thread if enabled."""
    cfg = _load_config()
    disk_triggers = (cfg.get("_scheduler") or {}).get("last_triggers") or {}
    with _scheduler_lock:
        _scheduler_state["last_triggers"].update(disk_triggers)

    saved_running = (cfg.get("_scheduler") or {}).get("running", True)
    _scheduler_state["running"] = saved_running

    if _scheduler_state["running"]:
        if _scheduler_state["thread"] is None or not _scheduler_state["thread"].is_alive():
            _scheduler_state["thread"] = threading.Thread(target=_scheduler_loop, daemon=True)
            _scheduler_state["thread"].start()
            logger.info("Scheduler thread spawned")
    else:
        logger.info("Scheduler disabled in config")


def _stop_scheduler():
    """Stop the scheduler thread."""
    _scheduler_state["running"] = False
    if _scheduler_state["thread"]:
        _scheduler_state["thread"].join(timeout=5)
        _scheduler_state["thread"] = None
    logger.info("Scheduler thread joined")


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
