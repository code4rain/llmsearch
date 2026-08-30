import argparse
import sys
from pathlib import Path

import uvicorn

from .config import ConfigNotFound, load_config, load_env, resolve_config_path
from .rebuild import run_cli
from .web.app import _scheduled_sources, create_app, run_sync


def main():
    parser = argparse.ArgumentParser(prog="llmsearch")
    parser.add_argument("--config", type=Path, default=None,
                        help="생략 시 $LLMSEARCH_CONFIG 또는 ~/.llmsearch/config.yaml")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--rebuild", action="store_true",
                        help="기동 전 인덱스 재구축 (요약 md 재사용, 일부 소스 실패해도 서버는 기동)")
    parser.add_argument("--yes", action="store_true", help="--rebuild 확인 프롬프트 생략")
    parser.add_argument("--force", action="store_true", help="--rebuild 시 미존재 폴더 경고 무시")
    args = parser.parse_args()
    load_env()
    try:
        config_path = resolve_config_path(args.config)
    except ConfigNotFound as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    app = create_app(load_config(config_path))
    if args.rebuild:
        state = app.state.llmsearch
        code = run_cli(state, run_sync, _scheduled_sources(state), yes=args.yes, force=args.force)
        if code == 2:  # 거부/취소만 종료 — code 1(일부 소스 재수집 실패)은 out()으로 로그만 남기고 서버는 계속 기동
            sys.exit(code)
    uvicorn.run(app, host="127.0.0.1", port=args.port)  # 로컬 전용 (스펙 §10)


if __name__ == "__main__":
    main()
