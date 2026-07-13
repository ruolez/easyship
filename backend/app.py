from flask import Flask, jsonify

import config
import db


def create_app():
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    )

    db.run_migrations()

    import auth
    import backoffice_api
    import reports_api
    import settings_api
    import shipments_api
    import shipper_api
    import shopify_api

    app.register_blueprint(auth.bp)
    app.register_blueprint(settings_api.bp)
    app.register_blueprint(shipments_api.bp)
    app.register_blueprint(shipper_api.bp)
    app.register_blueprint(shopify_api.bp)
    app.register_blueprint(backoffice_api.bp)
    app.register_blueprint(reports_api.bp)

    app.teardown_appcontext(db.close_db)

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    return app
