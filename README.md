# EasyShip

Internal web app for generating shipping labels through the [Easyship API](https://developers.easyship.com/), pulling orders from **Shopify stores** and the local **BackOffice SQL Server**, and writing tracking numbers back to both.

## Stack

- **nginx** — single entry point on port **5557**, serves the vanilla JS frontend (no-cache) and proxies `/api/` to the backend
- **backend** — Python 3.12 / Flask / gunicorn (hot-reloads on code changes)
- **postgres** — PostgreSQL 16: users, settings, Shopify stores, shipment history, audit log
- Label PDFs are stored in the `labels` Docker volume and served by the backend

## Install on Ubuntu 24 (production)

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ruolez/easyship/main/install.sh)"
```

Interactive menu with three options:

- **Install** — installs Docker if missing, clones the repo to `/opt/easyship`, asks for the app port (default 5557) and initial admin password, generates random secrets in `.env`, builds and starts everything.
- **Update** — backs up the database to `/opt/easyship-backups/` (gzipped `pg_dump`), pulls the latest code from this repo, rebuilds containers. **Settings, users, shipment history and label PDFs are all preserved** (they live in Docker volumes, which updates never touch).
- **Remove** — stops and removes the app; asks separately before deleting data volumes, and offers a final backup first.

Non-interactive: `sudo bash install.sh install|update|remove`.

Restore a backup: `zcat easyship-db-<stamp>.sql.gz | docker compose --project-directory /opt/easyship exec -T postgres psql -U easyship easyship`

## Run locally (development)

```bash
cp .env.example .env   # then edit values
docker compose up -d --build
# open http://localhost:5557
```

Default login: **admin** / value of `ADMIN_INITIAL_PASSWORD` in `.env` (change it after first login in Settings → Change my password).

## Setup checklist (Settings page, admin only)

1. **Easyship API** — mode starts as *Sandbox*. Paste your sandbox token (starts with `sand_`), click *Test connection*. Switch the mode to *Production* and paste the production token only when ready to buy real labels. A SANDBOX/PRODUCTION badge is always visible in the top bar.
2. **Origin address** — your ship-from address, used for every rate request.
3. **BackOffice SQL Server** — host/port/database/user/password, *Test connection* runs `SELECT TOP 1` on `Invoices_tbl`.
4. **Shopify stores** — one row per store: name, `*.myshopify.com` domain, Admin API access token from a custom app with scopes:
   - `read_orders`, `read_customers`, `read_products`, `read_inventory`
   - `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders`
5. **Users** — create logins for warehouse staff. Every label records who bought it.

## Workflow

1. **Orders** page — Shopify tab (unfulfilled orders per store) or BackOffice tab (invoices with `Void=0` and empty `TrackingNo`). Click **Ship**.
2. **Ship** page — destination is pre-filled and editable → enter boxes (inches/pounds; BackOffice pre-seeds from `NoBoxes`/`TotalWeight`) → **Get rates** → pick a courier → **Buy label**.
3. On success: tracking number + printable 4x6 PDF label, and automatically:
   - Shopify: order fulfilled via `fulfillmentCreate` with tracking info (customer notified)
   - BackOffice: `UPDATE Invoices_tbl SET TrackingNo, ShippingCost`
   - A failed writeback never loses the label — retry from the Shipments page.
4. **Shipments** page — full history with user attribution, label reprint, void (cancels the shipment at Easyship), retry writeback.

## Notes

- Units: UI takes **lb/in**; the backend converts to kg/cm for Easyship.
- Multi-box: add boxes on the Ship page. If no rates come back, some couriers don't support multi-parcel — ship one box per shipment instead.
- Easyship API version `2024-09`; sandbox base URL `https://public-api-sandbox.easyship.com`.
- Shopify Admin GraphQL API version `2025-07` (queries validated against the schema).
- API tokens live in the Postgres `settings` table and are masked in all API responses; `.env` holds only infra secrets.

## Development

Backend code is volume-mounted with gunicorn `--reload`; frontend is served directly from `./frontend` — edit and refresh (no caching anywhere).

```bash
docker compose logs -f backend     # watch logs
docker compose exec postgres psql -U easyship   # inspect DB
docker compose restart backend     # force restart
```
