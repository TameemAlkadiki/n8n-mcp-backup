# n8n MCP Backup Server

An MCP (Model Context Protocol) server that lets an AI assistant (Claude Desktop) back up, list, and restore n8n workflows and PostgreSQL databases — all through natural language commands.

> **"Take a backup of my n8n workflows"** → Claude calls the MCP tool → backup uploaded to S3.

---

## What it does

| Tool | Description |
|---|---|
| `take_backup` | Exports all n8n workflows and credentials to a timestamped JSON and uploads to S3 |
| `list_backups` | Lists available backups in S3, newest first with sizes |
| `restore_backup` | Downloads a backup from S3 and imports workflows back into n8n |
| `take_postgres_backup` | Streams a `pg_dump` directly to S3 via multipart upload (no temp file) |
| `list_postgres_backups` | Lists available Postgres backups in S3 |
| `restore_postgres_backup` | Downloads a Postgres backup from S3 and restores via `pg_restore` |

---

## Architecture

```
Claude Desktop
     │
     │  stdio (MCP)
     ▼
mcp-server (FastMCP)
     │
     ├──► n8n API        (export / import workflows)
     ├──► S3 Storage     (store / retrieve backup files)
     └──► PostgreSQL     (pg_dump / pg_restore)
```

All services run as Docker containers and communicate over an internal Docker network. S3 storage defaults to [Garage](https://garagehq.deuxfleurs.fr/) (self-hosted) but works with any S3-compatible backend (AWS S3, MinIO, etc.).

---

## Tech Stack

- **Python** + **FastMCP** — MCP server with stdio transport
- **boto3** — S3-compatible storage client
- **requests** — n8n REST API calls
- **Docker Compose** — orchestrates all services
- **Garage v1.3.1** — self-hosted S3-compatible object storage
- **PostgreSQL 16** — database with backup/restore support

---

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/TameemAlkadiki/n8n-mcp-backup.git
cd n8n-mcp-backup
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in all required values. See [Configuration](#configuration) below.

### 3. Set up Garage (S3 storage)

Place your `garage.toml` at `./garage/config/garage.toml`.  
See the [Garage quick-start guide](https://garagehq.deuxfleurs.fr/documentation/quick-start/) for setup instructions.

### 4. Start all services

```bash
docker compose up -d
```

### 5. Initialize Garage buckets

After first boot, create the S3 buckets:

```bash
# Apply cluster layout
docker exec garage /garage layout assign -z default -c 1G <NODE_ID>
docker exec garage /garage layout apply --version 1

# Create buckets
docker exec garage /garage bucket create n8n-backup
docker exec garage /garage bucket create postgres-backup

# Create access key and allow bucket access
docker exec garage /garage key create mcp-key
docker exec garage /garage bucket allow n8n-backup --read --write --key mcp-key
docker exec garage /garage bucket allow postgres-backup --read --write --key mcp-key
```

Copy the generated key ID and secret into your `.env` as `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`, then restart:

```bash
docker compose restart mcp-server
```

### 6. Connect Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "n8n-backup": {
      "command": "docker",
      "args": ["exec", "-i", "mcp_backup_server", "python", "server.py"]
    }
  }
}
```

Restart Claude Desktop. You can now say:
- *"Take a backup of my n8n workflows"*
- *"List available backups"*
- *"Restore the backup from yesterday"*
- *"Take a Postgres backup"*

---

## Configuration

All configuration is via environment variables in `.env`. Copy `.env.example` to get started.

| Variable | Required | Description |
|---|---|---|
| `N8N_ENCRYPTION_KEY` | ✅ | 32-byte hex key for n8n credential encryption |
| `N8N_API_KEY` | ✅ | n8n API key (Settings → n8n API → Create) |
| `N8N_URL` | optional | n8n base URL (default: `http://n8n:5678`) |
| `GARAGE_RPC_SECRET` | ✅ | Garage cluster RPC secret |
| `GARAGE_ADMIN_TOKEN` | ✅ | Garage admin API token (for web UI) |
| `S3_ACCESS_KEY_ID` | ✅ | S3 access key ID |
| `S3_SECRET_ACCESS_KEY` | ✅ | S3 secret access key |
| `S3_ENDPOINT` | optional | S3 endpoint (default: `http://garage:3900`) |
| `S3_REGION` | optional | S3 region (default: `us-east-1`) |
| `S3_BUCKET` | optional | Bucket for n8n backups (default: `n8n-backup`) |
| `S3_BUCKET_POSTGRES` | optional | Bucket for Postgres backups (default: `postgres-backup`) |
| `POSTGRES_DB` | ✅ | PostgreSQL database name |
| `POSTGRES_USER` | ✅ | PostgreSQL user |
| `POSTGRES_PASSWORD` | ✅ | PostgreSQL password |
| `PG_HOST` | optional | Postgres host (default: `postgres`) |
| `PG_PORT` | optional | Postgres port (default: `5432`) |

---

## Project Structure

```
n8n-mcp-backup/
├── server.py              # FastMCP server — all 6 tools
├── docker-compose.yml     # All services: n8n, mcp-server, garage, postgres
├── Dockerfile             # MCP server container build
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── garage/
    └── config/
        └── garage.toml    # Garage storage config (not included — see setup)
```

---

## Notes

- **Credentials are not restored** by `restore_backup` — n8n's API does not expose credential secrets on export. After restoring workflows, re-enter credentials manually in n8n Settings → Credentials.
- **Multipart upload** is used for Postgres backups, meaning no temporary files are written to disk — the dump streams directly from `pg_dump` to S3.
- The MCP server uses **stdio transport** — it is spawned per-session by Claude Desktop via `docker exec`, not run as a persistent HTTP service.

---

## License

MIT
