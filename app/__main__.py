"""Запуск: python -m app  (сервер слушает только 127.0.0.1)."""
import argparse
import threading
import webbrowser

import uvicorn

from .main import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="PictureManager: менеджер фотоархива")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=None, help="каталог для базы, миниатюр и настроек (по умолчанию ./data)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    app = create_app(args.data)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
