from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol


def _l2_normalize(vec: list[float]) -> list[float]:
	"""L2-normalize a vector to unit norm."""
	norm = math.sqrt(sum(v * v for v in vec)) or 1.0
	return [v / norm for v in vec]


class EmbeddingProvider(Protocol):
	def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddings:
	"""결정적 토큰 해시 임베딩 — 테스트·오프라인 개발용. 토큰 겹침 = 유사도."""

	def __init__(self, dim: int = 768):
		self.dim = dim

	def _one(self, text: str) -> list[float]:
		vec = [0.0] * self.dim
		for token in text.lower().split():
			h = int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big")
			vec[h % self.dim] += 1.0
		return _l2_normalize(vec)

	def embed(self, texts: list[str]) -> list[list[float]]:
		return [self._one(t) for t in texts]


class GeminiEmbeddings:
	"""Gemini 임베딩 API — 768차원 MRL 절단, 100건 배치 (스펙 §8)."""

	def __init__(self, model: str = "gemini-embedding-001", dim: int = 768):
		from google import genai  # 지연 import — 테스트 환경에 키 불필요

		self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
		self.model = model
		self.dim = dim

	def embed(self, texts: list[str]) -> list[list[float]]:
		from google.genai import types

		out: list[list[float]] = []
		for i in range(0, len(texts), 100):
			batch = texts[i : i + 100]
			resp = self.client.models.embed_content(
				model=self.model,
				contents=batch,
				config=types.EmbedContentConfig(output_dimensionality=self.dim),
			)
			out.extend([_l2_normalize(list(e.values)) for e in resp.embeddings])
		return out
