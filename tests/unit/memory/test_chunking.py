"""nox.memory.chunking: paragraph/heading-based chunking with stable hashes."""

from __future__ import annotations

from nox.memory.chunking import chunk_text


def test_chunk_text_splits_on_headings() -> None:
    # Each section is kept above chunking.MIN_CHARS so the small-trailing-chunk merge (tested
    # separately below) does not fold the second heading back into the first.
    body = (
        "# Heading One\n\nParagraph one has enough content to stand on its own as a chunk.\n\n"
        "## Heading Two\n\nParagraph two also has enough content to stand on its own as a chunk."
    )
    chunks = chunk_text(body, max_chars=10_000)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("# Heading One")
    assert chunks[1].text.startswith("## Heading Two")
    assert [c.ord for c in chunks] == [0, 1]


def test_chunk_text_packs_under_max_chars() -> None:
    paragraphs = [f"Paragraph {i} " + "x" * 50 for i in range(10)]
    body = "\n\n".join(paragraphs)
    chunks = chunk_text(body, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)  # a single long paragraph is kept whole


def test_chunk_hash_stable_for_identical_text() -> None:
    a = chunk_text("# H\n\nSame text here.", max_chars=1000)
    b = chunk_text("# H\n\nSame text here.", max_chars=1000)
    assert [c.hash for c in a] == [c.hash for c in b]


def test_chunk_hash_changes_with_content() -> None:
    a = chunk_text("# H\n\nOriginal.", max_chars=1000)
    b = chunk_text("# H\n\nChanged.", max_chars=1000)
    assert a[0].hash != b[0].hash


def test_small_trailing_chunk_merged_into_previous() -> None:
    body = "Paragraph one is reasonably long to stand alone as its own chunk of text.\n\n# X"
    chunks = chunk_text(body, max_chars=10_000)
    assert len(chunks) == 1
    assert "# X" in chunks[0].text


def test_empty_body() -> None:
    assert chunk_text("") == []
