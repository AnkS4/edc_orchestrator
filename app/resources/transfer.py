import logging
import time
import uuid
from typing import Any

# import json
import requests
from flask import current_app, request  # Response
from flask_restful import Resource
from marshmallow import Schema, fields, ValidationError, validates_schema

from app.utils.error_handling import (
    create_error_response,
    create_success_response,
    handle_exceptions,
)
from app.utils.helpers import import_time, make_request
from app.utils.storage import orchestration_store, orchestration_store_lock

logger = logging.getLogger(__name__)


class DataEntrySchema(Schema):
    """Schema for validating data entry parameters."""
    type = fields.Str(required=True, validate=lambda x: x in ["edc-asset"])
    counterPartyAddress = fields.Str(required=True)
    contractId = fields.Str(required=True)
    connectorId = fields.Str(required=True)

    @validates_schema
    def validate_type_fields(self, data, **kwargs):
        if data['type'] == "edc-asset":
            required = ['counterPartyAddress', 'contractId', 'connectorId']
            for field in required:
                if field not in data:
                    raise ValidationError(f"{field} is required for edc-asset type")


class ServiceSchema(Schema):
    """Schema for validating service transfer parameters."""
    counterPartyAddress = fields.Str(required=True)
    contractId = fields.Str(required=True)
    connectorId = fields.Str(required=True)


class CombinedTransferSchema(Schema):
    """Schema for validating combined transfer requests."""
    service = fields.Nested(ServiceSchema, required=False)
    data = fields.List(
        fields.Nested(DataEntrySchema),
        required=True,
        validate=lambda x: len(x) > 0
    )
    connectorAddress = fields.Str(required=True)


class TransferProcessResource(Resource):
    """Orchestrates EDC asset transfers and data address retrieval.

    Handles combined service/data transfers through the Eclipse Dataspace
    Connector API, managing the complete transfer lifecycle including:
    - Transfer process initiation
    - EDR data address retrieval
    - Storage API integration
    - Status tracking
    """
    def __init__(self):
        self.timeout = current_app.config['REQUEST_TIMEOUT']
        self.api_key = current_app.config['EDC_API_KEY']
        self.headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key,
        }
        self.data_address_delay = current_app.config['DATA_ADDRESS_DELAY']
        self.data_address_max_retries = current_app.config['DATA_ADDRESS_MAX_RETRIES']
        self.schema = CombinedTransferSchema()

    def _update_orchestration_status(
            self,
            orchestration_id: str,
            status: str,
            **kwargs: Any
    ):
        """Update orchestration process status in shared storage."""

        with orchestration_store_lock:
            process = orchestration_store.get(orchestration_id)
            if process:
                process.update({
                    'status': status,
                    'updated_at': import_time(),
                    **kwargs
                })

    @handle_exceptions
    def post(self):
        logger.info("Processing combined transfer request")
        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response(message='Missing API key', details=401)
        if api_key != self.api_key:
            return create_error_response(message='Invalid API key', details=403)

        orchestration_id = str(uuid.uuid4())
        try:
            data = self.schema.load(request.get_json())
            connector_address = data['connectorAddress']

            with orchestration_store_lock:
                orchestration_store[orchestration_id] = {
                    'status': 'INITIALIZING',
                    'type': 'combined',
                    'original_request': data,
                    'created_at': import_time(),
                    'updated_at': import_time(),
                    'transfer_id': None,
                    'data_entries': []
                }

            # Process data entries
            data_responses = []
            for entry in data['data']:
                # Initiate transfer process
                logger.info(f"Initiating EDC transfer process for Contract ID: {entry['contractId']}")
                response = self._handle_edc_request(
                    {**entry, 'type': 'edc-asset'},
                    edc_url=f"{connector_address}/api/management/v3/transferprocesses",
                    orchestration_id=orchestration_id,
                    transfer_type="HttpData-PULL",
                    success_status="ASSET_REGISTERED"
                )

                if response.status_code >= 400:
                    return response

                workflow_data = response.get_json()['workflow']
                transfer_id = workflow_data['resource_id']

                # Retrieve data address for the initiated transfer
                data_address_response = self._retrieve_data_address(
                    connector_address,
                    transfer_id,
                    orchestration_id
                )

                if data_address_response.status_code >= 400:
                    return data_address_response

                # Add data address to the workflow data
                workflow_data['access_info'] = data_address_response.get_json()['workflow']
                data_responses.append(workflow_data)

                # print(workflow_data['access_info'])

                # Save the Authorization header
                authorization = workflow_data['access_info']['authorization']

                download_response = self._download_data(
                    workflow_data['access_info'],
                    authorization=authorization
                )
                if download_response.status_code >= 400:
                    return download_response

                # Attach downloaded data info
                workflow_data['downloaded_data'] = download_response.get_json()['workflow']
                data_responses.append(workflow_data)

            """
            # Process service transfer
            service_response = self._handle_edc_request(
                data['service'],
                edc_url=f"{connector_address}/api/management/v3/transferprocesses",
                orchestration_id=orchestration_id,
                transfer_type="HttpData-PULL",
                success_status="SERVICE_INITIATED"
            )

            if service_response.status_code >= 400:
                return service_response

            # Retrieve data address for the service transfer
            service_workflow = service_response.get_json()['workflow']
            service_transfer_id = service_workflow['resource_id']

            service_data_address_response = self._retrieve_data_address(
                connector_address,
                service_transfer_id,
                orchestration_id
            )

            if service_data_address_response.status_code >= 400:
                return service_data_address_response

            # Add data address to the service workflow
            service_workflow['access_info'] = service_data_address_response.get_json()['workflow']

            # Send to storage API
            storage_payload = {
                'orchestration_id': orchestration_id,
                'service_data': service_workflow,
                'data_entries': data_responses
            }
            storage_response = self._send_to_storage(storage_payload)

            if storage_response.status_code >= 400:
                logger.error(f"Storage API failed: {storage_response.text}") 
            """

            self._update_orchestration_status(
                orchestration_id,
                status='COMPLETED',
                # service_response=service_workflow,
                data_responses=data_responses
            )

            return create_success_response(
                data={
                    # 'service': service_workflow,
                    'data': data_responses,
                    'status': 'COMPLETED'
                },
                orchestration_id=orchestration_id
            )

        except ValidationError as ve:
            logger.error(f"Validation error: {ve.messages}")
            return create_error_response("Invalid request format", details=ve.messages, status_code=400)
        except Exception as exc:
            logger.error(f"Combined transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=str(exc))
            return create_error_response(str(exc), status_code=500)

    """
    # Add new helper method
    def _send_to_storage(self, data):
        # Send processed data to storage API
        try:
            response = requests.post(
                current_app.config['STORAGE_API_URL'],
                json=data,
                headers={'Content-Type': 'application/json'},
                timeout=5
            )
            return response
        except Exception as e:
            logger.error(f"Storage API communication failed: {str(e)}")
            return Response(
                response=json.dumps({'error': 'Storage service unavailable'}),
                status=503,
                mimetype='application/json'
            )
    """

    def _handle_edc_request(self, args: dict, edc_url: str, orchestration_id: str,
                            transfer_type: str, success_status: str):
        """Unified handler for EDC API requests."""
        logger.info(f"Processing EDC request to {edc_url}")

        request_data = {
            "@context": ["https://w3id.org/edc/connector/management/v0.0.1"],
            "counterPartyAddress": args['counterPartyAddress'],
            "contractId": args['contractId'],
            "connectorId": args['connectorId'],
            "protocol": "dataspace-protocol-http"
        }

        if 'transferType' in args:
            request_data["transferType"] = args['transferType']
        else:
            request_data["transferType"] = transfer_type

        try:
            response = make_request(
                method='post',
                url=edc_url,
                json=request_data,
                headers=self.headers,
                timeout=self.timeout,
            )

            response.raise_for_status()
            response_data = response.json()
            resource_id = response_data.get('@id')

            if not resource_id:
                raise ValueError("Invalid EDC response: missing resource ID")

            self._update_orchestration_status(
                orchestration_id,
                success_status,
                resource_id=resource_id,
                edc_response=response_data,
            )

            return create_success_response({
                'resource_id': resource_id,
                'status': success_status,
            })

        except requests.HTTPError as exc:
            logger.error(f"EDC API error ({edc_url}): {exc.response.text}")
            self._update_orchestration_status(orchestration_id, status='FAILED', error=exc.response.text)
            return create_error_response(
                message="EDC API communication failed",
                details=exc.response.text,
                status_code=exc.response.status_code,
            )

    def _retrieve_data_address(self, connector_address: str, transfer_id: str, orchestration_id: str):
        """Retrieve data address for a transfer process."""
        logger.info(f"Processing EDR DataAddress retrieval for Transfer ID: {transfer_id}")
        last_exception = None
        for attempt in range(self.data_address_max_retries):
            try:
                url = f"{connector_address}/api/management/v3/edrs/{transfer_id}/dataaddress"
                if attempt > 0:
                    time.sleep(self.data_address_delay)

                response = make_request(
                    method='get',
                    url=url,
                    headers=self.headers,
                    timeout=self.timeout
                )
                response.raise_for_status()

                self._update_orchestration_status(
                    orchestration_id,
                    status='DATA_ADDRESS_RETRIEVED',
                    data_address=response.json()
                )

                return create_success_response(response.json())

            except Exception as exc:
                logger.error(f"Attempt {attempt + 1} failed: {exc}")
                last_exception = exc

        self._update_orchestration_status(orchestration_id, status='FAILED', error=str(last_exception))
        return create_error_response(
            message="Data address retrieval failed",
            details=str(last_exception),
            status_code=500
        )

    def _download_data(self, data_address_info: dict, authorization: str = None):
        """
        Download the data from the data address endpoint.
        :param data_address_info: The dictionary returned by _retrieve_data_address, expected to contain the data endpoint.
        :param authorization: The Authorization header value (e.g., 'Bearer <token>').
        :return: Response object with the downloaded data or error.
        """
        logger.info(f"Initiating data download")
        try:
            # Extract the endpoint from the data address info
            endpoint = data_address_info.get('endpoint')
            if not endpoint:
                raise ValueError("Data address does not contain a valid endpoint URL")

            headers = {}
            if authorization:
                headers['Authorization'] = authorization

            # Download file in streaming mode for large files
            response = requests.get(endpoint, headers=headers, timeout=self.timeout, stream=True)
            response.raise_for_status()

            # For demonstration, we read the first 1MB (customize as needed)
            content = response.raw.read(1024 * 1024)
            content_type = response.headers.get('Content-Type')

            return create_success_response(
                data={'content': content.hex(), 'content_type': content_type},
                status_code=200
            )
        except Exception as exc:
            logger.error(f"Data download failed: {str(exc)}")
            return create_error_response(
                message="Data download failed",
                details=str(exc),
                status_code=500
            )
