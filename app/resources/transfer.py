import logging
import time
import uuid
from typing import Any
import os
from datetime import datetime
import json

from urllib.parse import urlparse
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
    """Orchestrates EDC asset transfers and data retrieval through Dataspace Protocol.

    Implements the full transfer lifecycle:
    - Contract negotiation initialization
    - Transfer process management
    - EDR (Endpoint Data Reference) retrieval
    - Data download
    - Saving data to files
    - Data plane communication
    - Status tracking and storage integration

    Attributes:
        timeout: Request timeout in seconds from app config
        api_key: EDC API key for authentication
        headers: Default headers for EDC API requests
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
        """Update orchestration process status in shared storage.

        Args:
            orchestration_id: UUID identifying the orchestration process
            status: Current status (INITIALIZING|ASSET_REGISTERED|COMPLETED|FAILED)
            **kwargs: Additional process metadata to store

        Thread-safe operation using orchestration_store_lock.
        """

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
        """Handle combined transfer request.

        Endpoint: POST /orchestrator/orchestrate

        Request Headers:
            X-Api-Key: Authentication token

        Returns:
            JSON response with transfer results or error details
        """
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
            parsed_url = urlparse(connector_address)
            connector_hostname = parsed_url.hostname

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
            # download_responses = []
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

                response_data = response.get_json()
                # print(response_data)
                transfer_id = response_data['response']['resource_id']

                # Retrieve data address for the initiated transfer
                data_address_response = self._retrieve_data_address(
                    connector_address,
                    transfer_id,
                    orchestration_id
                )

                if data_address_response.status_code >= 400:
                    return data_address_response

                # Extract critical components with validation
                edr_data = data_address_response.get_json()['response']
                logger.debug("EDR Data Received", extra={'edr_data': edr_data})

                if not edr_data.get('authorization'):
                    return create_error_response("Invalid EDR data","Missing authorization token",500)

                auth_token = edr_data.get('authorization')
                auth_type = edr_data.get('authType', 'bearer')
                endpoint = edr_data.get('endpoint', 'http://provider-qna-dataplane:11002/api/public')

                headers = {
                    'Authorization': auth_token,
                    'endpoint': endpoint,
                    'authType': auth_type
                }

                download_response = self._download_data(
                    url=f"http://{connector_hostname}/provider-qna/public/api/public",
                    headers=headers
                )
                if download_response.status_code >= 400:
                    return download_response

                # Attach downloaded data info
                # download_responses.append(download_response.get_json()['response']['content'])

                # In the post() method, replace:
                # download_responses.append(download_response.get_json()['response']['content'])

                content = download_response.get_json()['response']['content']
                try:
                    file_path = self._save_data_content(
                        content,
                        filename_prefix=f"file_{transfer_id}"
                    )
                    data_responses.append({
                        'transfer_id': transfer_id,
                        'storage_path': file_path,
                        'status': 'SAVED'
                    })
                except IOError as exc:
                    logger.error(f"Failed to save data for transfer {transfer_id}: {exc}")
                    return create_error_response(
                        f"Data storage failed for transfer {transfer_id}",
                        status_code=500
                    )

            self._update_orchestration_status(
                orchestration_id,
                status='COMPLETED',
                data_responses=data_responses
            )

            return create_success_response(
                status_code = 200,
                orchestration_id=orchestration_id,
                data={
                'status': "SUCCESS",
                'status_code': 200,
                'data_responses': data_responses
                }
            )

        except ValidationError as ve:
            logger.error(f"Validation error: {ve.messages}")
            return create_error_response("Invalid request format", details=ve.messages, status_code=400)
        except Exception as exc:
            logger.error(f"Combined transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=str(exc))
            return create_error_response(str(exc), status_code=500)

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

            return create_success_response(
                status_code=200,
                data={
                'resource_id': resource_id,
                'data': response_data,
                'status': success_status
                }
            )

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

                return create_success_response(
                    status_code = 200,
                    data=response.json()
                )

            except Exception as exc:
                logger.error(f"Attempt {attempt + 1} failed: {exc}")
                last_exception = exc

        self._update_orchestration_status(orchestration_id, status='FAILED', error=str(last_exception))
        return create_error_response(
            message="Data address retrieval failed",
            details=str(last_exception),
            status_code=500
        )

    def _download_data(self, url: str, headers: dict):
        """Execute data retrieval from provider data plane.

        Args:
            url: Provider endpoint URL from EDR
            headers: HTTP headers including Authorization token

        Returns:
            Response: Success with downloaded data or error details

        Raises:
            ConnectionError: Network communication failure
            Timeout: Request timeout
            HTTPError: Non-2xx response from provider
        """
        logger.info(f"Initiating data download")

        try:
            """
            # Extract the endpoint from the data address info
            endpoint = headers.get('endpoint')
            if not endpoint:
                raise ValueError("Data address does not contain a valid endpoint URL")

            headers = {}
            if authorization:
                headers['Authorization'] = authorization    
            """

            response = requests.get(url=url, headers=headers, timeout=self.timeout)  # stream=True
            response.raise_for_status()

            content = response.content
            content_type = response.headers.get('Content-Type')

            # Handle both JSON and binary data
            if 'application/json' in content_type:
                data = response.json()
            else:
                data = content.hex()  # For binary safety

            # print("content: ", data)
            # print("content type: ", content_type)

            return create_success_response(
                status_code=200,
                data={
                    'content': data,
                    'content_type': content_type
                }
            )

        except Exception as exc:
            logger.error(f"Data download failed: {str(exc)}")
            return create_error_response(
                message="Data download failed",
                details=str(exc),
                status_code=500
            )

    def _save_data_content(self, content, filename_prefix="data", directory=None):
        """Save downloaded content to persistent storage with proper serialization."""
        try:
            save_dir = directory or current_app.config.get('DATA_STORAGE_PATH', './data')
            os.makedirs(save_dir, exist_ok=True)

            # Serialize content based on type
            if isinstance(content, (dict, list)):
                file_ext = 'json'
                serialized = json.dumps(content, indent=2)
                write_mode = 'w'
            elif isinstance(content, bytes):
                file_ext = 'dat'
                serialized = content
                write_mode = 'wb'
            else:  # Assume string-like
                file_ext = 'txt'
                serialized = str(content)
                write_mode = 'w'

            # Generate filename with type indicator
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{filename_prefix}_{timestamp}_{uuid.uuid4().hex[:6]}.{file_ext}"
            file_path = os.path.join(save_dir, filename)

            with open(file_path, write_mode) as f:
                f.write(serialized)

            logger.info("Data saved successfully",
                        extra={'path': file_path, 'size': os.path.getsize(file_path)})
            return file_path

        except json.JSONDecodeError as jde:
            logger.error("JSON serialization failed", exc_info=True)
            raise ValueError("Invalid JSON content") from jde
        except TypeError as te:
            logger.error("Type mismatch during serialization", exc_info=True)
            raise ValueError(f"Unsupported content type: {type(content)}") from te
        except Exception as exc:
            logger.error("Data storage failed", exc_info=True)
            raise
