import logging
from datetime import datetime
import requests

logger = logging.getLogger(__name__)


def import_time():
    """Helper function to get current time."""
    return datetime.utcnow().isoformat()


def make_request(method, url, **kwargs):
    """
    Simple request wrapper.

    Args:
        method (str): HTTP method (GET, POST)
        url (str): URL to send the request to
        **kwargs: Additional arguments for the request

    Returns:
        requests.Response: The HTTP response

    Raises:
        ValueError: If method is not supported
        Exception: Any exception that occurs during the request
    """
    try:
        if method.lower() == 'post':
            return requests.post(url, **kwargs)
        elif method.lower() == 'get':
            return requests.get(url, **kwargs)
        raise ValueError(f"Unsupported method: {method}")
    except Exception as e:
        raise e
