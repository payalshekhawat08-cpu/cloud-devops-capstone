"""Cloud DevOps Capstone: Flask web app.

Features: user registration and login (hashed passwords, CSRF protection),
managed PostgreSQL (SQLite fallback for local runs), structured request
logging, a /health endpoint and a Prometheus-style /metrics endpoint.

Author: Payal Shekhawat
"""
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

# ---------- Configuration (everything comes from environment variables) ----------
APP_ENV = os.environ.get("APP_ENV", "development")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///local.db")
# Render sets RENDER_GIT_COMMIT automatically; the Docker image can set GIT_SHA.
APP_VERSION = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_SHA", "dev")

if APP_ENV == "production" and SECRET_KEY == "dev-only-change-me":
    raise RuntimeError("SECRET_KEY must be set in production")

# Render/Heroku style URLs start with postgres://, SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("capstone")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(APP_ENV == "production"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
START_TIME = time.time()

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,30}$")
# Used so that a login for an unknown user takes as long as one for a known user.
DUMMY_HASH = generate_password_hash("not-a-real-password")

# ---------- Metrics (kept in memory; the app runs one worker process) ----------
_metrics_lock = threading.Lock()
_requests = defaultdict(int)          # (method, endpoint, status) -> count
_duration = {"sum": 0.0, "count": 0}  # request latency
_logins = defaultdict(int)            # result -> count


def _count_login(result):
    with _metrics_lock:
        _logins[result] += 1


# ---------- Database ----------
def init_db(retries=5):
    """Create tables if missing (works on SQLite and PostgreSQL)."""
    pk = ("INTEGER PRIMARY KEY AUTOINCREMENT" if engine.dialect.name == "sqlite"
          else "SERIAL PRIMARY KEY")
    statements = [
        f"CREATE TABLE IF NOT EXISTS users ("
        f"id {pk}, "
        f"username VARCHAR(30) UNIQUE NOT NULL, "
        f"password_hash VARCHAR(255) NOT NULL, "
        f"created_at VARCHAR(40) NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS messages ("
        f"id {pk}, "
        f"name VARCHAR(80) NOT NULL, "
        f"body VARCHAR(500) NOT NULL, "
        f"created_at VARCHAR(40) NOT NULL)",
    ]
    for attempt in range(1, retries + 1):
        try:
            with engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
            log.info("Database ready (dialect=%s)", engine.dialect.name)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("DB init attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(2)


init_db()


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------- Security helpers ----------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Request hooks: CSRF check, logging, metrics ----------
@app.before_request
def before_request():
    g.start = time.perf_counter()
    if request.method == "POST":
        sent = request.form.get("_csrf", "").encode()
        expected = session.get("_csrf", "").encode()
        if not expected or not hmac.compare_digest(sent, expected):
            log.warning("CSRF check failed for %s", request.path)
            abort(400)


@app.after_request
def after_request(response):
    elapsed = time.perf_counter() - g.get("start", time.perf_counter())
    endpoint = request.endpoint or "unknown"
    with _metrics_lock:
        _requests[(request.method, endpoint, response.status_code)] += 1
        _duration["sum"] += elapsed
        _duration["count"] += 1
    level = logging.DEBUG if endpoint in ("health", "metrics") else logging.INFO
    log.log(level, "%s %s -> %s %.1fms user=%s", request.method, request.path,
            response.status_code, elapsed * 1000, session.get("user", "-"))
    return response


# ---------- Pages ----------
@app.route("/")
@login_required
def index():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name, body, created_at FROM messages ORDER BY id DESC LIMIT 50"
        )).fetchall()
    return render_template("index.html", messages=rows, user=session["user"],
                           env=APP_ENV, db=engine.dialect.name)


@app.route("/add", methods=["POST"])
@login_required
def add():
    body = (request.form.get("body") or "").strip()[:500]
    if not body:
        return redirect(url_for("index"))
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO messages (name, body, created_at) VALUES (:n, :b, :t)"),
            {"n": session["user"], "b": body, "t": now_utc()},
        )
    log.info("Message saved for user %r", session["user"])
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if not USERNAME_RE.match(username):
            error = "Username must be 3-30 characters: letters, numbers or underscore."
        elif not 8 <= len(password) <= 128:
            error = "Password must be 8 to 128 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("INSERT INTO users (username, password_hash, created_at) "
                             "VALUES (:u, :p, :t)"),
                        {"u": username, "p": generate_password_hash(password),
                         "t": now_utc()},
                    )
            except IntegrityError:
                error = "That username is already taken."
            else:
                session.clear()
                session["user"] = username
                session.permanent = True
                log.info("New user registered: %r", username)
                return redirect(url_for("index"))
    return render_template("auth.html", mode="register", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT password_hash FROM users WHERE username = :u"),
                {"u": username},
            ).fetchone()
        valid = check_password_hash(row[0] if row else DUMMY_HASH, password)
        if row and valid:
            session.clear()
            session["user"] = username
            session.permanent = True
            _count_login("success")
            log.info("Login success for %r", username)
            return redirect(url_for("index"))
        _count_login("failure")
        log.warning("Login failed for %r", username[:30])
        error = "Wrong username or password."
    return render_template("auth.html", mode="login", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    log.info("Logout for %r", session.get("user"))
    session.clear()
    return redirect(url_for("login"))


# ---------- Monitoring endpoints ----------
@app.route("/health")
def health():
    """Checked by Render, by Docker (HEALTHCHECK) and by the uptime monitor."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        log.error("Health check DB failure: %s", exc)
        db_ok = False
    return jsonify(
        status="ok" if db_ok else "degraded",
        database="up" if db_ok else "down",
        environment=APP_ENV,
        commit=APP_VERSION,
        uptime_seconds=int(time.time() - START_TIME),
    ), (200 if db_ok else 503)


@app.route("/metrics")
def metrics():
    """Prometheus text format: request counts, latency and login results."""
    lines = [
        "# HELP app_requests_total Total HTTP requests.",
        "# TYPE app_requests_total counter",
    ]
    with _metrics_lock:
        for (method, endpoint, status), n in sorted(_requests.items()):
            lines.append(f'app_requests_total{{method="{method}",'
                         f'endpoint="{endpoint}",status="{status}"}} {n}')
        lines += [
            "# HELP app_request_duration_seconds Request latency.",
            "# TYPE app_request_duration_seconds summary",
            f"app_request_duration_seconds_sum {_duration['sum']:.6f}",
            f"app_request_duration_seconds_count {_duration['count']}",
            "# HELP app_logins_total Login attempts by result.",
            "# TYPE app_logins_total counter",
        ]
        for result, n in sorted(_logins.items()):
            lines.append(f'app_logins_total{{result="{result}"}} {n}')
    lines += [
        "# HELP app_uptime_seconds Seconds since the app started.",
        "# TYPE app_uptime_seconds gauge",
        f"app_uptime_seconds {int(time.time() - START_TIME)}",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
