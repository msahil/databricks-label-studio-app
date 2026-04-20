#!/usr/bin/env python3
"""
Label Studio entrypoint for Databricks Apps + Lakebase (Autoscaling).

Lakebase uses an OAuth token as the Postgres password. The runtime sets PGHOST,
PGUSER, etc., but often does not set PGPASSWORD. We obtain a token with
POST /api/2.0/postgres/credentials using the app service principal (Config).

The bundle maps resource key `database` -> LAKEBASE_POSTGRES_ENDPOINT (endpoint path).
See: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/environment-variables

PostgreSQL 15+ does not allow all roles to CREATE in schema `public`. Lakebase app roles
typically have CREATE on the database but not on `public`. We create a dedicated schema
owned by the app role, set `search_path` via PGOPTIONS, and set `DJANGO_SETTINGS_MODULE` to
`databricks_label_studio_settings` so Django adds the same `search_path` in DATABASE OPTIONS
for migrate and all ORM connections.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import psycopg2
from psycopg2 import sql as sql_composer
from databricks.sdk.core import Config

# Must be a valid Postgres identifier; Django creates tables in this schema.
_DEFAULT_DB_SCHEMA = os.environ.get("LABEL_STUDIO_DB_SCHEMA", "label_studio")


def _databricks_apps_https_origin() -> str | None:
    """
    Public URL for AWS Databricks Apps: https://<app-name>-<workspace-id>.aws.databricksapps.com
    (see system-env: DATABRICKS_APP_NAME, DATABRICKS_WORKSPACE_ID).
    """
    name = os.environ.get("DATABRICKS_APP_NAME", "").strip()
    ws = os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip()
    if not name or not ws:
        return None
    return f"https://{name}-{ws}.aws.databricksapps.com"


def _ensure_csrf_and_public_host() -> None:
    """
    Django CSRF + correct absolute URLs for uploads/previews behind the Apps reverse proxy.
    Without LABEL_STUDIO_HOST, task media links can resolve wrong and the UI shows $undefined$.
    """
    origin = _databricks_apps_https_origin()
    if not origin:
        return
    if not os.environ.get("CSRF_TRUSTED_ORIGINS", "").strip():
        os.environ["CSRF_TRUSTED_ORIGINS"] = origin
    if not os.environ.get("LABEL_STUDIO_HOST", "").strip():
        # No trailing slash (Label Studio / Django expectations for link generation).
        os.environ["LABEL_STUDIO_HOST"] = origin.rstrip("/")


def _fetch_lakebase_oauth_token(endpoint: str) -> str:
    cfg = Config()
    host = (cfg.host or os.environ.get("DATABRICKS_HOST", "")).rstrip("/")
    if not host:
        raise RuntimeError("DATABRICKS_HOST / Config.host is not set")
    url = f"{host}/api/2.0/postgres/credentials"
    body = json.dumps({"endpoint": endpoint}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for name, value in cfg.authenticate().items():
        req.add_header(name, value)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Lakebase credential request failed: {e.code} {detail}") from e
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError(f"Unexpected credential response: {payload!r}")
    return token


def _ensure_pg_password() -> None:
    if os.environ.get("PGPASSWORD"):
        return
    endpoint = os.environ.get("LAKEBASE_POSTGRES_ENDPOINT", "").strip()
    if not endpoint:
        return
    os.environ["PGPASSWORD"] = _fetch_lakebase_oauth_token(endpoint)


def _map_pg_to_label_studio() -> None:
    if not os.environ.get("PGHOST"):
        return
    os.environ.setdefault("DJANGO_DB", "default")
    os.environ["POSTGRE_HOST"] = os.environ["PGHOST"]
    os.environ["POSTGRE_NAME"] = os.environ.get("PGDATABASE", "postgres")
    os.environ["POSTGRE_USER"] = os.environ.get("PGUSER", "")
    os.environ["POSTGRE_PASSWORD"] = os.environ.get("PGPASSWORD", "")
    os.environ["POSTGRE_PORT"] = os.environ.get("PGPORT", "5432")


def _prepend_pythonpath(directory: str) -> None:
    """Ensure label-studio child process can import databricks_label_studio_settings."""
    sep = os.pathsep
    raw = os.environ.get("PYTHONPATH", "").strip()
    parts = [p for p in raw.split(sep) if p] if raw else []
    if directory not in parts:
        parts.insert(0, directory)
    os.environ["PYTHONPATH"] = sep.join(parts)


def _ensure_django_uses_lakebase_schema(schema: str) -> None:
    """
    Label Studio's server uses os.environ.setdefault(DJANGO_SETTINGS_MODULE, ...).
    Set our module first so Django (migrate, runserver, ORM) loads DATABASE OPTIONS
    with search_path for this schema.
    """
    app_dir = os.path.dirname(os.path.abspath(__file__))
    _prepend_pythonpath(app_dir)
    os.environ["DJANGO_SETTINGS_MODULE"] = "databricks_label_studio_settings"
    # Same schema as CREATE SCHEMA / PGOPTIONS so Django migrations and ORM stay aligned.
    os.environ["LABEL_STUDIO_DB_SCHEMA"] = schema


def _ensure_dedicated_schema(schema: str) -> None:
    """Create a non-public schema so migrations are allowed (PG15+ public schema restrictions)."""
    if not os.environ.get("PGHOST") or not os.environ.get("PGPASSWORD"):
        return
    if not schema.replace("_", "").isalnum() or not schema[0].isalpha():
        raise ValueError(f"Invalid schema name: {schema!r}")
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER"),
        password=os.environ["PGPASSWORD"],
        port=os.environ.get("PGPORT", "5432"),
        sslmode=os.environ.get("PGSSLMODE", "require"),
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                sql_composer.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION CURRENT_USER").format(
                    sql_composer.Identifier(schema)
                )
            )
    finally:
        conn.close()
    # libpq default for new connections; include public so search_path is never empty.
    os.environ["PGOPTIONS"] = f"-c search_path={schema},public"


def main() -> None:
    _ensure_csrf_and_public_host()
    _ensure_pg_password()
    _map_pg_to_label_studio()
    _ensure_dedicated_schema(_DEFAULT_DB_SCHEMA)
    _ensure_django_uses_lakebase_schema(_DEFAULT_DB_SCHEMA)

    port = os.environ.get("DATABRICKS_APP_PORT", "8000")
    os.environ["LABEL_STUDIO_PORT"] = port

    os.execvp(
        "label-studio",
        [
            "label-studio",
            "start",
            "--no-browser",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[start.py] {exc}", file=sys.stderr)
        sys.exit(1)
