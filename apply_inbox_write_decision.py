#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

VAULT = Path(os.getenv("OBSIDIAN_VAULT", os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault")))
OS_DIR = VAULT / "00-OS"
RUNTIME_DIR = Path(os.getenv("INBOX_RUNTIME_DIR", os.path.expanduser("~/.openclaw/runtime")))

QUEUE_FILE = RUNTIME_DIR / "inbox-write-queue.json"
DECISION_FILE = RUNTIME_DIR / "inbox-archive-decisions.json"
LOG_FILE = RUNTIME_DIR / "inbox-write-apply-log.md"
REPORT_STATUS_FILE = RUNTIME_DIR / "inbox-write-report.status"
QUEUE_LOCK_FILE = RUNTIME_DIR / ".inbox-write-queue.lock"
WRITE_CACHE_LOCK_FILE = RUNTIME_DIR / ".inbox-write-history-cache.lock"
# 重复检测缓存：存储已成功写入的内容哈希
WRITE_CACHE_FILE = RUNTIME_DIR / "inbox-write-history-cache.json"

# capture 清理相关
CAPTURE_DIR = VAULT / "00-OS" / "inbox" / "capture"
ID_MAP_FILE = RUNTIME_DIR / "inbox-id-map.json"

DOC_MAP = {
    "OS-关于我": VAULT / "01-Area" / "个人" / "OS-关于我.md",
    "OS-家庭-孩子学校总表": VAULT / "01-Area" / "家庭" / "OS-家庭-孩子学校总表.md",
    "工作-近期事实与进展": VAULT / "01-Area" / "工作" / "工作-近期事实与进展.md",
    "OS-工作-关系互动记录": VAULT / "01-Area" / "工作" / "OS-工作-关系互动记录.md",
    "工作-人物画像与协作地图": VAULT / "01-Area" / "工作" / "工作-人物画像与协作地图.md",
    "OS-个人想法与灵感": VAULT / "01-Area" / "个人" / "OS-个人想法与灵感.md",
    "OS-个人思考与反思": VAULT / "01-Area" / "个人" / "OS-个人思考与反思.md",
    "OS-工具与方法论": VAULT / "01-Area" / "个人" / "OS-工具与方法论.md",
    "OS-个人项目与研究": VAULT / "01-Area" / "个人" / "OS-工具与方法论.md",  # 旧名兼容，实际写到工具与方法论
    "健康-MasterLog": VAULT / "01-Area" / "健康" / "健康-MasterLog.md",
    "个人-美食记录": VAULT / "01-Area" / "个人" / "个人-美食记录.md",
}

# promote_to_note 输出目录
NOTES_DIR = VAULT / "00-OS" / "notes"

# note 池索引
NOTES_INDEX_FILE = VAULT / "00-OS" / "notes" / "notes_index.json"
NOTES_INDEX_LOCK_FILE = RUNTIME_DIR / ".notes-index.lock"

# Area 总览页白名单
ALLOWED_AREA_OVERVIEWS = {
    "01-Area/个人/AI工作流总览.md": VAULT / "01-Area" / "个人" / "AI工作流总览.md",
    "01-Area/阅读/个人知识库总览.md": VAULT / "01-Area" / "阅读" / "个人知识库总览.md",
}
AREA_OVERVIEW_SECTION = "## 最近关键进展"

def _find_note_by_title(title: str) -> Path | None:
    """从 notes_index.json 按标题模糊匹配，返回 note 文件路径。"""
    if not NOTES_INDEX_FILE.exists():
        return None
    try:
        index = json.loads(NOTES_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    title_lower = title.strip().lower()
    # 精确匹配
    for entry in index:
        if entry.get("title", "").strip().lower() == title_lower:
            p = VAULT / entry["path"]
            if p.exists():
                return p
    # 包含匹配（标题是子串）
    for entry in index:
        entry_title = entry.get("title", "").strip().lower()
        if title_lower in entry_title or entry_title in title_lower:
            p = VAULT / entry["path"]
            if p.exists():
                return p
    # 文件名匹配
    safe = re.sub(r'[^\w\s\u4e00-\u9fff\.-]', '', title).strip()
    candidate = NOTES_DIR / f"{safe}.md"
    return candidate if candidate.exists() else None


class QueueLoadError(RuntimeError):
    pass

@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            tmp_path.chmod(path.stat().st_mode)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

def atomic_write_json(path: Path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

def load_queue():
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QueueLoadError(f"invalid queue JSON: {exc}") from exc
    except OSError as exc:
        raise QueueLoadError(f"failed to read queue: {exc}") from exc
    if not isinstance(data, list):
        raise QueueLoadError("queue file must contain a JSON list")
    return data

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""

APPEND_ANCHOR = "<!-- INBOX_APPEND_ABOVE -->"


def build_appended_text(old: str, text: str) -> str:
    chunk = text.strip() + "\n"
    # 如果文件里有锚点标记，插入到锚点前面而不是文件末尾
    if APPEND_ANCHOR in old:
        return old.replace(APPEND_ANCHOR, chunk + "\n" + APPEND_ANCHOR, 1)
    # 没有锚点，追加到末尾（原逻辑）
    return old + ("\n" if old and not old.endswith("\n") else "") + chunk

def append_to_doc_atomically(path: Path, text: str) -> str:
    lock_path = path.parent / f".{path.name}.lock"
    normalized = text.strip()
    with file_lock(lock_path):
        old = read_text(path)
        if normalized in old:
            return "duplicate"
        atomic_write_text(path, build_appended_text(old, text))
    return "written"

def write_log(lines: list[str]):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = ["# inbox write apply log", "", f"> 更新时间：{now}", ""]
    atomic_write_text(LOG_FILE, "\n".join(header + lines).strip() + "\n")

def save_queue_and_status(queue: list[dict]):
    atomic_write_json(QUEUE_FILE, queue)
    atomic_write_text(REPORT_STATUS_FILE, "pending\n" if queue else "empty\n")

def update_write_cache(target_doc: str, text: str):
    """记录成功写入的哈希"""
    try:
        with file_lock(WRITE_CACHE_LOCK_FILE):
            cache = {}
            if WRITE_CACHE_FILE.exists():
                try:
                    cache = json.loads(WRITE_CACHE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    cache = {}

            # 使用 target_doc + text 的哈希作为 key
            content_hash = hashlib.sha256(f"{target_doc}:{text.strip()}".encode("utf-8")).hexdigest()
            cache[content_hash] = datetime.now().isoformat()

            # 只保留最近 1000 条记录
            if len(cache) > 1000:
                sorted_keys = sorted(cache.keys(), key=lambda k: cache[k])
                for k in sorted_keys[:len(cache) - 1000]:
                    del cache[k]

            atomic_write_json(WRITE_CACHE_FILE, cache)
    except Exception as e:
        print(f"Warning: failed to update write cache: {e}")

def _extract_section(content: str, heading: str) -> str:
    """提取 note 正文中某个 ## 区块的文本内容。"""
    pattern = re.compile(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", re.M | re.S)
    m = pattern.search(content)
    return m.group(1).strip() if m else ""


def update_notes_index(note_path: Path, note_title: str, note_content: str,
                       area: str, project: str, tags: list) -> None:
    """将 note 写入 / 更新到 notes_index.json。"""
    note_id = str(note_path.relative_to(VAULT))
    one_liner = _extract_section(note_content, "一句话")
    my_judgment = _extract_section(note_content, "我的判断")
    today = datetime.now().strftime("%Y-%m-%d")

    entry = {
        "id": note_id,
        "title": note_title,
        "path": note_id,
        "area": area or "",
        "project": project or "",
        "source": "inbox",
        "created": today,
        "tags": tags if isinstance(tags, list) else [],
        "one_liner": one_liner,
        "my_judgment": my_judgment,
        "status": "active",
        "last_reviewed": "",
        "review_count": 0,
    }

    NOTES_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(NOTES_INDEX_LOCK_FILE):
        index = []
        if NOTES_INDEX_FILE.exists():
            try:
                index = json.loads(NOTES_INDEX_FILE.read_text(encoding="utf-8"))
                if not isinstance(index, list):
                    index = []
            except Exception:
                index = []
        # 按 id 更新或追加
        existing = {e["id"]: i for i, e in enumerate(index)}
        if note_id in existing:
            # 保留 last_reviewed / review_count
            old = index[existing[note_id]]
            entry["last_reviewed"] = old.get("last_reviewed", "")
            entry["review_count"] = old.get("review_count", 0)
            index[existing[note_id]] = entry
        else:
            index.append(entry)
        atomic_write_json(NOTES_INDEX_FILE, index)


def append_to_area_overview_section(path: Path, text: str) -> str:
    """追加到总览页的"最近关键进展"区块，返回 "written" / "duplicate"。"""
    lock_path = path.parent / f".{path.name}.lock"
    normalized = text.strip()
    with file_lock(lock_path):
        old = read_text(path)
        if normalized in old:
            return "duplicate"
        if AREA_OVERVIEW_SECTION in old:
            idx = old.index(AREA_OVERVIEW_SECTION) + len(AREA_OVERVIEW_SECTION)
            next_sec = re.search(r"\n## ", old[idx:])
            if next_sec:
                insert_pos = idx + next_sec.start()
                new_content = old[:insert_pos].rstrip() + "\n" + normalized + "\n" + old[insert_pos:]
            else:
                new_content = old.rstrip() + "\n" + normalized + "\n"
        else:
            new_content = old.rstrip() + "\n\n---\n\n" + AREA_OVERVIEW_SECTION + "\n\n" + normalized + "\n"
        atomic_write_text(path, new_content)
    return "written"


def apply_area_overview(item: dict, lines: list) -> tuple:
    """处理 area_overview 追加，返回 (written, skipped)。"""
    if not item.get("should_append_to_area_overview"):
        return 0, 0
    ov_target = item.get("area_overview_target", "").strip()
    ov_text = item.get("area_overview_append_text", "").strip()
    ov_path = ALLOWED_AREA_OVERVIEWS.get(ov_target)
    if not ov_path or not ov_text:
        if ov_target and not ov_path:
            lines.append(f"- area_overview: skipped (not in allowlist: {ov_target})")
        return 0, 0
    try:
        ov_result = append_to_area_overview_section(ov_path, ov_text)
        lines.append(f"- area_overview: {ov_result}")
        lines.append(f"- area_overview_target: {ov_target}")
        if ov_result == "written":
            lines.append(f"- area_overview_text: {ov_text}")
            return 1, 0
        return 0, 1
    except Exception as exc:
        lines.append(f"- area_overview: error ({exc})")
        return 0, 0


INBOX_ARCHIVE_DIR = VAULT / "04-Archive" / "inbox-原始"


REVIEW_DIR = VAULT / "00-OS" / "inbox" / "review"

# health 类别原文专属存档目录
HEALTH_RECORD_DIR = VAULT / "01-Area" / "健康" / "记录"


def move_resolved_capture_files(resolved_ids: set[str], review_ids: set[str], health_ids: set[str], log_lines: list[str]) -> tuple[int, int]:
    """将已 resolve 的 capture 源文件移走。
    review_ids 中的移到 00-OS/inbox/review/，
    health_ids 中的移到 01-Area/健康/记录/，
    其余移到 04-Archive/inbox-原始/YYYY-MM/。
    返回 (moved, skipped)。
    """
    if not ID_MAP_FILE.exists():
        log_lines.append("- capture_cleanup: skipped (no id-map file)")
        return 0, 0

    try:
        id_map = json.loads(ID_MAP_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log_lines.append(f"- capture_cleanup: error reading id-map ({exc})")
        return 0, 0

    if not id_map:
        log_lines.append("- capture_cleanup: skipped (id-map empty)")
        return 0, 0

    month = datetime.now().strftime("%Y-%m")
    done_dir = INBOX_ARCHIVE_DIR / month
    done_dir.mkdir(parents=True, exist_ok=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    HEALTH_RECORD_DIR.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0

    for item_id in sorted(resolved_ids):
        filename = id_map.get(item_id)
        if not filename:
            continue
        src = CAPTURE_DIR / filename
        if not src.exists():
            log_lines.append(f"- capture_cleanup: [{item_id}] {filename} — not found, skip")
            skipped += 1
            continue
        # keep_in_review 的移到 review/，health 的移到专属目录，其余移到归档
        if item_id in review_ids:
            dst = REVIEW_DIR / filename
            label = "inbox/review/"
        elif item_id in health_ids:
            dst = HEALTH_RECORD_DIR / filename
            label = "01-Area/健康/记录/"
        else:
            dst = done_dir / filename
            label = f"inbox-原始/{month}/"
        # 避免覆盖同名文件
        if dst.exists():
            stem, suffix = dst.stem, dst.suffix
            dst = dst.parent / f"{stem}-{item_id}{suffix}"
        try:
            src.rename(dst)
            log_lines.append(f"- capture_cleanup: [{item_id}] {filename} → {label}")
            moved += 1
        except Exception as exc:
            log_lines.append(f"- capture_cleanup: [{item_id}] {filename} — move failed ({exc})")
            skipped += 1

    return moved, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="apply confirmed inbox write queue items")
    parser.add_argument("--confirm", nargs="*", type=str, default=[], help="ids to confirm")
    parser.add_argument("--ignore", nargs="*", type=str, default=[], help="ids to ignore")
    args = parser.parse_args()

    try:
        with file_lock(QUEUE_LOCK_FILE):
            queue = load_queue()
            confirm_ids = set(args.confirm)
            ignore_ids = set(args.ignore)

            lines = []
            written = ignored = untouched = errors = duplicates = kept_in_review = archived_raw = 0
            area_ov_written = area_ov_skipped = 0

            overlap_ids = confirm_ids & ignore_ids
            if overlap_ids:
                for item_id in sorted(overlap_ids):
                    lines.append(f"## [{item_id}]")
                    lines.append("- result: error (id cannot be both confirmed and ignored)")
                    lines.append("")
                    errors += 1
                confirm_ids -= overlap_ids
                ignore_ids -= overlap_ids

            by_id = {}
            for item in queue:
                iid = str(item.get("id", ""))
                if iid:
                    by_id[iid] = item

            resolved_ids = set()
            review_ids = set()
            health_ids = set()

            # 加载 id_map（用于 note 原文链接）
            try:
                id_map = json.loads(ID_MAP_FILE.read_text(encoding="utf-8")) if ID_MAP_FILE.exists() else {}
            except Exception:
                id_map = {}

            for item_id in sorted(confirm_ids):
                item = by_id.get(item_id)
                lines.append(f"## [{item_id}]")
                if not item:
                    lines.append("- result: error (id not found in queue)")
                    lines.append("")
                    errors += 1
                    continue

                action = item.get("action", "append_to_existing")
                target = item.get("target_doc", "").strip()
                destination = item.get("destination", target).strip()
                write_text = item.get("write_text", "").strip()

                # 安全网：health 类别强制走 append_to_existing → 健康-MasterLog
                cat = item.get("category", "")
                if cat == "health":
                    health_ids.add(item_id)
                    if action != "append_to_existing":
                        lines.append(f"- override: health category forced to append_to_existing (was {action})")
                        action = "append_to_existing"
                        if not target or "健康" not in target:
                            target = "健康-MasterLog"
                            destination = target

                # keep_in_review: 留 inbox，无写操作
                if action == "keep_in_review":
                    lines.append("- result: kept_in_review")
                    lines.append(f"- destination: {destination or 'inbox'}")
                    ov_w, ov_s = apply_area_overview(item, lines)
                    area_ov_written += ov_w; area_ov_skipped += ov_s
                    lines.append("")
                    kept_in_review += 1
                    resolved_ids.add(item_id)
                    review_ids.add(item_id)
                    continue

                # archive_raw_only: 原文已由 execute 处理，此处无需内容写入
                if action == "archive_raw_only":
                    lines.append("- result: archived_raw (no content write)")
                    lines.append(f"- destination: {destination or target}")
                    ov_w, ov_s = apply_area_overview(item, lines)
                    area_ov_written += ov_w; area_ov_skipped += ov_s
                    lines.append("")
                    archived_raw += 1
                    resolved_ids.add(item_id)
                    continue

                # promote_to_note: 在 NOTES_DIR 新建独立笔记文件
                if action == "promote_to_note":
                    note_title = destination.strip() if destination and destination != "inbox" else f"inbox-note-{item_id}"
                    safe_title = re.sub(r'[^\w\s\u4e00-\u9fff\.-]', '', note_title).strip()[:80]
                    note_path = NOTES_DIR / f"{datetime.now().strftime('%Y-%m-%d')} {safe_title}.md"
                    try:
                        NOTES_DIR.mkdir(parents=True, exist_ok=True)
                        if note_path.exists():
                            # 同名 note 已存在 → 追加内容（AI 合并场景）
                            append_result = append_to_doc_atomically(note_path, write_text)
                            update_write_cache(str(note_path), write_text)
                            lines.append(f"- result: appended_to_existing_note ({append_result})")
                            lines.append(f"- path: {note_path.relative_to(VAULT)}")
                            lines.append("")
                            written += 1
                            resolved_ids.add(item_id)
                            continue
                        area_match = item.get("area_match", "") or ""
                        project_match = item.get("project_match", "") or ""
                        today = datetime.now().strftime("%Y-%m-%d")
                        # 原文链接：从 id_map 找到源文件名
                        source_file = id_map.get(item_id, "")
                        source_display = os.path.splitext(source_file)[0] if source_file else ""
                        frontmatter = (
                            f"---\n"
                            f"kind: note\n"
                            f"area: {area_match}\n"
                            f"project: {project_match}\n"
                            f"source: inbox\n"
                            f"source_file: \"{source_file}\"\n"
                            f"status: active\n"
                            f"created: {today}\n"
                            f"tags: []\n"
                            f"related: []\n"
                            f"---\n\n"
                        )
                        # 正文末尾加原文链接
                        source_link = f"\n\n---\n> 原文来源：[[{source_display}]]" if source_display else ""
                        note_content = f"{frontmatter}# {note_title}\n\n{write_text}{source_link}\n"
                        atomic_write_text(note_path, note_content)
                        update_write_cache(str(note_path), write_text)
                        try:
                            update_notes_index(
                                note_path, note_title, note_content,
                                area_match, project_match,
                                item.get("tags") or [],
                            )
                        except Exception as idx_exc:
                            lines.append(f"- notes_index: error ({idx_exc})")
                        lines.append("- result: promoted_to_note")
                        lines.append(f"- path: {note_path.relative_to(VAULT)}")
                        ov_w, ov_s = apply_area_overview(item, lines)
                        area_ov_written += ov_w; area_ov_skipped += ov_s
                        lines.append("")
                        written += 1
                        resolved_ids.add(item_id)
                    except Exception as exc:
                        lines.append(f"- result: error (promote failed: {exc})")
                        lines.append("")
                        errors += 1
                    continue

                # append_to_note: 追加内容到已有的 note 文件
                if action == "append_to_note":
                    note_title = destination.strip() if destination else ""
                    if not note_title or not write_text:
                        lines.append(f"- result: error (append_to_note missing title or text)")
                        lines.append("")
                        errors += 1
                        continue
                    # 从 notes_index 查找匹配的 note
                    matched_path = _find_note_by_title(note_title)
                    if matched_path and matched_path.exists():
                        try:
                            today = datetime.now().strftime("%Y-%m-%d")
                            append_text = f"\n---\n\n## 更新 {today}\n\n{write_text.strip()}\n"
                            result = append_to_doc_atomically(matched_path, append_text)
                            if result == "duplicate":
                                lines.append("- result: skipped_duplicate (append_to_note)")
                                lines.append(f"- path: {matched_path.relative_to(VAULT)}")
                                duplicates += 1
                            else:
                                update_write_cache(str(matched_path), write_text)
                                lines.append("- result: appended_to_note")
                                lines.append(f"- path: {matched_path.relative_to(VAULT)}")
                                written += 1
                            ov_w, ov_s = apply_area_overview(item, lines)
                            area_ov_written += ov_w; area_ov_skipped += ov_s
                            lines.append("")
                            resolved_ids.add(item_id)
                        except Exception as exc:
                            lines.append(f"- result: error (append_to_note failed: {exc})")
                            lines.append("")
                            errors += 1
                    else:
                        # 兜底：找不到已有 note，降级为 promote_to_note
                        lines.append(f"- note_match: not found for '{note_title}', fallback to promote")
                        safe_title = re.sub(r'[^\w\s\u4e00-\u9fff\.-]', '', note_title).strip()[:80]
                        note_path = NOTES_DIR / f"{datetime.now().strftime('%Y-%m-%d')} {safe_title}.md"
                        try:
                            NOTES_DIR.mkdir(parents=True, exist_ok=True)
                            if note_path.exists():
                                note_path = NOTES_DIR / f"{datetime.now().strftime('%Y-%m-%d')} {safe_title}-{item_id}.md"
                            area_match = item.get("area_match", "") or ""
                            today = datetime.now().strftime("%Y-%m-%d")
                            source_file = id_map.get(item_id, "")
                            source_display = os.path.splitext(source_file)[0] if source_file else ""
                            frontmatter = (
                                f"---\nkind: note\narea: {area_match}\n"
                                f"source: inbox\nsource_file: \"{source_file}\"\n"
                                f"status: active\ncreated: {today}\n"
                                f"tags: []\nrelated: []\n---\n\n"
                            )
                            source_link = f"\n\n---\n> 原文来源：[[{source_display}]]" if source_display else ""
                            note_content = f"{frontmatter}# {note_title}\n\n{write_text}{source_link}\n"
                            atomic_write_text(note_path, note_content)
                            update_write_cache(str(note_path), write_text)
                            lines.append(f"- result: fallback_promoted_to_note")
                            lines.append(f"- path: {note_path.relative_to(VAULT)}")
                            ov_w, ov_s = apply_area_overview(item, lines)
                            area_ov_written += ov_w; area_ov_skipped += ov_s
                            lines.append("")
                            written += 1
                            resolved_ids.add(item_id)
                        except Exception as exc:
                            lines.append(f"- result: error (fallback promote failed: {exc})")
                            lines.append("")
                            errors += 1
                    continue

                # append_to_existing → 写入文档
                # destination 优先，回落到 target_doc
                effective_target = destination if destination and destination != "inbox" else target
                if effective_target not in DOC_MAP:
                    lines.append(f"- result: error (unknown target_doc: {effective_target})")
                    lines.append("")
                    errors += 1
                    continue

                if not write_text:
                    lines.append("- result: error (empty write_text)")
                    lines.append("")
                    errors += 1
                    continue

                target = effective_target

                target_path = DOC_MAP[target]

                try:
                    write_result = append_to_doc_atomically(target_path, write_text)
                except Exception as exc:
                    lines.append(f"- result: error (write failed: {exc})")
                    lines.append(f"- target_doc: {target}")
                    lines.append(f"- reason: {item.get('reason', '')}")
                    lines.append("")
                    errors += 1
                    continue

                if write_result == "duplicate":
                    lines.append("- result: skipped_duplicate")
                    lines.append(f"- target_doc: {target}")
                    lines.append(f"- reason: {item.get('reason', '')}")
                    ov_w, ov_s = apply_area_overview(item, lines)
                    area_ov_written += ov_w; area_ov_skipped += ov_s
                    lines.append("")
                    duplicates += 1
                    resolved_ids.add(item_id)
                    continue

                update_write_cache(target, write_text)
                lines.append("- result: written")
                lines.append(f"- target_doc: {target}")
                lines.append(f"- reason: {item.get('reason', '')}")
                ov_w, ov_s = apply_area_overview(item, lines)
                area_ov_written += ov_w; area_ov_skipped += ov_s
                lines.append("")
                written += 1
                resolved_ids.add(item_id)

            for item_id in sorted(ignore_ids):
                item = by_id.get(item_id)
                lines.append(f"## [{item_id}]")
                if not item:
                    lines.append("- result: error (id not found in queue)")
                    lines.append("")
                    errors += 1
                    continue
                lines.append("- result: ignored")
                lines.append(f"- target_doc: {item.get('target_doc', '')}")
                lines.append("")
                ignored += 1
                resolved_ids.add(item_id)

            remaining_queue = [item for item in queue if str(item.get("id")) not in resolved_ids]
            untouched = len(remaining_queue)

            # ── 从全量 decisions 补充 health_ids（非 confirm 路径的也要识别）──
            # ── 同时根据文件名兜底：含"健身""训练""练腿""练背""练胸"的强制归 health ──
            try:
                if ID_MAP_FILE.exists():
                    _id_map = json.loads(ID_MAP_FILE.read_text(encoding="utf-8"))
                    _health_keywords = ["健身", "训练", "练腿", "练背", "练胸", "练肩", "手臂", "有氧", "腿日", "背日", "胸日", "肩日"]
                    for _id, _fname in _id_map.items():
                        if any(kw in _fname for kw in _health_keywords):
                            health_ids.add(_id)
            except Exception:
                pass
            try:
                if DECISION_FILE.exists():
                    all_decisions = json.loads(DECISION_FILE.read_text(encoding="utf-8"))
                    for d in all_decisions:
                        if d.get("category") == "health":
                            health_ids.add(str(d.get("id", "")))
            except Exception:
                pass

            # ── capture 源文件清理 ──────────────────────────────────
            capture_moved = capture_skipped = 0
            capture_lines: list[str] = []
            if resolved_ids:
                capture_moved, capture_skipped = move_resolved_capture_files(
                    resolved_ids, review_ids, health_ids, capture_lines
                )

            summary = [
                "## summary",
                f"- written: {written}",
                f"- skipped_duplicate: {duplicates}",
                f"- kept_in_review: {kept_in_review}",
                f"- archived_raw: {archived_raw}",
                f"- ignored: {ignored}",
                f"- untouched: {untouched}",
                f"- errors: {errors}",
                f"- area_overview_written: {area_ov_written}",
                f"- area_overview_skipped: {area_ov_skipped}",
                f"- capture_moved: {capture_moved}",
                f"- capture_skipped: {capture_skipped}",
                "",
            ]
            write_log(summary + lines + ["", "## capture cleanup", ""] + capture_lines)

            if resolved_ids or not queue:
                save_queue_and_status(remaining_queue)

    except QueueLoadError as exc:
        print(f"queue error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"fatal error: {exc}", file=sys.stderr)
        return 1

    print(f"log: {LOG_FILE}")
    print(f"written={written} skipped_duplicate={duplicates} kept_in_review={kept_in_review} archived_raw={archived_raw} ignored={ignored} untouched={untouched} errors={errors} area_overview_written={area_ov_written} area_overview_skipped={area_ov_skipped} capture_moved={capture_moved} capture_skipped={capture_skipped}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
