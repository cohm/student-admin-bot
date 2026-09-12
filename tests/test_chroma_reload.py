"""Why scripts/maintain.sh restarts the stack after a reindex.

A reindex run by another process is NOT picked up by an already-running
service. Chroma reads a collection's HNSW segment into memory on first query
and does not reload it; `count()` keeps updating (that reads SQLite), so the
collection looks fresh while `query()` keeps returning the old vectors.

That combination is the dangerous part: the weekly eval runs in a fresh
container and reports the new index as green, while `web` and `mattermost`
answer students from the pre-reindex one. Nothing anywhere says so.

This test pins the behaviour. If a chromadb upgrade makes running processes
pick up external writes, it fails — and the restart in maintain.sh can be
reconsidered deliberately rather than removed on a hunch.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import chromadb
from chromadb.config import Settings


SETTINGS = Settings(anonymized_telemetry=False)
OLD = [1.0, 0.0, 0.0]
NEW = [0.9, 0.1, 0.0]

# Stands in for `scripts/reindex.py`: a separate process writing the same
# persist directory the services have open.
_REINDEXER = textwrap.dedent(
    """
    import sys
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(path=sys.argv[1],
                                       settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection("corpus", metadata={"hnsw:space": "cosine"})
    coll.upsert(ids=["new-1"], embeddings=[[0.9, 0.1, 0.0]], documents=["freshly scraped"])
    print(coll.count())
    """
)


def _collection(path):
    client = chromadb.PersistentClient(path=str(path), settings=SETTINGS)
    return client.get_or_create_collection("corpus", metadata={"hnsw:space": "cosine"})


def test_a_running_process_keeps_serving_the_pre_reindex_index(tmp_path):
    persist = tmp_path / "chroma"

    # A service starts and answers one question — this is what loads the
    # segment. Without this first query the process would have nothing cached.
    serving = _collection(persist)
    serving.upsert(ids=["old-1"], embeddings=[OLD], documents=["before the reindex"])
    assert serving.query(query_embeddings=[OLD], n_results=5)["ids"] == [["old-1"]]

    # Meanwhile, the weekly job reindexes.
    written = subprocess.run(
        [sys.executable, "-c", _REINDEXER, str(persist)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert written.stdout.strip() == "2"

    # The count goes up: that is read from SQLite, and is why "the index looks
    # fine" is a trap here.
    assert serving.count() == 2

    # ...but the vectors served are still the old ones.
    still_served = serving.query(query_embeddings=[NEW], n_results=5)["ids"][0]
    assert "new-1" not in still_served, (
        "chromadb now reloads external writes — re-examine the restart step in "
        "scripts/maintain.sh before relying on it"
    )

    # A fresh PersistentClient in the SAME process is not a workaround:
    # chromadb caches the System per path, so it is the same segment.
    assert "new-1" not in _collection(persist).query(query_embeddings=[NEW], n_results=5)["ids"][0]


def test_a_restarted_process_sees_the_new_index(tmp_path):
    """The other half: restarting is sufficient, so the restart in
    maintain.sh is the whole fix — no cache to clear, nothing to invalidate."""
    persist = tmp_path / "chroma"
    seeded = _collection(persist)
    seeded.upsert(ids=["old-1"], embeddings=[OLD], documents=["before the reindex"])

    subprocess.run(
        [sys.executable, "-c", _REINDEXER, str(persist)],
        capture_output=True,
        text=True,
        check=True,
    )

    reader = textwrap.dedent(
        """
        import sys
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(path=sys.argv[1],
                                           settings=Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection("corpus", metadata={"hnsw:space": "cosine"})
        print(",".join(coll.query(query_embeddings=[[0.9, 0.1, 0.0]], n_results=5)["ids"][0]))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", reader, str(persist)], capture_output=True, text=True, check=True
    )
    assert "new-1" in out.stdout
