"""
dev_server.py – Runs only the Flask API on a fixed port for browser dev/preview.
No pywebview window is opened.  Visit http://127.0.0.1:5757 in a browser.
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
os.environ["GIS_PORT"] = "5757"

from webview_app import app          # import the Flask app object

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5757, debug=False, use_reloader=False)
