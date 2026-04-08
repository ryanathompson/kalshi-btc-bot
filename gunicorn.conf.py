"""
gunicorn.conf.py â Gunicorn configuration for Kalshi BTC bot.

The bot background thread MUST be started inside the forked worker, not in
the master process.  Python threads don't survive fork(): any thread started
during the master's module import will only run in the master, while the
worker that actually serves HTTP ends up with no bot thread running.

post_worker_init fires after the worker has been forked AND has imported the
app, so starting the thread here guarantees it runs in the right process.
"""

import os
import threading


def post_worker_init(worker):
    """Start the bot thread inside the forked worker process."""
    if not os.getenv("KALSHI_API_KEY_ID"):
        print("[gunicorn] KALSHI_API_KEY_ID not set â skipping bot thread.", flush=True)
        return

    # app is already imported by the worker by this point; grab from sys.modules
    import sys
    app_module = sys.modules.get("app")
    if app_module is None:
        import app as app_module

    print("[gunicorn] post_worker_init: starting bot thread in worker...", flush=True)
    t = threading.Thread(target=app_module._bot_thread, daemon=True)
    t.start()
