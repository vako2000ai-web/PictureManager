import hashlib
import os
import shutil

BLOCK = 1 << 20  # 1 МиБ


class HashMismatch(Exception):
    pass


class Cancelled(Exception):
    pass


def sha256_file(path: str, on_bytes=None, cancel=None) -> str:
    """Потоковый SHA-256 блоками."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BLOCK)
            if not chunk:
                break
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
            if cancel is not None and cancel():
                raise Cancelled()
    return h.hexdigest()


def _existing_parent(path: str) -> str:
    p = os.path.dirname(os.path.abspath(path))
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def same_volume(a: str, b: str) -> bool:
    try:
        return os.stat(_existing_parent(a) if not os.path.exists(a) else a).st_dev == os.stat(
            _existing_parent(b)
        ).st_dev
    except OSError:
        return False


def unique_path(path: str) -> str:
    """path, либо path с суффиксом _1, _2 … перед расширением."""
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.lexists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def suffixed(path: str, n: int) -> str:
    stem, ext = os.path.splitext(path)
    return f"{stem}_{n}{ext}"


def move_file(src: str, dst: str, expected_sha: str | None = None, on_bytes=None) -> None:
    """Перемещение без перезаписи.

    Тот же том: атомарный os.replace. Другой том: копия во временный файл, сверка SHA-256
    и только после совпадения удаление оригинала (иначе копия удаляется, HashMismatch).
    """
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if same_volume(src, dst):
        os.replace(src, dst)
        return
    part = dst + ".part"
    try:
        h = hashlib.sha256()
        with open(src, "rb") as fin, open(part, "wb") as fout:
            while True:
                chunk = fin.read(BLOCK)
                if not chunk:
                    break
                h.update(chunk)
                fout.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(src, part)
        source_hash = expected_sha or h.hexdigest()
        if h.hexdigest() != source_hash or sha256_file(part) != source_hash:
            raise HashMismatch(src)
        os.replace(part, dst)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise
    os.remove(src)
