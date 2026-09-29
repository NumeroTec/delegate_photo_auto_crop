import os
from flask import Flask, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from routes.dashboard import dashboard_bp
from routes.processing import processing_bp
from routes.preview import preview_bp
from routes.update import update_bp
from routes.photos import photos_bp
from routes.logs import logs_bp
from routes.s3_upload import upload_bp


def create_app():
    app = Flask(__name__)

    # Load configuration from environment variables via Config
    app.config.from_object('config.Config')

    # Behind nginx/Apache on live (photo-crop.numerotech.com -> 127.0.0.1:5010):
    # trust X-Forwarded-* headers so url_for/redirect build the PUBLIC
    # https://photo-crop.numerotech.com/... URLs instead of http://127.0.0.1:5010/...
    # Requires nginx: proxy_set_header Host $host; X-Real-IP; X-Forwarded-For; X-Forwarded-Proto $scheme;
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Register blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(update_bp)
    app.register_blueprint(photos_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(upload_bp)

    # Site root (/) — no blueprint owns it, so redirect to dashboard
    # (visiting https://photo-crop.numerotech.com/ gave 404 before this).
    @app.route('/')
    def index():
        return redirect(url_for('dashboard.dashboard'))



    # Jinja filter for basename (used in photos.html)
    import os as _os
    @app.template_filter('basename')
    def basename_filter(path):
        return _os.path.basename(path) if path else ''

    return app

if __name__ == '__main__':
    flask_app = create_app()
    # Run in debug mode for development
    flask_app.run(host='0.0.0.0', port=5010, debug=True)
