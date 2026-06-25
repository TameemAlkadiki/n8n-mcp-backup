"""
n8n Backup MCP Server
=====================
A FastMCP server that bridges an LLM (e.g. Claude Desktop) with a running n8n
instance. Exposes six tools:

  take_backup             – exports all workflows AND credentials to a timestamped JSON
  list_backups            – lists available backups in the S3 bucket, newest first
  restore_backup          – imports a specific backup from the S3 bucket into n8n
  take_postgres_backup    – streams a pg_dump directly to S3 via multipart upload
  list_postgres_backups   – lists available Postgres backups in the S3 bucket
  restore_postgres_backup – downloads a Postgres backup from S3 and restores it

Tech-stack rationale
--------------------
- Python + FastMCP  : minimal boilerplate, stdio transport works out of the box
                      with Claude Desktop's `docker exec -i` pattern.
- requests          : straightforward HTTP client; no async needed here since
                      MCP tool calls are themselves sequential.
- boto3             : AWS-SDK-compatible S3 client; works with any S3-compatible
                      storage backend (AWS S3, Garage, MinIO, etc.).
- python-dotenv     : keeps credentials out of code; reads from .env at startup.

Storage model
-------------
S3-compatible storage is the source of truth. /tmp/backup-staging is used only
as a transient staging area during upload/download and is always cleaned up.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import boto3
import requests
from botocore.config import Config as BotocoreConfig
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ── Environment ──────────────────────────────────────────────────────────────
load_dotenv()

N8N_URL     = os.getenv("N8N_URL", "http://n8n:5678").rstrip("/")
N8N_API_KEY = os.getenv("N8N_API_KEY", "")

S3_ENDPOINT   = os.getenv("S3_ENDPOINT", "http://localhost:3900")
S3_REGION     = os.getenv("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET          = os.getenv("S3_BUCKET",          "n8n-backup")
S3_BUCKET_POSTGRES = os.getenv("S3_BUCKET_POSTGRES", "postgres-backup")

STAGING_DIR = "/tmp/backup-staging"

PG_HOST     = os.getenv("PG_HOST",     "postgres")
PG_PORT     = os.getenv("PG_PORT",     "5432")
PG_DB       = os.getenv("PG_DB",       "")
PG_USER     = os.getenv("PG_USER",     "")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

if not N8N_API_KEY:
    raise RuntimeError(
        "N8N_API_KEY is not set. "
        "Copy .env.example → .env and fill in your key."
    )

if not S3_ACCESS_KEY or not S3_SECRET_KEY:
    raise RuntimeError(
        "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set in .env."
    )

if not PG_DB or not PG_USER or not PG_PASSWORD:
    raise RuntimeError(
        "PG_DB, PG_USER, and PG_PASSWORD must all be set in .env."
    )

# ── S3 Client ─────────────────────────────────────────────────────────────────

_s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=BotocoreConfig(signature_version="s3v4"),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "X-N8N-API-KEY": N8N_API_KEY,
        "Content-Type":  "application/json",
    }


def _get_all_pages(endpoint: str) -> list:
    """Fetch every page of a paginated n8n v1 list endpoint."""
    items, cursor = [], None
    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{N8N_URL}/api/v1/{endpoint}",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        body   = resp.json()
        items += body.get("data", [])
        cursor = body.get("nextCursor")
        if not cursor:
            break
    return items


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("n8n-backup-manager")


@mcp.tool()
def take_backup() -> str:
    """
    Export all n8n workflows AND credentials and upload them as a single
    timestamped JSON object to the S3 bucket.
    """
    try:
        workflows   = _get_all_pages("workflows")
        credentials = _get_all_pages("credentials")
    except requests.exceptions.ConnectionError:
        return (
            f"Connection Error: could not reach n8n at {N8N_URL}. "
            "Is the container running?"
        )
    except requests.exceptions.HTTPError as exc:
        return f"n8n API error: {exc.response.status_code} – {exc.response.text}"
    except Exception as exc:
        return f"Unexpected error fetching data: {exc}"

    now        = datetime.now()
    timestamp  = now.strftime("%Y-%m-%d_%H-%M-%S")
    object_key = f"n8n_backup_{timestamp}.json"

    payload = {
        "exported_at": now.isoformat(),
        "n8n_url":     N8N_URL,
        "workflows":   workflows,
        "credentials": credentials,
    }

    os.makedirs(STAGING_DIR, exist_ok=True)
    tmp_path = os.path.join(STAGING_DIR, object_key)

    try:
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as exc:
            return f"Failed to write staging file: {exc}"

        _s3.upload_file(tmp_path, S3_BUCKET, object_key)
    except Exception as exc:
        return f"Upload to S3 failed: {exc}"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return (
        f"Backup complete → s3://{S3_BUCKET}/{object_key}\n"
        f"  • {len(workflows)} workflow(s)\n"
        f"  • {len(credentials)} credential(s)"
    )


@mcp.tool()
def list_backups() -> str:
    """
    Return a list of available backup objects in the S3 bucket, newest first
    with sizes.
    """
    try:
        paginator = _s3.get_paginator("list_objects_v2")
        objects = []
        for page in paginator.paginate(Bucket=S3_BUCKET):
            objects.extend(page.get("Contents", []))
    except Exception as exc:
        return f"Could not list S3 bucket '{S3_BUCKET}': {exc}"

    json_objects = [o for o in objects if o["Key"].endswith(".json")]

    if not json_objects:
        return f"Bucket '{S3_BUCKET}' exists but contains no backup files yet."

    json_objects.sort(key=lambda o: o["LastModified"], reverse=True)

    lines = [f"Found {len(json_objects)} backup(s) in s3://{S3_BUCKET} (newest first):"]
    for obj in json_objects:
        ts   = obj["LastModified"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        size = obj["Size"]
        lines.append(f"  {obj['Key']}  ({size:,} bytes)  {ts}")

    return "\n".join(lines)


@mcp.tool()
def restore_backup(filename: str, skip_existing: bool = False) -> str:
    """
    Download a backup object from S3 and import it into n8n.

    Args:
        filename:      Name of the backup object (e.g. n8n_backup_2026-03-10_15-56-33.json).
        skip_existing: If True, skip any workflow whose name already exists in n8n.
                       Default: False (safe mode – always create, appending
                       ' (Restored)' to the name to avoid collisions).
    """
    safe_name = os.path.basename(filename)
    os.makedirs(STAGING_DIR, exist_ok=True)
    tmp_path = os.path.join(STAGING_DIR, safe_name)

    try:
        _s3.download_file(S3_BUCKET, filename, tmp_path)
    except Exception as exc:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        available = list_backups()
        return f"Could not download '{filename}' from S3: {exc}\n\n{available}"

    try:
        with open(tmp_path, "r", encoding="utf-8") as fh:
            backup = json.load(fh)
    except json.JSONDecodeError as exc:
        return f"Corrupt backup file – JSON parse error: {exc}"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # Support both old format (data: [...]) and new format (workflows: [...])
    workflows = backup.get("workflows") or backup.get("data", [])

    if not workflows:
        return f"No workflows found inside {filename}."

    # Fetch existing workflow names once before the loop (avoids N+1 API calls)
    existing_names: set = set()
    if skip_existing:
        try:
            existing_names = {w.get("name") for w in _get_all_pages("workflows")}
        except Exception as exc:
            return f"Could not fetch existing workflows from n8n: {exc}"

    restored, skipped, errors = 0, 0, []

    for wf in workflows:
        restore_name = wf.get("name", "Unnamed Workflow")

        if skip_existing and restore_name in existing_names:
            skipped += 1
            continue

        # Strip all server-managed / read-only fields; send only what the
        # POST /workflows endpoint actually accepts.
        clean_wf = {
            "name":        restore_name if skip_existing else f"{restore_name} (Restored)",
            "nodes":       wf.get("nodes", []),
            "connections": wf.get("connections", {}),
            "settings":    {},
            "staticData":  None,
        }

        try:
            resp = requests.post(
                f"{N8N_URL}/api/v1/workflows",
                headers=_headers(),
                json=clean_wf,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                restored += 1
            else:
                errors.append(f"'{restore_name}': {resp.status_code} – {resp.text[:120]}")
        except requests.exceptions.ConnectionError:
            return (
                f"Connection Error: lost contact with n8n at {N8N_URL} "
                "mid-restore. Check container health."
            )

    parts = [f"Restore complete from s3://{S3_BUCKET}/{filename}:"]
    parts.append(f"  • {restored} workflow(s) imported")
    if skipped:
        parts.append(f"  • {skipped} skipped (already exist, skip_existing=True)")
    if errors:
        parts.append(f"  • {len(errors)} error(s):")
        parts += [f"      – {e}" for e in errors]
    parts.append(
        "\n⚠ Credentials not restored — re-enter secrets manually in "
        "n8n Settings → Credentials."
    )

    return "\n".join(parts)


# ── Postgres Tools ───────────────────────────────────────────────────────────

@mcp.tool()
def take_postgres_backup() -> str:
    """
    Run pg_dump against the configured PostgreSQL database (custom format) and
    stream the output directly to S3 via multipart upload — no local temp file
    is written.
    """
    CHUNK_SIZE = 6 * 1024 * 1024  # 6 MB — above the 5 MB S3 minimum part size

    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "z"
    filename   = f"{timestamp}.dump"
    object_key = f"postgres/{PG_DB}/{filename}"

    pg_env = os.environ.copy()
    pg_env["PGPASSWORD"] = PG_PASSWORD

    try:
        mpu = _s3.create_multipart_upload(
            Bucket=S3_BUCKET_POSTGRES, Key=object_key
        )
    except Exception as exc:
        return f"Could not start multipart upload: {exc}"

    upload_id = mpu["UploadId"]
    parts: list = []

    try:
        try:
            proc = subprocess.Popen(
                [
                    "pg_dump",
                    "-h", PG_HOST, "-p", PG_PORT,
                    "-U", PG_USER, "-d", PG_DB,
                    "-Fc",
                ],
                env=pg_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            _s3.abort_multipart_upload(
                Bucket=S3_BUCKET_POSTGRES, Key=object_key, UploadId=upload_id
            )
            return "pg_dump not found – is postgresql-client installed in this container?"

        part_number = 1
        while True:
            chunk = proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            resp = _s3.upload_part(
                Bucket=S3_BUCKET_POSTGRES,
                Key=object_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=chunk,
            )
            parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
            part_number += 1

        proc.stdout.close()
        proc.wait(timeout=300)

        if proc.returncode != 0:
            stderr_text = proc.stderr.read().decode(errors="replace").strip()
            _s3.abort_multipart_upload(
                Bucket=S3_BUCKET_POSTGRES, Key=object_key, UploadId=upload_id
            )
            return f"pg_dump failed (exit {proc.returncode}): {stderr_text}"

        _s3.complete_multipart_upload(
            Bucket=S3_BUCKET_POSTGRES,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    except Exception as exc:
        _s3.abort_multipart_upload(
            Bucket=S3_BUCKET_POSTGRES, Key=object_key, UploadId=upload_id
        )
        return f"Streaming backup failed: {exc}"

    head = _s3.head_object(Bucket=S3_BUCKET_POSTGRES, Key=object_key)
    size = head["ContentLength"]

    return (
        f"Postgres backup complete → s3://{S3_BUCKET_POSTGRES}/{object_key}\n"
        f"  • file:      {filename}\n"
        f"  • key:       {object_key}\n"
        f"  • size:      {size:,} bytes\n"
        f"  • timestamp: {timestamp}"
    )


@mcp.tool()
def list_postgres_backups() -> str:
    """
    List available Postgres backup objects in the S3 bucket, newest first.
    """
    try:
        paginator = _s3.get_paginator("list_objects_v2")
        objects = []
        for page in paginator.paginate(Bucket=S3_BUCKET_POSTGRES, Prefix="postgres/"):
            objects.extend(page.get("Contents", []))
    except Exception as exc:
        return f"Could not list S3 bucket '{S3_BUCKET_POSTGRES}': {exc}"

    if not objects:
        return f"Bucket '{S3_BUCKET_POSTGRES}' contains no Postgres backups yet."

    objects.sort(key=lambda o: o["LastModified"], reverse=True)

    lines = [
        f"Found {len(objects)} Postgres backup(s) in "
        f"s3://{S3_BUCKET_POSTGRES} (newest first):"
    ]
    for obj in objects:
        ts   = obj["LastModified"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        size = obj["Size"]
        lines.append(f"  {obj['Key']}  ({size:,} bytes)  {ts}")

    return "\n".join(lines)


@mcp.tool()
def restore_postgres_backup(object_key: str) -> str:
    """
    Download a Postgres backup from S3 and restore it into the configured
    database using pg_restore --clean --if-exists.

    Args:
        object_key: Full S3 key of the backup to restore
                    (e.g. postgres/mydb/20260506_143022z.dump).
    """
    safe_name = os.path.basename(object_key)
    tmp_path  = os.path.join(STAGING_DIR, safe_name)

    os.makedirs(STAGING_DIR, exist_ok=True)

    pg_env = os.environ.copy()
    pg_env["PGPASSWORD"] = PG_PASSWORD

    try:
        try:
            _s3.download_file(S3_BUCKET_POSTGRES, object_key, tmp_path)
        except Exception as exc:
            available = list_postgres_backups()
            return (
                f"Could not download '{object_key}' from S3: {exc}\n\n{available}"
            )

        try:
            result = subprocess.run(
                [
                    "pg_restore",
                    "-h", PG_HOST, "-p", PG_PORT,
                    "-U", PG_USER, "-d", PG_DB,
                    "--clean", "--if-exists",
                    tmp_path,
                ],
                env=pg_env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return f"pg_restore failed (exit {result.returncode}): {result.stderr.strip()}"
        except FileNotFoundError:
            return "pg_restore not found – is postgresql-client installed in this container?"
        except subprocess.TimeoutExpired:
            return "pg_restore timed out after 300 seconds."
        except Exception as exc:
            return f"pg_restore error: {exc}"

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return (
        f"Restore complete from s3://{S3_BUCKET_POSTGRES}/{object_key}\n"
        f"  • Database '{PG_DB}' restored successfully."
    )


# ── Entry-point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
