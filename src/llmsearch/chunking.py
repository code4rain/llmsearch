from __future__ import annotations


def chunk_text(text: str, max_chars: int = 800) -> list[str]:
	if max_chars <= 0:
		raise ValueError("max_chars must be positive")
	text = text.strip()
	if not text:
		return []
	paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
	chunks: list[str] = []
	buf = ""
	for p in paragraphs:
		while len(p) > max_chars:  # 문단 자체가 상한 초과 → 강제 분할
			if buf:
				chunks.append(buf)
				buf = ""
			chunks.append(p[:max_chars])
			p = p[max_chars:]
		if buf and len(buf) + 2 + len(p) > max_chars:
			chunks.append(buf)
			buf = p
		else:
			buf = f"{buf}\n\n{p}" if buf else p
	if buf:
		chunks.append(buf)
	return chunks
