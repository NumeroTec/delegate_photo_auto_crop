import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    # Database configuration
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = int(os.getenv('DB_PORT', 3306))
    DB_NAME = os.getenv('DB_NAME')
    DB_USER = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')

    # Photo base URL
    PHOTO_BASE_URL = os.getenv('PHOTO_BASE_URL', '')

    # Output dimensions
    OUTPUT_WIDTH = int(os.getenv('OUTPUT_WIDTH', 354))
    OUTPUT_HEIGHT = int(os.getenv('OUTPUT_HEIGHT', 472))

    # Crop configuration
    TOP_PADDING_RATIO = float(os.getenv('TOP_PADDING_RATIO', 0.20))
    FACE_TARGET_RATIO = float(os.getenv('FACE_TARGET_RATIO', 0.45))
    SHOULDER_PADDING = float(os.getenv('SHOULDER_PADDING', 1.5))

    # Processing settings
    BATCH_SIZE = int(os.getenv('BATCH_SIZE', 50))

    # Flask secret key for session
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')

    # AWS S3 upload configuration
    AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID', '')
    AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY', '')
    AWS_REGION = os.getenv('AWS_REGION', 'ap-southeast-1')
    S3_BUCKET = os.getenv('S3_BUCKET', '')
    S3_PREFIX = os.getenv('S3_PREFIX', 'delegate_photo')
    S3_DRY_RUN = os.getenv('S3_DRY_RUN', '0') == '1'
