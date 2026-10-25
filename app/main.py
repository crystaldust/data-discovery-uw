"""Vercel entrypoint: Vercel looks for a FastAPI instance named `app` in app/main.py."""

from ddm.config import load_demo_mcp_token, load_readonly_database_url
from ddm.web.app import create_app

app = create_app(token=load_demo_mcp_token(), readonly_db_url=load_readonly_database_url())
