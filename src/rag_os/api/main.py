"""ASGI entry point: ``uvicorn rag_os.api.main:app``."""

from rag_os.api.app import create_app

app = create_app()
