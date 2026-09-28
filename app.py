import os
from flask import Flask
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

    # Register blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(processing_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(update_bp)
    app.register_blueprint(photos_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(upload_bp)

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
