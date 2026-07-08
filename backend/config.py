import os

SECRET_KEY = os.environ["SECRET_KEY"]
ADMIN_INITIAL_PASSWORD = os.environ.get("ADMIN_INITIAL_PASSWORD", "admin")

POSTGRES = {
    "host": os.environ.get("POSTGRES_HOST", "postgres"),
    "dbname": os.environ.get("POSTGRES_DB", "easyship"),
    "user": os.environ.get("POSTGRES_USER", "easyship"),
    "password": os.environ["POSTGRES_PASSWORD"],
}

LABELS_DIR = os.environ.get("LABELS_DIR", "/data/labels")

EASYSHIP_BASE_URLS = {
    "production": "https://public-api.easyship.com/2024-09",
    "sandbox": "https://public-api-sandbox.easyship.com/2024-09",
}

SHOPIFY_API_VERSION = "2025-07"
