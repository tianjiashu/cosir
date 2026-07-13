"""后端 ASGI 应用入口。"""

from app.api.app import create_app

app = create_app()
