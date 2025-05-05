import logging
from datetime import datetime, UTC
import requests

logger = logging.getLogger(__name__)


def import_time():
    """Helper function to get current time in UTC with timezone info."""
    return datetime.now(UTC).isoformat()


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
        kwargs.setdefault('timeout', 3)  # Default timeout if not specified
        response = requests.request(method.lower(), url, **kwargs)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed to {url}: {str(e)}")
        raise e
