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

- **Install** — installs Docker if missing, clones the repo to `/opt/easyship`, asks for the app port (default **80**), autogenerates all secrets (Postgres password, Flask secret key, and the initial admin password, which is printed at the end and saved in `.env`), builds and starts everything.
- **Update** — backs up the database to `/opt/easyship-backups/` (gzipped `pg_dump`), pulls the latest code from this repo, rebuilds containers. **Settings, users, shipment history and label PDFs are all preserved** (they live in Docker volumes, which updates never touch).
- **Remove** — stops and removes the app; asks separately before deleting data volumes, and offers a final backup first.

Non-interactive: `sudo bash install.sh install|update|remove`.

Restore a backup: `zcat easyship-db-<stamp>.sql.gz | docker compose --project-directory /opt/easyship exec -T postgres psql -U easyship easyship`

## Run locally (development)

```bash
cp .env.example .env   # then edit values
bash nginx/gen-certs.sh <lan-ip>   # self-signed TLS cert (nginx won't start without it)
docker compose up -d --build
# open http://localhost:5557 or https://localhost:5558
```

Default login: **admin** / value of `ADMIN_INITIAL_PASSWORD` in `.env` (change it after first login in Settings → Change my password).

## Setup checklist (Settings page, admin only)

1. **Shipping providers** (Providers tab) — enable one or more platforms; packers pick the one to ship with in the sidebar. A SANDBOX badge shows whenever the selected provider is in a test mode.
   - **Easyship** — mode starts as *Sandbox*. Paste your sandbox token (starts with `sand_`), click *Test connection*. Switch the mode to *Production* and paste the production token only when ready to buy real labels.
   - **GoShippo** / **EasyPost** — one API token; its prefix (`shippo_test_`/`shippo_live_`, `EZTK`/`EZAK`) sets the environment.
   - **ShipStation (API v2)** — paste the v2 API key from ShipStation → Settings → Account → API Settings (needs a Standard plan or higher; one active v2 key per account). ShipStation has no sandbox: switch **Test labels** to *On* to buy free test labels while verifying the flow (they are not valid for shipping, and their test tracking numbers are still written back to Shopify/BackOffice exactly like sandbox labels), then *Off* for live labels. Rates are fetched for every carrier connected to the account — hide unwanted services under *ShipStation shipping services*.
2. **Origin address** — your ship-from address, used for every rate request.
3. **BackOffice SQL Server** — host/port/database/user/password, *Test connection* runs `SELECT TOP 1` on `Invoices_tbl`.
4. **Shopify stores** — one row per store: name, `*.myshopify.com` domain, Admin API access token from a custom app with scopes:
   - `read_orders`, `read_customers`, `read_products`, `read_inventory`
   - `read_merchant_managed_fulfillment_orders`, `write_merchant_managed_fulfillment_orders`
5. **Users** — create logins for warehouse staff. Every label records who bought it.
6. **Shopify order tag rules** (Shipping tab, optional) — map an order tag to a preferred courier service and/or a signature requirement (signature / adult 21+). Tagged orders show the tags, the order note and the matched rule on the Ship page; the preferred rate is pre-selected and the signature requirement is sent to the provider (Easyship `delivery_confirmation`, Shippo `signature_confirmation`, EasyPost `delivery_confirmation`). Packers can override both before buying. Easyship only enables signature options for some accounts — if it rejects the option, rating falls back without it and the packer is warned.

## Workflow

1. **Orders** page — Shopify tab (unfulfilled orders per store) or BackOffice tab (invoices with `Void=0` and empty `TrackingNo`). Click **Ship**.
2. **Ship** page — destination is pre-filled and editable → enter boxes (inches/pounds; BackOffice pre-seeds from `NoBoxes`/`TotalWeight`) → **Get rates** → pick a courier → **Buy label**.
3. On success: tracking number + printable 4x6 PDF label, and automatically:
   - Shopify: order fulfilled via `fulfillmentCreate` with tracking info (customer notified)
   - BackOffice: `UPDATE Invoices_tbl SET TrackingNo, ShippingCost`
   - A failed writeback never loses the label — retry from the Shipments page.
4. **Shipments** page — full history with user attribution, label reprint, void (cancels the shipment at Easyship), retry writeback.

### Auto Mode (scan → weigh → rate → buy → print, no clicks)

On the Scan page tick **Auto Mode** and choose a Carrier, Service and Box for this station (stored in the browser, per shipping provider). From then on a scanned order opens the Ship page with the preset box, waits for a stable weight from the USB scale (or a typed weight + Enter), rates, buys the preset service — a Shopify tag rule's preferred service and signature requirement take precedence — and prints. Anything uncertain (preset service not offered, multi-box invoice, missing address, rate/buy errors) drops back to the normal manual form with a message; **Esc** or *Switch to manual* cancels any time before the label is bought. For truly click-free printing use the *Network thermal printer* or *Zebra Browser Print* print mode — the browser print dialog still needs one click.

### Zebra Browser Print (silent label printing)

For a USB-attached Zebra thermal printer on the packing station, set Settings → Label printing → **Zebra Browser Print (local USB printer)**. Labels then print silently the moment they're ready — no print dialog. Requirements on each packing station: the [Zebra Browser Print](https://www.zebra.com/us/en/software/printer-software/browser-print.html) app installed and running with the Zebra set as its default printer, and the Zebra's resolution (203 or 300 dpi) selected in Settings → Label printing. Any provider label format works: native ZPL is forwarded as-is, and PDF/PNG labels are rendered server-side to 4x6 ZPL at the printer DPI (the same conversion is used by the Network thermal printer mode). The first print asks you to accept the website inside the Browser Print app — click Accept once. Use Settings → *Test Zebra print (this computer)* to verify a station; reprints from the Parcels page go to the Zebra too.

### USB scale (auto-weigh)

The Ship page reads a USB HID scale via WebHID and live-fills the focused weight field with stable readings. Fairbanks Ultegra (e.g. 29824) is the primary/preferred device; any HID-class scale (Dymo, Mettler, Stamps.com) works. Requirements: Chrome/Edge and the **https** URL (WebHID needs a secure context — plain `http://<lan-ip>` won't show the scale widget; `http://localhost` is fine). One-time setup per station: open `https://<server-ip>:<https-port>`, accept the self-signed certificate warning, click **Connect scale** in the Packages card and pick the scale. After that it reconnects automatically on every page load. Typing a weight manually pauses auto-fill until the field is refocused.

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
