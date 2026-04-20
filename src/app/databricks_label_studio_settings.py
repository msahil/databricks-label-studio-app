"""
Lakebase/Databricks App: extend Label Studio Django settings so PostgreSQL
always uses a dedicated schema for ORM tables and django.db.migrations.

We cannot ``from label_studio.core.settings.label_studio import *`` while
``DJANGO_SETTINGS_MODULE`` is this file: Label Studio's settings run
``sentry.init_sentry()``, which reads ``django.conf.settings.SENTRY_DSN`` before
this module's namespace is populated, causing AttributeError.

Instead: temporarily set ``DJANGO_SETTINGS_MODULE`` to Label Studio's module,
import it, copy its public bindings into this module, patch ``DATABASES``,
re-bind ``django.conf.settings`` to this module, then import ``django.db``.
"""
from __future__ import annotations

import os
import sys
from importlib import import_module

_LABEL_STUDIO_SETTINGS = "label_studio.core.settings.label_studio"
_this = sys.modules[__name__]

_saved = os.environ.get("DJANGO_SETTINGS_MODULE")
if _saved == __name__:
    os.environ["DJANGO_SETTINGS_MODULE"] = _LABEL_STUDIO_SETTINGS

_ls = import_module(_LABEL_STUDIO_SETTINGS)

if _saved == __name__:
    os.environ["DJANGO_SETTINGS_MODULE"] = _saved

for _k, _v in vars(_ls).items():
    if _k.startswith("_"):
        continue
    setattr(_this, _k, _v)


def _validated_schema(name: str) -> str:
    s = (name or "").strip() or "label_studio"
    if not s.replace("_", "").isalnum() or not s[0].isalpha():
        raise ValueError(f"Invalid LABEL_STUDIO_DB_SCHEMA for Postgres OPTIONS: {name!r}")
    return s


_schema = _validated_schema(os.environ.get("LABEL_STUDIO_DB_SCHEMA", "label_studio"))

_db = DATABASES.get("default")
if _db and _db.get("ENGINE") == "django.db.backends.postgresql":
    opts = dict(_db.get("OPTIONS") or {})
    existing = str(opts.get("options", "")).strip()
    search_opt = f"-c search_path={_schema},public"
    opts["options"] = f"{existing} {search_opt}".strip() if existing else search_opt
    _db["OPTIONS"] = opts

# After DATABASES patch: Django must use this module (not label_studio) for settings lookups.
import django.conf as _django_conf

_holder = getattr(_django_conf, "UserSettingsHolder", None)
if _holder is None:
    raise RuntimeError("django.conf.UserSettingsHolder missing; incompatible Django version")
_django_conf.settings._wrapped = _holder(_this)


def _lakebase_set_search_path(sender, connection, **kwargs) -> None:
    if connection.vendor != "postgresql":
        return
    schema = _validated_schema(os.environ.get("LABEL_STUDIO_DB_SCHEMA", "label_studio"))
    q = connection.ops.quote_name(schema)
    raw = connection.connection
    if raw is None:
        return
    with raw.cursor() as cursor:
        cursor.execute(f"SET search_path TO {q}, public")


from django.db.backends.signals import connection_created  # noqa: E402

connection_created.connect(_lakebase_set_search_path)
