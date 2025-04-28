from flask_restful import Resource, reqparse
from flask import current_app, request
import requests
import uuid
import logging
from app.utils.storage import orchestration_store, orchestration_store_lock
from app.utils.helpers import import_time, make_request
from app.utils.error_handling import create_error_response, create_success_response, handle_exceptions

logger = logging.getLogger(__name__)

# Define request parsers for input validation
service_parser = reqparse.RequestParser()
service_parser.add_argument('type', type=str, required=True, choices=['service'],
                           help="Only 'service' type is supported")
service_parser.add_argument('contract_agreement_id', type=str, required=True,
                           help="Missing 'contract_agreement_id' for service transfer")
service_parser.add_argument('counterPartyAddress', type=str, required=True,
                           help="Missing 'counterPartyAddress' for service transfer")
service_parser.add_argument('connectorId', type=str, required=False,
                           help="Connector ID of the provider")
service_parser.add_argument('properties', type=dict)

data_parser = reqparse.RequestParser()
data_parser.add_argument('type', type=str, required=True, choices=['data'],
                         help="Only 'data' type is supported")
data_parser.add_argument('endpoint', type=str, required=True,
                         help="Missing 'endpoint' for data registration")
data_parser.add_argument('auth_type', type=str, default='none')
data_parser.add_argument('properties', type=dict)


class TransferProcessResource(Resource):
    """Resource for initiating service or data transfers."""

    def __init__(self):
        """Initialize resource with default values."""
        self.timeout = None

    def _get_config_values(self):
        """Retrieve configuration values from app config."""
        self.timeout = current_app.config.get('REQUEST_TIMEOUT', 3)
        self.api_key = current_app.config.get('EDC_API_KEY')
        if not self.api_key:
            logger.warning("No EDC_API_KEY configured - API key validation will fail")

    def _update_orchestration_status(self, orchestration_id, status, **kwargs):
        """
        Update the status of an orchestration process.

        Args:
            orchestration_id (str): The orchestration ID to update
            status (str): The new status
            **kwargs: Additional fields to update
        """
        with orchestration_store_lock:
            if orchestration_id in orchestration_store:
                update_data = {
                    'status': status,
                    'updated_at': import_time()
                }
                update_data.update(kwargs)
                orchestration_store[orchestration_id].update(update_data)

    @handle_exceptions
    def post(self):
        """Initiate a transfer with the EDC or register a direct data endpoint."""
        self._get_config_values()
        logger.info("Received transfer process request")

        # Check for API key
        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('No API key provided', status_code=401)

        # Validate API key
        if api_key != self.api_key:
            return create_error_response('Invalid API key', status_code=403)

        orchestration_id = None

        try:
            request_data = request.get_json()
            if not request_data:
                return create_error_response('Missing or invalid JSON body', status_code=400)

            input_type = request_data.get('type')
            if input_type not in ['service', 'data']:
                return create_error_response("Only 'service' or 'data' types are supported", status_code=400)

            orchestration_id = str(uuid.uuid4())
            logger.info(f"Generated orchestration ID: {orchestration_id}")

            # Initialize orchestration process in store
            with orchestration_store_lock:
                orchestration_store[orchestration_id] = {
                    'status': 'INITIALIZING',
                    'type': input_type,
                    'original_request': request_data,
                    'created_at': import_time(),
                    'updated_at': import_time(),
                    'transfer_id': None
                }

            if input_type == 'service':
                args = service_parser.parse_args()
                return self._handle_service_transfer(args, orchestration_id)
            elif input_type == 'data':
                args = data_parser.parse_args()
                return self._handle_data_registration(args, orchestration_id)

        except requests.exceptions.Timeout as e:
            logger.error("Timeout connecting to EDC Consumer API")
            if orchestration_id:
                self._update_orchestration_status(orchestration_id, 'FAILED',
                                                  error="Connection to EDC Consumer API timed out after {self.timeout} seconds")
            return create_error_response(
                f"Connection to EDC Consumer API timed out after {self.timeout} seconds",
                orchestration_id=orchestration_id,
                status_code=504
            )

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error to EDC Consumer API: {str(e)}")
            if orchestration_id:
                self._update_orchestration_status(orchestration_id, 'FAILED',
                                                  error=f"Connection error: {str(e)}")
            return create_error_response(
                f"Failed to connect to EDC Consumer API",
                details=str(e),
                orchestration_id=orchestration_id,
                status_code=502
            )

        except Exception as e:
            logger.error(f"Error initiating transfer: {str(e)}", exc_info=True)
            if orchestration_id:
                self._update_orchestration_status(orchestration_id, 'FAILED', error=str(e))
            return create_error_response(
                str(e),
                orchestration_id=orchestration_id,
                status_code=500
            )

    def _handle_service_transfer(self, args, orchestration_id):
        """
        Handle a service transfer through EDC, compatible with MVD.

        Args:
            args (dict): The validated request arguments
            orchestration_id (str): The unique identifier for this orchestration process

        Returns:
            tuple: A tuple containing (response_body, status_code)
        """
        self._get_config_values()
        logger.info(f"Handling service transfer for orchestration ID: {orchestration_id}")

        try:
            contract_id = args.get('contract_agreement_id')
            connector_address = args.get('counterPartyAddress')

            headers = {
                'Content-Type': 'application/json',
                'X-Api-Key': self.api_key
            }

            """
            # MVD Default request
            payload = json.dumps({
                "@context": [
                    "https://w3id.org/edc/connector/management/v0.0.1"
                ],
                "assetId": "asset-1",
                "counterPartyAddress": "http://provider-qna-controlplane:8082/api/dsp",
                "connectorId": "did:web:provider-identityhub%3A7083:provider",
                "contractId": "a2c9c5bb-cb68-4378-95b2-109d30bac4e3",
                "dataDestination": {
                    "type": "HttpProxy"
                },
                "protocol": "dataspace-protocol-http",
                "transferType": "HttpData-PULL"
            })
            """

            # MVD compatible payload request
            transfer_request = {
                "@context": [
                    "https://w3id.org/edc/connector/management/v0.0.1"
                ],
                # "assetId": "asset-1",
                "counterPartyAddress": connector_address,
                "contractId": contract_id,
                "connectorId": "did:web:provider-identityhub%3A7083:provider",
                "dataDestination": {
                    "type": "HttpProxy"  # Changed from HttpData-PULL to HttpProxy for MVD
                },
                "protocol": "dataspace-protocol-http",
                "transferType": "HttpData-PULL"
            }

            properties = args.get('properties')
            if properties:
                transfer_request['properties'] = properties

            connector_id = args.get('connectorId')
            if connector_id:
                transfer_request['connectorId'] = connector_id

            # edc_url = f"{current_app.config['EDC_CONSUMER_API']}/transferprocesses"
            edc_url = f"{current_app.config['EDC_CONSUMER_API']}/consumer/cp/api/management/v3/transferprocesses"

            logger.info(f"Sending request to EDC Consumer at {edc_url}")
            response = make_request(
                'post',
                edc_url,
                json=transfer_request,
                headers=headers,
                timeout=self.timeout
            )

            if response.status_code == 200:
                transfer_data = response.json()
                transfer_id = transfer_data.get('@id')
                logger.info(f"Service transfer initiated successfully with transfer ID: {transfer_id}")
                self._update_orchestration_status(
                    orchestration_id,
                    'INITIATED',
                    type='service',
                    transfer_id=transfer_id,
                    edc_response=transfer_data
                )
                return create_success_response({
                    'orchestration_id': orchestration_id,
                    'type': 'service',
                    'process_status': 'INITIATED',
                    'transfer_id': transfer_id,
                    'edc_response': transfer_data
                })
            else:
                logger.error(f"EDC Consumer API returned status code {response.status_code}: {response.text}")
                self._update_orchestration_status(
                    orchestration_id,
                    'FAILED',
                    type='service',
                    error=response.text
                )
                return create_error_response(
                    f"EDC API returned status code {response.status_code}",
                    details=response.text,
                    orchestration_id=orchestration_id,
                    status_code=500
                )

        except Exception as e:
            logger.error(f"Error during service transfer: {str(e)}", exc_info=True)
            self._update_orchestration_status(
                orchestration_id,
                'FAILED',
                type='service',
                error=str(e)
            )
            return create_error_response(
                str(e),
                orchestration_id=orchestration_id,
                status_code=500
            )

    def _handle_data_registration(self, args, orchestration_id):
        """
        Handle direct data endpoint registration.

        Args:
            args (dict): The validated request arguments
            orchestration_id (str): The unique identifier for this orchestration process

        Returns:
            tuple: A tuple containing (response_body, status_code)
        """
        logger.info(f"Handling data registration for orchestration ID: {orchestration_id}")

        endpoint = args.get('endpoint')
        auth_type = args.get('auth_type', 'none')
        properties = args.get('properties', {})

        self._update_orchestration_status(
            orchestration_id,
            'REGISTERED',
            type='data',
            endpoint=endpoint,
            auth_type=auth_type,
            properties=properties
        )

        logger.info(f"Data endpoint registered successfully: {endpoint}")
        return create_success_response({
            'orchestration_id': orchestration_id,
            'type': 'data',
            'process_status': 'REGISTERED',
            'endpoint': endpoint,
            'auth_type': auth_type,
            'properties': properties
        })
