# databricks-label-studio-app

Run [Label Studio](https://labelstud.io/guide/install.html) as a **Databricks App** with **Lakebase** (PostgreSQL) for application data. The app maps Databricks-injected `PGHOST` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` / `PGPORT` variables to Label Studio’s `POSTGRE_*` settings ([database storage](https://labelstud.io/guide/storedata.html)).

## Prerequisites

- Databricks CLI (recent version with Asset Bundle and Apps support), authenticated (`databricks auth login` or a profile in `~/.databrickscfg`).
- **Lakebase Autoscaling** enabled in the workspace region; a Lakebase **project** you can attach to the app (you need **CAN MANAGE** on the project to add it as an app resource).
- Workspace under the **app limit** (delete unused apps if creation fails).

Confirm Lakebase project API names with:

`databricks postgres list-projects --profile <profile>`

## Deploying to another Databricks workspace

Point the bundle at the **target workspace** and at **your** Lakebase project, branch, and database resource. All of this is controlled in **`databricks.yml`** (and optionally extra **targets** in the same file).

### 1. Workspace URL and authentication (`targets`)

Under `targets.<name>.workspace`:

| Setting | Purpose |
|--------|---------|
| **`host`** | HTTPS URL of the workspace, e.g. `https://my-workspace.cloud.databricks.com`. **Change this** when deploying outside the workspace used for development. |
| **`profile`** | Databricks CLI profile from `~/.databrickscfg` that maps to that host. Use **`${var.profile}`** and set the **`profile`** variable (below), or set `profile` inline per target. |

If several profiles share the same `host`, the CLI can error with “multiple profiles matched”; setting **`variables.profile`** (or `workspace.profile` explicitly) fixes that.

**Example** for a second workspace (add a target or edit `dev`):

```yaml
targets:
  dev:
    default: true
    mode: development
    workspace:
      profile: ${var.profile}
      host: https://your-other-workspace.cloud.databricks.com
```

Deploy with:

`databricks bundle deploy -t dev --profile <YourProfile>`

The **`--profile`** flag must match a profile that can authenticate to the **`host`** in that target.

### 2. Bundle and app naming (`bundle.name`, app resource)

| Setting | File | Purpose |
|--------|------|---------|
| **`bundle.name`** | `databricks.yml` | Logical bundle name; **must be unique** in the workspace **bundle state path** (`.bundle/<bundle-name>/...`). Change if you deploy multiple copies of this repo to the same workspace. |
| **App name** | `resources/label_studio.app.yml` → `name: label-studio-${bundle.target}` | Deployed app name in the workspace (e.g. `label-studio-dev`). Must be unique among apps; **≤ 26 characters**, lowercase letters, digits, hyphens. |

### 3. Lakebase variables (`variables` in `databricks.yml`)

These **must match the Lakebase project** in the workspace you deploy to:

| Variable | Purpose |
|----------|---------|
| **`lakebase_project`** | API **project id** (e.g. `my-project`), **not** necessarily the UUID shown in the Lakebase UI. Use `databricks postgres list-projects`. |
| **`lakebase_branch`** | Branch id (often `production`). List with `databricks postgres list-branches projects/<lakebase_project>`. |
| **`lakebase_database_resource_id`** | The **`db-…`** segment from the **database resource name** returned by the API. **Not** the Postgres database name `databricks_postgres`. |

Discover the database resource id:

```bash
databricks api get "/api/2.0/postgres/projects/<lakebase_project>/branches/<lakebase_branch>/databases" -p <profile>
```

Use the `name` field like `projects/.../databases/db-xxxxx` and set **`lakebase_database_resource_id`** to `db-xxxxx`.

The **`resources/label_studio.app.yml`** file builds the full `postgres.database` path from these variables; you normally **do not** edit that file when moving workspaces—only **`databricks.yml`** variables.

### 4. Optional: multiple environments (dev / staging / prod)

You can define **several targets** (e.g. `dev`, `prod`) with different `workspace.host`, `profile`, and `variables` overrides per target. Use the same `include: resources/*.yml` pattern; override variables under each target:

```yaml
targets:
  prod:
    mode: production
    workspace:
      profile: prod-profile
      host: https://prod-workspace.cloud.databricks.com
    variables:
      lakebase_project: "label-studio-prod"
      lakebase_database_resource_id: "db-xxxxxxxxxxxx"
```

Then: `databricks bundle deploy -t prod --profile prod-profile`.

### 5. After deploy: app URL and OAuth

After the first successful deploy, the app’s public URL is shown in **Compute → Apps** (or `databricks apps get label-studio-<target>`). On **AWS** Databricks Apps, `start.py` sets **`CSRF_TRUSTED_ORIGINS`** and **`LABEL_STUDIO_HOST`** from the runtime environment. On **Azure** or custom domains, set **`CSRF_TRUSTED_ORIGINS`** and **`LABEL_STUDIO_HOST`** in **`src/app/app.yaml`** to your app’s HTTPS origin (no trailing slash on `LABEL_STUDIO_HOST`).

## Deploy (default target)

```bash
databricks bundle deploy -t dev --profile DEFAULT
databricks bundle run label_studio -t dev --profile DEFAULT
```

After deployment, open the app URL from **Compute → Apps** (or `databricks apps get label-studio-dev`).

## CSRF / signup (403 Forbidden)

On **AWS** Databricks Apps, `start.py` sets **`CSRF_TRUSTED_ORIGINS`** from `DATABRICKS_APP_NAME` and `DATABRICKS_WORKSPACE_ID`. For **Azure** or a custom domain, set **`CSRF_TRUSTED_ORIGINS`** in `app.yaml` to your full app origin (comma-separated, no trailing slash), e.g. `https://your-app.example.com`.

## Preview shows `$undefined$` / bad URLs for imported files

`start.py` sets **`LABEL_STUDIO_HOST`** to your public app URL when unset so Label Studio can build correct links ([external URL](https://labelstud.io/guide/start#Run-Label-Studio-with-an-external-domain-name)).

If the error still mentions **`$undefined$`**, the **labeling config** is usually referencing a field that your tasks do not define. For example, `<Image value="$image"/>` requires each task’s `data` to include an **`image`** key. Rename the key in your import JSON or change `$image` in the config to match your field.

## PostgreSQL 15+ / `public` schema

Lakebase uses Postgres 15-style defaults: new roles may not **CREATE** objects in schema `public`. The app’s `start.py` creates schema `label_studio` (override with env `LABEL_STUDIO_DB_SCHEMA`) and sets `PGOPTIONS` so Django migrations run in that schema. If schema creation fails, ask a project admin to run `GRANT CREATE ON SCHEMA public TO "<app role>";` or create the schema manually.

## Lakebase auth (Autoscaling)

Connection uses an **OAuth token as the Postgres password** ([connection strings](https://docs.databricks.com/aws/en/oltp/projects/connection-strings)). The app’s `start.py` calls `POST /api/2.0/postgres/credentials` when `PGPASSWORD` is not set. Tokens are short-lived; if the app fails after a long idle period, **restart the app** from Compute → Apps.