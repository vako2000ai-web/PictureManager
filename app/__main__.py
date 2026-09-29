import argparse
import threading
import webbrowser

import uvicorn

from .main import create_app

HOST = "127.0.0.1"  # только localhost: из сети приложение недоступно


def main() -> None:
    parser = argparse.ArgumentParser(description="PictureManager")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None, help="каталог базы данных и кэша миниатюр")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    app = create_app(args.data_dir)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{args.port}/")).start()
    uvicorn.run(app, host=HOST, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
