import argparse
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .web.app import create_app


def main():
    parser = argparse.ArgumentParser(prog="llmsearch")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8642)
    args = parser.parse_args()
    load_dotenv()
    app = create_app(load_config(args.config))
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # 로컬 전용 (스펙 §10)


if __name__ == "__main__":
    main()
