#!/usr/bin/env bash
# llmsearch 스킬 설치 (멱등):
#   1) $LLMSEARCH_HOME(기본 ~/.llmsearch)에 config.yaml/.env 초기화(없을 때만), env에 인터프리터 기록
#   2) $CLAUDE_SKILLS_DIR(기본 ~/.claude/skills)/llmsearch → 이 스킬 디렉터리 심볼릭 링크
#   3) status 스모크
# 사용: install.sh [--python PATH]   (기본 PATH = <repo>/.venv/bin/python)
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$SKILL_DIR/../.." && pwd)"
HOME_DIR="${LLMSEARCH_HOME:-$HOME/.llmsearch}"
SKILLS_ROOT="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
PY="$REPO/.venv/bin/python"
while [ $# -gt 0 ]; do
  case "$1" in
    --python) PY="$2"; shift 2 ;;
    *) echo "알 수 없는 인자: $1 (사용: install.sh [--python PATH])" >&2; exit 2 ;;
  esac
done

mkdir -p "$HOME_DIR" "$SKILLS_ROOT"
if [ ! -f "$HOME_DIR/config.yaml" ]; then
  cp "$REPO/config.example.yaml" "$HOME_DIR/config.yaml"
  echo "생성: $HOME_DIR/config.yaml — data_dir·watch_folders 등을 편집하세요"
fi
if [ ! -f "$HOME_DIR/.env" ]; then
  cp "$REPO/.env.example" "$HOME_DIR/.env"
  echo "생성: $HOME_DIR/.env — API 키를 기입하세요 (GEMINI_API_KEY 필수)"
fi
if [ ! -x "$PY" ]; then
  echo "경고: 인터프리터가 없습니다: $PY — README Setup으로 venv를 만들거나 --python PATH를 지정하세요" >&2
fi
printf 'LLMSEARCH_PYTHON=%s\n' "$PY" > "$HOME_DIR/env"

LINK="$SKILLS_ROOT/llmsearch"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "중단: $LINK 가 심볼릭 링크가 아닌 실제 디렉터리/파일입니다 — 직접 옮기거나 지운 뒤 다시 실행하세요" >&2
  exit 1
fi
ln -sfn "$SKILL_DIR" "$LINK"
echo "링크: $LINK -> $SKILL_DIR"

echo "--- status 스모크 ---"
if ! "$SKILL_DIR/scripts/llmsearch" status; then
  echo "(status 실패 — $HOME_DIR/config.yaml·.env를 편집한 뒤 '$SKILL_DIR/scripts/llmsearch status'로 확인하세요)"
fi
