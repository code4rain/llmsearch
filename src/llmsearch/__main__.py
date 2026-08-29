import argparse
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .rebuild import run_cli
from .web.app import _scheduled_sources, create_app, run_sync


def main():
    parser = argparse.ArgumentParser(prog="llmsearch")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--rebuild", action="store_true", help="기동 전 인덱스 재구축 (요약 md 재사용)")
    parser.add_argument("--yes", action="store_true", help="--rebuild 확인 프롬프트 생략")
    parser.add_argument("--force", action="store_true", help="--rebuild 시 미존재 폴더 경고 무시")
    args = parser.parse_args()
    load_dotenv()
    app = create_app(load_config(args.config))
    if args.rebuild:
        state = app.state.llmsearch
        code = run_cli(state, run_sync, _scheduled_sources(state), yes=args.yes, force=args.force)
        if code != 0:
            sys.exit(code)
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # 로컬 전용 (스펙 §10)


if __name__ == "__main__":
    main()
