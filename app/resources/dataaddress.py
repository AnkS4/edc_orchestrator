from flask_restful import Resource
from flask import current_app, request
import logging
from app.utils.storage import orchestration_store, orchestration_store_lock
from app.utils.helpers import import_time, make_request
from app.utils.error_handling import create_error_response, create_success_response, handle_exceptions

logger = logging.getLogger(__name__)


class DataAddressResource(Resource):
    """Resource for retrieving data addresses."""

    def __init__(self):
        """Initialize resource with default values."""
        self.timeout = None

    def _get_config_values(self):
        """Retrieve configuration values from app config."""
        self.timeout = current_app.config.get('REQUEST_TIMEOUT', 3)
        self.api_key = current_app.config.get('EDC_API_KEY')
        if not self.api_key:
            logger.warning("No EDC_API_KEY configured - API key validation will fail")

    @handle_exceptions
    def get(self, transfer_id):
        """
        Get the data address for a transfer.

        Args:
            transfer_id (str): The transfer ID to get the data address for

        Returns:
            tuple: A tuple containing (response_body, status_code)
        """
        self._get_config_values()

        # Check for API key
        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('No API key provided', status_code=401)

        # Validate API key
        if api_key != self.api_key:
            return create_error_response('Invalid API key', status_code=403)

        # args = parser.parse_args()

        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key
        }


        try:
            """
            # Default MVD API request
            url = "http://localhost/consumer/cp/api/management/v3/edrs/48366999-b68d-4df2-b471-a424511cec87/dataaddress"
            """

            response = make_request(
                'get',
                f"{current_app.config['EDC_CONSUMER_API']}/consumer/cp/api/management/v3/edrs/{transfer_id}/dataaddress",
                headers=headers,
                timeout=self.timeout
            )

            if response.status_code == 200:
                data_address = response.json()

                # Update orchestration status if process exists
                orchestration_id = None
                with orchestration_store_lock:
                    for orch_id, process in orchestration_store.items():
                        if process.get('transfer_id') == transfer_id:
                            orchestration_id = orch_id
                            # Update status
                            orchestration_store[orch_id].update({
                                'status': 'DATA_ADDRESS_RETRIEVED',
                                'updated_at': import_time()
                            })
                            break

                result = {
                    'transfer_id': transfer_id,
                    'data_address': data_address
                }

                if orchestration_id:
                    result['orchestration_id'] = orchestration_id

                return create_success_response(result)
            else:
                return create_error_response(
                    f"EDC API returned status code {response.status_code}",
                    details=response.text,
                    status_code=response.status_code
                )

        except Exception as e:
            logger.error(f"Error getting data address: {str(e)}")
            return create_error_response(str(e), status_code=500)
