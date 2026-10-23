"""Zone "bronze" (brute) du data lake : on conserve les réponses brutes des API, telles que reçues.

Organisation des fichiers (partitionnement à la Hive) :
    bronze/<source>/<dataset>/date=YYYY-MM-DD/<HHMMSS>_<run_label>[_partN].jsonl.gz

Chaque ligne JSONL = une réponse d'API brute + ses métadonnées de requête.
Fichiers compressés en gzip et découpés en parties pour borner le tampon mémoire.
Backend RAW_STORAGE : minio (default) | local | none (development only).
"""

import gzip
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from .lake import lake_bucket, s3_client
from pathlib import Path

from .logging_utils import get_logger

log = get_logger("raw_storage")

PART_MAX_BYTES = 40 * 1024 * 1024  # taille max non compressée d'une partie


class RawWriter:
    def __init__(self, source: str, dataset: str, run_label: str):
        self.source = source
        self.dataset = dataset
        self.run_label = run_label
        self.backend = os.getenv("RAW_STORAGE", "minio").lower()
        if self.backend not in {"minio", "local", "none"}:
            raise ValueError("RAW_STORAGE must be minio, local or none")
        self._run_id = uuid4().hex
        self._lines: list[str] = []
        self._size = 0
        self._part = 0
        self._first_path: str | None = None
        self._written_parts = 0

    def add(self, request: dict, payload: dict) -> None:
        if self.backend == "none":
            return
        record = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "request": request,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False)
        self._lines.append(line)
        self._size += len(line.encode("utf-8"))
        if self._size >= PART_MAX_BYTES:
            self._write_part()

    def flush(self) -> str | None:
        """Persist pending bytes; return the run provenance, including earlier flushes."""
        if self._lines:
            self._write_part()
        if self._written_parts == 0:
            return None
        return (self._first_path if self._written_parts == 1 else
                f"{self._first_path} (+{self._written_parts - 1} parties)")

    def _write_part(self) -> None:
        """Persist one buffer; callers flush at API-response boundaries for provenance."""
        self._part += 1
        now = datetime.now(timezone.utc)
        prefix = "bronze"
        path = (f"{prefix}/{self.source}/{self.dataset}/date={now:%Y-%m-%d}/"
                f"{now:%H%M%S}_{self.run_label}_{self._run_id}_part{self._part}.jsonl.gz")
        body = gzip.compress(("\n".join(self._lines) + "\n").encode("utf-8"))

        if self.backend == "local":
            target = Path(os.getenv("RAW_LOCAL_DIR", "data")) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        else:
            bucket = lake_bucket()
            s3_client().put_object(Bucket=bucket, Key=path, Body=body, ContentType="application/gzip")
            path = f"s3://{bucket}/{path}"
        log.info("raw: %d réponses écrites dans %s", len(self._lines), path)
        self._first_path = self._first_path or path
        self._written_parts += 1
        # Clear only after successful persistence. A failed lake write fails ingestion.
        self._lines.clear()
        self._size = 0
