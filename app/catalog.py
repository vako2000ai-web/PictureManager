"""Слой доступа к каталогу: корни, файлы, ручные даты, группы связанных файлов, лента."""
import os
import sys
from datetime import datetime

from .metadata import iso, resolve_date

EFF_DATE = "COALESCE(f.manual_taken_at, f.taken_at)"


def volume_id(path: str) -> str:
    """Серийный номер тома (Windows) либо st_dev (иначе); не зависит от буквы диска."""
    if sys.platform == "win32":
        try:
            import ctypes

            drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
            serial = ctypes.c_uint32()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive), None, 0, ctypes.byref(serial), None, None, None, 0
            )
            if ok:
                return f"{serial.value:08X}"
        except Exception:  # noqa: BLE001
            pass
    return f"dev{os.stat(path).st_dev}"


def _tail(path: str) -> str:
    return os.path.splitdrive(path)[1].replace("\\", "/").lower()


# ---- корни ----

def add_root(db, path: str, label: str | None = None) -> dict:
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ValueError(f"каталог не существует: {path}")
    vol = volume_id(path)
    for row in db.query("SELECT * FROM roots WHERE volume_id=?", (vol,)):
        if _tail(row["path"]) == _tail(path):  # тот же диск под другой буквой
            db.execute("UPDATE roots SET path=? WHERE id=?", (path, row["id"]))
            return dict(db.one("SELECT * FROM roots WHERE id=?", (row["id"],)))
    if db.one("SELECT 1 FROM roots WHERE path=?", (path,)):
        raise ValueError("корень уже добавлен")
    cur = db.execute("INSERT INTO roots(path, volume_id, label) VALUES(?,?,?)", (path, vol, label or path))
    return dict(db.one("SELECT * FROM roots WHERE id=?", (cur.lastrowid,)))


def list_roots(db) -> list[dict]:
    rows = db.query(
        "SELECT r.*, (SELECT COUNT(*) FROM files f WHERE f.root_id=r.id AND f.status='present') AS files, "
        "(SELECT COALESCE(SUM(size),0) FROM files f WHERE f.root_id=r.id AND f.status='present') AS bytes "
        "FROM roots r ORDER BY id"
    )
    return [dict(r) for r in rows]


def remove_root(db, root_id: int) -> None:
    db.execute("DELETE FROM roots WHERE id=?", (root_id,))


def get_root(db, root_id: int):
    row = db.one("SELECT * FROM roots WHERE id=?", (root_id,))
    if row is None:
        raise KeyError(f"нет корня {root_id}")
    return row


def root_path(db, root_id: int) -> str:
    return get_root(db, root_id)["path"]


# ---- файлы ----

def abs_path(root_path_: str, rel_path: str) -> str:
    return os.path.join(root_path_, *rel_path.split("/"))


def get_file(db, file_id: int):
    """Строка файла с полем root_path и абсолютным путём abs (в dict)."""
    row = db.one("SELECT f.*, r.path AS root_path FROM files f JOIN roots r ON r.id=f.root_id WHERE f.id=?", (file_id,))
    if row is None:
        return None
    d = dict(row)
    d["abs"] = abs_path(d["root_path"], d["rel_path"])
    d["eff_date"] = d["manual_taken_at"] or d["taken_at"]
    return d


def index_file(db, cfg, root_id: int, root_path_: str, rel_path: str, st: os.stat_result, existing=None) -> str:
    """Создаёт или обновляет запись файла; возвращает 'new' | 'updated'."""
    name = rel_path.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    kind = cfg.kind_of(ext)
    path = abs_path(root_path_, rel_path)
    taken, source, conf = resolve_date(path, name, kind, st.st_mtime_ns, cfg.find_tool("ffprobe"))
    if existing is None:
        db.execute(
            "INSERT INTO files(root_id, rel_path, size, mtime_ns, kind, ext, status, taken_at, date_source, date_confidence) "
            "VALUES(?,?,?,?,?,?, 'present', ?,?,?)",
            (root_id, rel_path, st.st_size, st.st_mtime_ns, kind, ext, taken, source, conf),
        )
        return "new"
    if existing["manual_taken_at"]:  # ручная дата не затирается
        taken, source, conf = existing["taken_at"], existing["date_source"], existing["date_confidence"]
    db.execute(
        "UPDATE files SET size=?, mtime_ns=?, kind=?, ext=?, status='present', taken_at=?, date_source=?, "
        "date_confidence=?, sha256=NULL, hash_size=NULL, hash_mtime_ns=NULL WHERE id=?",
        (st.st_size, st.st_mtime_ns, kind, ext, taken, source, conf, existing["id"]),
    )
    return "updated"


def set_manual_date(db, file_ids: list[int], taken_at: str | None) -> int:
    """Ручная дата (ISO) для файлов; None сбрасывает её и возвращает автоопределённую."""
    if taken_at is not None:
        try:
            taken_at = iso(datetime.fromisoformat(taken_at))
        except ValueError as exc:
            raise ValueError(f"некорректная дата: {taken_at}") from exc
    count = 0
    for fid in file_ids:
        if taken_at is not None:
            cur = db.execute(
                "UPDATE files SET manual_taken_at=?, taken_at=?, date_source='manual', date_confidence=1.0 WHERE id=?",
                (taken_at, taken_at, fid),
            )
        else:
            f = db.one("SELECT f.*, r.path AS rp FROM files f JOIN roots r ON r.id=f.root_id WHERE f.id=?", (fid,))
            if f is None:
                continue
            path = abs_path(f["rp"], f["rel_path"])
            name = f["rel_path"].rsplit("/", 1)[-1]
            from .metadata import resolve_date as rd

            t, s, c = rd(path, name, f["kind"], f["mtime_ns"], None)
            cur = db.execute(
                "UPDATE files SET manual_taken_at=NULL, taken_at=?, date_source=?, date_confidence=? WHERE id=?",
                (t, s, c, fid),
            )
        count += cur.rowcount
    return count


# ---- группы связанных файлов ----

_RANK = {"photo": 0, "raw": 1, "video": 2}


def primary_of(members) -> dict:
    """Основной файл группы: фото, затем RAW, затем видео; при равенстве HEIC/JPEG раньше прочих."""
    return min(members, key=lambda m: (_RANK.get(m["kind"], 3), m["ext"] not in ("jpg", "jpeg", "heic", "heif"), m["id"]))


def recompute_groups(db, root_id: int) -> None:
    """Присваивает общий group_key файлам с одинаковым именем без расширения в одной папке."""
    rows = db.query("SELECT id, rel_path, group_key FROM files WHERE root_id=? AND status='present'", (root_id,))
    buckets: dict[str, list] = {}
    for r in rows:
        stem = r["rel_path"].rsplit(".", 1)[0].lower() if "." in r["rel_path"].rsplit("/", 1)[-1] else r["rel_path"].lower()
        buckets.setdefault(stem, []).append(r)
    updates = []
    for stem, members in buckets.items():
        key = f"{root_id}:{stem}" if len(members) > 1 else None
        for m in members:
            if m["group_key"] != key:
                updates.append((key, m["id"]))
    if updates:
        with db.tx():
            db.conn.executemany("UPDATE files SET group_key=? WHERE id=?", updates)


# ---- лента ----

def list_catalog(db, sort="desc", kind=None, root_id=None, status=None, no_date=False, is_derived=None,
                 limit=200, offset=0) -> dict:
    where, params = [], []
    if kind:
        where.append("f.kind=?"); params.append(kind)
    if root_id:
        where.append("f.root_id=?"); params.append(root_id)
    if status:
        where.append("f.status=?"); params.append(status)
    if no_date:
        where.append(f"{EFF_DATE} IS NULL")
    if is_derived is not None:
        where.append(("f.id IN" if is_derived else "f.id NOT IN") + " (SELECT file_id FROM derived_files)")
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.one(f"SELECT COUNT(*) AS n FROM files f {cond}", params)["n"]
    order = "DESC" if sort == "desc" else "ASC"
    rows = db.query(
        f"SELECT f.*, r.path AS root_path, {EFF_DATE} AS eff_date, "
        f"EXISTS(SELECT 1 FROM derived_files d WHERE d.file_id=f.id) AS is_derived "
        f"FROM files f JOIN roots r ON r.id=f.root_id {cond} "
        f"ORDER BY ({EFF_DATE} IS NULL), {EFF_DATE} {order}, f.id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    days: dict[str | None, list] = {}
    for r in rows:
        d = dict(r)
        days.setdefault(d["eff_date"][:10] if d["eff_date"] else None, []).append(d)
    return {"total": total, "days": [{"day": k, "count": len(v), "files": v} for k, v in days.items()]}


def stats(db) -> dict:
    row = db.one(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes, "
        "SUM(status='present') AS present, SUM(status='missing') AS missing, SUM(status='quarantined') AS quarantined, "
        f"SUM(COALESCE(manual_taken_at, taken_at) IS NULL) AS no_date FROM files f"
    )
    return {k: (row[k] or 0) for k in row.keys()}
