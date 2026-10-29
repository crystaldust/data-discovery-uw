"""Vercel entrypoint: Vercel looks for a FastAPI instance named `app` in app/main.py."""

from ddm.config import load_web_config
from ddm.web.app import create_app

app = create_app(load_web_config())
