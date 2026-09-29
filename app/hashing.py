from .fileops import sha256_file


def ensure_hash(db, f: dict, on_bytes=None, cancel=None) -> str:
    """SHA-256 с кэшем по паре (size, mtime_ns); f — результат catalog.get_file."""
    if f["sha256"] and f["hash_size"] == f["size"] and f["hash_mtime_ns"] == f["mtime_ns"]:
        return f["sha256"]
    digest = sha256_file(f["abs"], on_bytes, cancel)
    db.execute(
        "UPDATE files SET sha256=?, hash_size=?, hash_mtime_ns=? WHERE id=?",
        (digest, f["size"], f["mtime_ns"], f["id"]),
    )
    f["sha256"], f["hash_size"], f["hash_mtime_ns"] = digest, f["size"], f["mtime_ns"]
    return digest


def cached_hash(f: dict) -> str | None:
    if f["sha256"] and f["hash_size"] == f["size"] and f["hash_mtime_ns"] == f["mtime_ns"]:
        return f["sha256"]
    return None
