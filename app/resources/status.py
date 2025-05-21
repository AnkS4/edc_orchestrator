import logging
import time

from flask import current_app
from flask_restful import Resource, reqparse

from app.utils.error_handling import create_error_response, create_success_response, handle_exceptions
from app.utils.helpers import make_request
from app.utils.storage import orchestration_store, orchestration_store_lock

logger = logging.getLogger(__name__)

detail_parser = reqparse.RequestParser()
detail_parser.add_argument(
    'clientIp',
    type=str,
    required=True,
    help="Client IP address must be provided"
)


def get_detailed_orchestration_data(orchestration_id, process):
    """
    Helper function to get detailed orchestration data for a given process.

    Args:
        orchestration_id (str): The orchestration ID
        process (dict): The orchestration process data

    Returns:
        dict: Detailed orchestration data
    """
    response_data = {
        'orchestration_id': orchestration_id,
        'process_status': process['status'],
        'created_at': process['created_at'],
        'updated_at': process['updated_at'],
        'properties': process.get('properties', {})
    }

    return response_data


class OrchestrationStatusResource(Resource):
    """Resource for retrieving status of all orchestration processes."""

    @handle_exceptions
    def get(self):
        """Get status of all orchestration processes with detailed information."""
        try:
            with orchestration_store_lock:
                # Create detailed data for each orchestration process
                detailed_processes = {}
                for orch_id, process in orchestration_store.items():
                    detailed_processes[orch_id] = get_detailed_orchestration_data(orch_id, process)

                return create_success_response(data={
                    'orchestration_processes': detailed_processes
                })
        except Exception as e:
            logger.error(f"Error retrieving all orchestration statuses: {str(e)}")
            return create_error_response(
                'Error retrieving orchestration statuses',
                str(e),
                status_code=500
            )


class OrchestrationDetailResource(Resource):
    """Resource for retrieving status of a specific orchestration process."""

    def __init__(self):
        """Initialize resource with default values."""
        self.timeout = None
        self._status_cache = {}
        self._cache_ttl = current_app.config.get('CACHE_TTL', 5)  # seconds
        self.parser = detail_parser
        self.api_key = current_app.config['EDC_API_KEY']

    def _get_config_values(self):
        """Retrieve configuration values from app config."""
        self.timeout = current_app.config.get('REQUEST_TIMEOUT', 3)

    @handle_exceptions
    def get(self, orchestration_id):
        """Get detailed status of a specific orchestration process."""
        try:
            self._get_config_values()

            # Parse and validate clientIp
            args = self.parser.parse_args()
            client_ip = args['clientIp']

            if not orchestration_id:
                return create_error_response('Invalid orchestration ID', status_code=400)

            with orchestration_store_lock:
                if orchestration_id not in orchestration_store:
                    return create_error_response(
                        'Orchestration process not found',
                        f"Process with ID '{orchestration_id}' does not exist",
                        status_code=404
                    )

                process = orchestration_store[orchestration_id]

                # Get basic detailed data
                response_data = get_detailed_orchestration_data(orchestration_id, process)

                # Add EDC transfer status if applicable
                transfer_id = process.get('transfer_id')
                process_type = response_data.get('type')

                if process_type == 'service' and transfer_id:
                    edc_response = self._get_edc_transfer_status(transfer_id, client_ip)
                    if edc_response:
                        response_data['transfer_status'] = edc_response.get('state')
                        response_data['edc_response'] = edc_response

            return create_success_response({
                'orchestration_processes': {
                    orchestration_id: response_data
                }
            })

        except Exception as e:
            logger.error(f"Error retrieving orchestration status: {str(e)}", exc_info=True)
            return create_error_response(
                'Error retrieving orchestration status',
                str(e),
                status_code=500
            )

    def _get_edc_transfer_status(self, transfer_id, client_ip):
        """
        Retrieve transfer status from EDC Consumer API with caching.

        Args:
            transfer_id (str): The transfer ID to check
            client_ip (str): Client IP address for EDC endpoint

        Returns:
            dict: The EDC response or None if error occurred
        """
        current_time = time.time()

        # Check cache
        if transfer_id in self._status_cache:
            cached_time, cached_data = self._status_cache[transfer_id]
            if current_time - cached_time < self._cache_ttl:
                return cached_data

        try:
            # Use client_ip to construct the URL (consistent with transfer.py)
            edc_url = f"http://{client_ip}/consumer/cp/api/management/v3/transferprocesses/{transfer_id}"

            response = make_request(
                'get',
                edc_url,
                headers={
                    'Content-Type': 'application/json',
                    'X-Api-Key': self.api_key
                },
                timeout=self.timeout
            )

            if response.status_code == 200:
                data = response.json()
                # Update cache
                self._status_cache[transfer_id] = (current_time, data)
                return data

            logger.warning(f"EDC API returned status {response.status_code} for transfer {transfer_id}")
            return None

        except Exception as e:
            logger.error(f"Error fetching EDC transfer status: {str(e)}")
            return None
