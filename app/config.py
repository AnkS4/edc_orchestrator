import os


class Config:
    """Application configuration settings."""
    # SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-for-dev-only')
    REQUEST_TIMEOUT = int(os.environ.get('REQUEST_TIMEOUT', 5))
    # MAX_RETRIES = int(os.environ.get('MAX_RETRIES', 2))
    DATA_ADDRESS_DELAY = int(os.environ.get('DATA_ADDRESS_DELAY', 1))
    DATA_ADDRESS_MAX_RETRIES = int(os.environ.get('DATA_ADDRESS_MAX_RETRIES', 3))

    # API keys and secrets
    EDC_API_KEY = os.environ.get('EDC_API_KEY', 'password')

    # Logging configuration
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

    # Cache settings
    CACHE_TTL = int(os.environ.get('CACHE_TTL', 5))

    # Storage API URL
    STORAGE_API_URL = 'http://localhost:5001/api/v1/store'
