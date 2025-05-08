from flask import Flask
from flask_restful import Api
import os
import logging
import sys


def create_app():
    """Application factory pattern for Flask app."""

    # Configure logging first
    os.makedirs('logs', exist_ok=True)

    # Create a logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Create formatter
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')

    # Create file handler
    file_handler = logging.FileHandler('logs/orchestrator.log')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Log a test message
    logging.info('Logging system initialized with both file and console output')

    app = Flask(__name__)

    # Load configuration
    from app.config import Config
    app.config.from_object(Config)

    # Initialize Flask-RESTful API
    api = Api(app)

    # Register resources
    from app.resources.transfer import TransferProcessResource
    # from app.resources.dataaddress import DataAddressResource
    from app.resources.status import OrchestrationStatusResource, OrchestrationDetailResource

    # Transfer resources
    api.add_resource(TransferProcessResource, '/orchestrator/orchestrate')

    # Data address resources
    # api.add_resource(DataAddressResource, '/orchestrator/edrs/<string:transfer_id>/dataaddress')

    # Status resources
    api.add_resource(OrchestrationStatusResource, '/orchestrator/status')
    api.add_resource(OrchestrationDetailResource, '/orchestrator/status/<string:orchestration_id>')

    @app.route('/health')
    def health_check():
        return {'status': 'healthy'}, 200

    return app
