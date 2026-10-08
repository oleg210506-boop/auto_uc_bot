"""Single-process entry point. Railway supplies PORT."""
import os
import uvicorn
from .web import create_app
from .config import Config
from .maintenance import restore_from_env
if __name__ == "__main__":
    # Do not add reload or multiple workers: the payment processor must have one owner.
    config=Config.from_env()
    restore_from_env(config)
    # ProxyHeadersMiddleware lives in the app (including factory launches).
    # Do not interpret X-Forwarded-For twice in both Uvicorn and the app.
    uvicorn.run(create_app(config),host="0.0.0.0",port=int(os.getenv("PORT","8080")),workers=1,
                access_log=False,proxy_headers=False,limit_concurrency=40,
                timeout_keep_alive=5,timeout_graceful_shutdown=40)
