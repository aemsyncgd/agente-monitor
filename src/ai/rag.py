# src/ai/rag.py
"""
RAG (Retrieval-Augmented Generation) para el agente WISP.

Utiliza ChromaDB como vector store ligero y embebible.
Indexa: topología de red, manuales, histórico de fallas, runbooks.
"""
import os
import json
import time
import logging
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

_chromadb = None
_chroma_available = False


def _get_chromadb():
    global _chromadb, _chroma_available
    if _chromadb is not None:
        return _chromadb
    try:
        import chromadb
        _chromadb = chromadb
        _chroma_available = True
        return _chromadb
    except ImportError:
        _chroma_available = False
        logger.warning("chromadb not installed — RAG disabled")
        return None


class RAGStore:
    """Vector store para conocimiento de la red WISP."""

    def __init__(self, collection_name: str = "wisp_knowledge",
                 persist_directory: str = "data/chromadb",
                 chunk_size: int = 500,
                 chunk_overlap: int = 50):
        self.collection_name = collection_name
        self.persist_directory = persist_directory
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._client = None
        self._collection = None
        self._ready = False

        self._init_store()

    def _init_store(self) -> None:
        chroma = _get_chromadb()
        if not chroma:
            return

        try:
            Path(self.persist_directory).mkdir(parents=True, exist_ok=True)
            self._client = chroma.PersistentClient(path=self.persist_directory)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            self._ready = True
            count = self._collection.count()
            logger.info(f"RAG initialized: {count} documents in '{self.collection_name}'")
        except Exception as e:
            logger.error(f"RAG init failed: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    def index_text(self, text: str, metadata: Optional[Dict] = None,
                   doc_id: Optional[str] = None) -> int:
        if not self._ready:
            return 0

        chunks = self._chunk_text(text)
        if not chunks:
            return 0

        ids = []
        documents = []
        metadatas = []

        for i, chunk in enumerate(chunks):
            chunk_id = doc_id or hashlib.md5(
                f"{text[:100]}:{i}".encode()
            ).hexdigest()[:16]

            meta = (metadata or {}).copy()
            meta["chunk_index"] = i
            meta["chunk_total"] = len(chunks)

            ids.append(chunk_id)
            documents.append(chunk)
            metadatas.append(meta)

        try:
            self._collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
            logger.info(f"Indexed {len(chunks)} chunks (doc={doc_id})")
            return len(chunks)
        except Exception as e:
            logger.error(f"RAG index failed: {e}")
            return 0

    def index_file(self, file_path: str, metadata: Optional[Dict] = None) -> int:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"File not found for RAG: {file_path}")
            return 0

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return 0

        meta = (metadata or {}).copy()
        meta["source"] = str(path)
        meta["filename"] = path.name

        doc_id = hashlib.md5(f"{file_path}:{path.stat().st_mtime}".encode()).hexdigest()[:16]
        return self.index_text(text, metadata=meta, doc_id=doc_id)

    def index_directory(self, directory: str, extensions: Optional[List[str]] = None,
                        metadata: Optional[Dict] = None) -> int:
        exts = extensions or [".txt", ".md", ".json", ".yaml", ".yml", ".conf"]
        total = 0

        for ext in exts:
            for path in Path(directory).rglob(f"*{ext}"):
                if path.is_file():
                    total += self.index_file(str(path), metadata=metadata)

        return total

    def retrieve(self, query: str, n_results: int = 5,
                 filters: Optional[Dict] = None) -> List[Dict]:
        if not self._ready:
            return []

        try:
            kwargs = {
                "query_texts": [query],
                "n_results": min(n_results, self._collection.count() or 1),
            }
            if filters:
                kwargs["where"] = filters

            results = self._collection.query(**kwargs)

            documents = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            output = []
            for doc, meta, dist in zip(documents, metadatas, distances):
                output.append({
                    "content": doc,
                    "metadata": meta,
                    "score": 1 - dist,
                })
            return output
        except Exception as e:
            logger.error(f"RAG retrieve failed: {e}")
            return []

    def delete_source(self, source: str) -> int:
        if not self._ready:
            return 0

        try:
            results = self._collection.get(
                where={"source": source}
            )
            if results["ids"]:
                self._collection.delete(ids=results["ids"])
                return len(results["ids"])
        except Exception as e:
            logger.error(f"RAG delete failed: {e}")
        return 0

    def count(self) -> int:
        if not self._ready:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def stats(self) -> Dict[str, Any]:
        return {
            "ready": self._ready,
            "collection": self.collection_name,
            "document_count": self.count(),
            "persist_directory": self.persist_directory,
        }

    def _chunk_text(self, text: str) -> List[str]:
        if not text.strip():
            return []
        if len(text) <= self.chunk_size:
            return [text.strip()]

        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            if end < len(text):
                for sep in ["\n\n", "\n", ". ", " "]:
                    last_sep = text.rfind(sep, start + self.chunk_size // 2, end + 50)
                    if last_sep > start:
                        end = last_sep + len(sep)
                        break
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - self.chunk_overlap

        return chunks


_rag_store: Optional[RAGStore] = None
_rag_lock = None


def get_rag_store(**kwargs) -> RAGStore:
    global _rag_store, _rag_lock
    if _rag_lock is None:
        import threading
        _rag_lock = threading.Lock()

    if _rag_store is None:
        with _rag_lock:
            if _rag_store is None:
                from ..config_ia import load_config_ia
                config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
                try:
                    config = load_config_ia(config_path)
                    _rag_store = RAGStore(
                        collection_name=config.rag.collection_name,
                        persist_directory=config.rag.persist_directory,
                        chunk_size=config.rag.chunk_size,
                        chunk_overlap=config.rag.chunk_overlap,
                    )
                except Exception:
                    _rag_store = RAGStore(**kwargs)
    return _rag_store
