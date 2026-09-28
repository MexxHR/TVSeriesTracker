"""WSGI adapter for a TLS-terminating reverse proxy / container platform."""
from backend.service import from_environment

application = from_environment()
