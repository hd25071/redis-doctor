"""Chunk the handbook by symptom and build the retrieval index.

One handbook file = one chunk = one symptom ("现象 → 排查步骤 → 常见原因").
Fixed-length chunking was rejected: it splits a symptom across chunks and makes
retrieval quality depend on where the paragraph boundaries happen to fall.

Run: ``python -m knowledge.build_index``
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tools.kb import Chunk

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = Path(__file__).resolve().parent / "handbook"
INDEX = Path(__file__).resolve().parent / "index" / "index.json"


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    front, _, body = rest.partition("\n---")
    return yaml.safe_load(front) or {}, body


def load_chunks(directory: Path | str = HANDBOOK) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(directory).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = _split_front_matter(text)
        chunks.append(
            Chunk(
                chunk_id=meta.get("id", path.stem),
                title=meta.get("title", path.stem),
                category=meta.get("category", "general"),
                # Always POSIX separators: the index is generated in CI and
                # compared byte-for-byte, so it must not depend on the platform
                # that happened to build it.
                source=meta.get("source", path.relative_to(REPO_ROOT).as_posix()),
                symptom=meta.get("symptom", ""),
                body=body.strip(),
                held_out=bool(meta.get("held_out", False)),
            )
        )
    return chunks


def build_index(
    directory: Path | str = HANDBOOK, output: Path | str = INDEX
) -> tuple[Path, list[Chunk]]:
    chunks = load_chunks(directory)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([c.to_json() for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return output, chunks


def main() -> int:
    output, chunks = build_index()
    by_category: dict[str, int] = {}
    for chunk in chunks:
        by_category[chunk.category] = by_category.get(chunk.category, 0) + 1
    print(f"indexed {len(chunks)} handbook chunks -> {output}")
    for category, count in sorted(by_category.items()):
        print(f"  {category:<24} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
