"""Content-addressed storage for full, untruncated event payloads.

dispatch.py's MAX_MESSAGE_CONTENT_CHARS still caps what's replayed to the
model on every future turn — that behavior is untouched by this module.
What changes is that the full text gets written here once, keyed by its
own hash, before truncation happens, so the discarded portion is still
byte-for-byte recoverable later (e.g. by a future memory_expand tool).
"""

import hashlib
import os


def _artifact_filename(artifact_id: str) -> str:
    return artifact_id.replace(":", "_") + ".txt"


def store(artifacts_dir: str, content: str) -> str:
    """Write content under its sha256 hash and return the artifact_id
    ("sha256:<hex>"). Idempotent — storing identical content twice writes
    the file once. Atomic write (tmp file + os.replace) so a crash
    mid-write can never leave a corrupt artifact behind."""
    os.makedirs(artifacts_dir, exist_ok=True)
    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = os.path.join(artifacts_dir, _artifact_filename(digest))
    if not os.path.exists(path):
        tmp_path = path + f".tmp{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    return digest


def load(artifacts_dir: str, artifact_id: str, offset: int = 0, length: int = None) -> str:
    """Retrieve exact content back, optionally a sub-range."""
    path = os.path.join(artifacts_dir, _artifact_filename(artifact_id))
    with open(path, "r", encoding="utf-8") as f:
        f.seek(offset)
        return f.read(length) if length is not None else f.read()
