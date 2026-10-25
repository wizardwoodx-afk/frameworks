"""Retrieval over your own text — BM25, hashed embeddings, or both.

No network, no model, no vector database: a dependency-free index that scores
deterministically, so a retrieval failure is reproducible and a test can assert
on it.

    retriever = Retriever.from_directory("docs", extensions=(".md", ".txt"))
    agent = Agent("a", model=model, tools=[retriever.as_tool()])

    retriever.search("how are budgets enforced?", k=3)
    # [Result(passage=Passage(...), score=4.21), ...]

``BM25Index`` rewards rare terms and document length; ``VectorIndex`` catches
vocabulary mismatch through hashed term overlap; ``Retriever(strategy="hybrid")``
fuses the two rankings with reciprocal rank fusion, which is what you want when
you cannot tune anything.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..core.errors import RisterError

#: A small stopword list: enough to stop "the" dominating short queries.
STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have
    how i if in into is it its just may me might more most must my no not of on or
    our out over should so some such than that the their them then there these they
    this to too under up us was we were what when where which who will with would
    you your""".split()
)

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords removed, no stemming."""
    return [word for word in _WORD.findall((text or "").lower())
            if len(word) > 1 and word not in STOPWORDS]


@dataclass
class Passage:
    """One retrievable unit of text."""

    text: str
    id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = hashlib.blake2b(self.text.encode("utf-8"), digest_size=6).hexdigest()

    def citation(self) -> str:
        source = self.metadata.get("source", self.id)
        line = self.metadata.get("start_line")
        return f"{source}:{line}" if line else str(source)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "metadata": dict(self.metadata)}


@dataclass
class Result:
    """A passage with the score that surfaced it."""

    passage: Passage
    score: float
    rank: int = 0

    @property
    def text(self) -> str:
        return self.passage.text

    def __float__(self) -> float:
        return self.score


def chunk_text(
    text: str,
    *,
    size: int = 800,
    overlap: int = 100,
    metadata: Mapping[str, Any] | None = None,
) -> list[Passage]:
    """Split text into overlapping passages, preferring paragraph boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0:
        raise RisterError("chunk size must be positive")
    overlap = max(0, min(overlap, size - 1))

    paragraphs = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    pieces: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        while len(paragraph) > size:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.append(paragraph[:size])
            paragraph = paragraph[size - overlap:]
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= size:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = paragraph
    if buffer:
        pieces.append(buffer)

    out: list[Passage] = []
    for index, piece in enumerate(pieces):
        out.append(
            Passage(
                text=piece,
                metadata={**dict(metadata or {}), "chunk": index},
            )
        )
    return out


class BM25Index:
    """Okapi BM25 over an in-memory corpus."""

    def __init__(self, passages: Iterable[Passage], *, k1: float = 1.5,
                 b: float = 0.75) -> None:
        self.passages = list(passages)
        self.k1 = k1
        self.b = b
        self._docs = [tokenize(p.text) for p in self.passages]
        self._lengths = [len(doc) for doc in self._docs]
        self._avgdl = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in self._docs:
            counts: dict[str, int] = {}
            for token in doc:
                counts[token] = counts.get(token, 0) + 1
            self._tf.append(counts)
            for token in counts:
                df[token] = df.get(token, 0) + 1
        total = len(self._docs)
        self._idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in df.items()
        }

    def __len__(self) -> int:
        return len(self.passages)

    def search(self, query: str, k: int = 5) -> list[Result]:
        terms = tokenize(query)
        if not terms or not self.passages:
            return []
        scores: list[tuple[float, int]] = []
        for index, counts in enumerate(self._tf):
            length = self._lengths[index] or 1
            score = 0.0
            for term in terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                idf = self._idf.get(term, 0.0)
                denominator = freq + self.k1 * (
                    1 - self.b + self.b * length / (self._avgdl or 1)
                )
                score += idf * (freq * (self.k1 + 1)) / denominator
            if score > 0:
                scores.append((score, index))
        scores.sort(key=lambda item: (-item[0], item[1]))
        return [
            Result(self.passages[index], round(score, 6), rank + 1)
            for rank, (score, index) in enumerate(scores[:k])
        ]


class HashingEmbedder:
    """Deterministic bag-of-words embedding via BLAKE2b feature hashing.

    Stable across processes and Python versions (unlike ``hash()``), which
    matters because an index built in CI must score the same locally.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise RisterError("embedding dimension must be positive")
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            bucket = number % self.dim
            sign = 1.0 if (number >> 63) & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class VectorIndex:
    """Cosine similarity over hashed embeddings, with an inverted candidate set."""

    def __init__(self, passages: Iterable[Passage], *, dim: int = 256) -> None:
        self.passages = list(passages)
        self.embedder = HashingEmbedder(dim)
        self._vectors = [self.embedder.embed(p.text) for p in self.passages]
        self._terms = [set(tokenize(p.text)) for p in self.passages]
        self._postings: dict[str, set[int]] = {}
        for index, terms in enumerate(self._terms):
            for term in terms:
                self._postings.setdefault(term, set()).add(index)

    def __len__(self) -> int:
        return len(self.passages)

    def search(self, query: str, k: int = 5) -> list[Result]:
        terms = tokenize(query)
        if not terms or not self.passages:
            return []
        candidates: set[int] = set()
        for term in terms:
            candidates |= self._postings.get(term, set())
        if not candidates:
            return []
        query_vector = self.embedder.embed(query)
        scored: list[tuple[float, int]] = []
        for index in candidates:
            score = self.embedder.similarity(query_vector, self._vectors[index])
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            Result(self.passages[index], round(score, 6), rank + 1)
            for rank, (score, index) in enumerate(scored[:k])
        ]


class Retriever:
    """A corpus plus a ranking strategy, exposed as a tool for agents."""

    def __init__(
        self,
        passages: Iterable[Passage] = (),
        *,
        strategy: str = "hybrid",
        k: int = 5,
        dim: int = 256,
    ) -> None:
        if strategy not in ("bm25", "vector", "hybrid"):
            raise RisterError(
                f"unknown retrieval strategy {strategy!r}; "
                "expected 'bm25', 'vector' or 'hybrid'"
            )
        self.passages: list[Passage] = list(passages)
        self.strategy = strategy
        self.k = k
        self.dim = dim
        self._bm25: BM25Index | None = None
        self._vector: VectorIndex | None = None
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        if self.strategy in ("bm25", "hybrid"):
            self._bm25 = BM25Index(self.passages)
        if self.strategy in ("vector", "hybrid"):
            self._vector = VectorIndex(self.passages, dim=self.dim)

    def add(self, passages: Iterable[Passage] | str, *, metadata: Mapping[str, Any] | None = None,
            chunk: int | None = None) -> "Retriever":
        """Add passages (or raw text) and rebuild the index."""
        if isinstance(passages, str):
            added = chunk_text(passages, size=chunk or 800, metadata=metadata)
        else:
            added = list(passages)
        self.passages.extend(added)
        self._build()
        return self

    @classmethod
    def from_texts(cls, texts: Iterable[str], **kwargs: Any) -> "Retriever":
        chunk = kwargs.pop("chunk_size", 800)
        overlap = kwargs.pop("overlap", 100)
        passages: list[Passage] = []
        for index, text in enumerate(texts):
            passages.extend(
                chunk_text(text, size=chunk, overlap=overlap,
                           metadata={"source": f"text:{index}"})
            )
        return cls(passages, **kwargs)

    @classmethod
    def from_directory(
        cls,
        path: str | os.PathLike[str],
        *,
        extensions: Sequence[str] = (".md", ".txt", ".rst", ".py"),
        chunk_size: int = 800,
        limit_files: int = 500,
        **kwargs: Any,
    ) -> "Retriever":
        """Ingest text files under ``path``; skips binaries and huge files."""
        root = Path(path)
        if not root.exists():
            raise RisterError(f"no such path: {root}")
        files = [root] if root.is_file() else sorted(
            item for item in root.rglob("*") if item.is_file()
            and item.suffix.lower() in tuple(extensions)
        )
        passages: list[Passage] = []
        for item in files[:limit_files]:
            try:
                if item.stat().st_size > 1_000_000:
                    continue
                text = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                source = str(item.relative_to(root))
            except ValueError:
                source = item.name
            passages.extend(
                chunk_text(text, size=chunk_size, metadata={"source": source})
            )
        return cls(passages, **kwargs)

    # -- querying ----------------------------------------------------------
    def search(self, query: str, k: int | None = None) -> list[Result]:
        """Rank passages for a query using the configured strategy."""
        limit = k or self.k
        if not self.passages:
            return []
        if self.strategy == "bm25":
            return (self._bm25 or BM25Index(self.passages)).search(query, limit)
        if self.strategy == "vector":
            return (self._vector or VectorIndex(self.passages, dim=self.dim)).search(query, limit)
        return self._hybrid(query, limit)

    def _hybrid(self, query: str, k: int) -> list[Result]:
        """Reciprocal rank fusion of BM25 and vector rankings."""
        lexical = (self._bm25 or BM25Index(self.passages)).search(query, max(k * 4, 20))
        semantic = (self._vector or VectorIndex(self.passages, dim=self.dim)).search(
            query, max(k * 4, 20)
        )
        fused: dict[str, float] = {}
        by_id: dict[str, Result] = {}
        for ranking in (lexical, semantic):
            for rank, result in enumerate(ranking, start=1):
                key = result.passage.id
                fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
                by_id.setdefault(key, result)
        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        return [
            Result(by_id[key].passage, round(score, 6), rank)
            for rank, (key, score) in enumerate(ordered[:k], start=1)
        ]

    def context(self, query: str, k: int | None = None, *, max_chars: int = 4000) -> str:
        """Build a prompt-ready context block with citations."""
        results = self.search(query, k)
        if not results:
            return ""
        blocks: list[str] = []
        used = 0
        for result in results:
            block = f"[{result.rank}] {result.passage.citation()}\n{result.text.strip()}"
            if blocks and used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n".join(blocks)

    def as_tool(self, name: str = "search_docs", *, k: int | None = None,
                description: str = "") -> Any:
        """Expose retrieval as a tool agents can call."""
        from .tool import Tool

        limit = k or self.k
        retriever = self

        def search(query: str) -> str:
            results = retriever.search(query, limit)
            if not results:
                return "no relevant passages found"
            return "\n\n".join(
                f"[{r.rank}] {r.passage.citation()} (score {r.score:.2f})\n{r.text.strip()}"
                for r in results
            )

        return Tool(
            name=name,
            fn=search,
            description=description
            or "Search the local document index. Returns the most relevant passages "
                 "with citations.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for."}
                },
                "required": ["query"],
            },
            tags=("retrieval",),
        )

    def describe(self) -> dict[str, Any]:
        sources: dict[str, int] = {}
        for passage in self.passages:
            source = str(passage.metadata.get("source", "inline"))
            sources[source] = sources.get(source, 0) + 1
        return {
            "strategy": self.strategy,
            "passages": len(self.passages),
            "sources": len(sources),
            "chunks_per_source": dict(sorted(sources.items())[:20]),
        }

    def __len__(self) -> int:
        return len(self.passages)


__all__ = [
    "Passage",
    "Result",
    "Retriever",
    "BM25Index",
    "VectorIndex",
    "HashingEmbedder",
    "chunk_text",
    "tokenize",
    "STOPWORDS",
]
