# app/utils/error_handling.py
import logging
from functools import wraps
import requests

logger = logging.getLogger(__name__)


def create_error_response(message, details=None, status_code=500, orchestration_id=None):
    """
    Create a standardized error response format.

    Args:
        message (str): Main error message
        details (str, optional): Detailed error information
        status_code (int, optional): HTTP status code
        orchestration_id (str, optional): Associated orchestration ID if available

    Returns:
        tuple: (JSON response, HTTP status code)
    """
    response = {
        "status": "FAILED",
        "error": message
    }

    if details:
        response["details"] = details

    if orchestration_id:
        response["orchestration_id"] = orchestration_id

    return response, status_code


def create_success_response(data, status_code=200):
    """
    Create a standardized success response format.

    Args:
        data (dict): Response data
        status_code (int, optional): HTTP status code

    Returns:
        tuple: (JSON response, HTTP status code)
    """
    response = {
        "status": "SUCCESS"
    }
    response.update(data)
    return response, status_code


def handle_exceptions(f):
    """
    Decorator to handle exceptions and return standardized error responses.
    Maintains the same error response format across all API endpoints.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except requests.exceptions.Timeout as e:
            logger.error(f"Connection timeout: {str(e)}")
            return create_error_response(
                "Connection to EDC Consumer API timed out",
                str(e),
                504
            )
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {str(e)}")
            return create_error_response(
                "Failed to connect to EDC Consumer API",
                str(e),
                502
            )
        except ValueError as e:
            logger.error(f"Validation error: {str(e)}")
            return create_error_response(
                str(e),
                None,
                400
            )
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}", exc_info=True)
            return create_error_response(
                "An unexpected error occurred",
                str(e),
                500
            )

    return decorated_function
