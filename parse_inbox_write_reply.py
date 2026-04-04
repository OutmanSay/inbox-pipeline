#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

APPLY_SCRIPT = Path.home() / ".openclaw/workspace/scripts/apply_inbox_write_decision.py"
QUEUE_FILE = Path(os.getenv("INBOX_RUNTIME_DIR", os.path.expanduser("~/.openclaw/runtime"))) / "inbox-write-queue.json"

class QueueLoadError(RuntimeError):
    pass

def extract_ids(text: str) -> list[str]:
    # 匹配模式：可以是纯数字，也可以是 B1, test-1 这种格式
    # 我们按空格、逗号、分号分割，然后清洗
    parts = re.split(r"[ ,;，；\n]+", text)
    ids = []
    for p in parts:
        p = p.strip(" .。").upper()
        if p:
            ids.append(p)
    return ids

def parse_reply(text: str) -> tuple[list[str], list[str], str]:
    s = text.strip()

    # 语义化的“全部”判断
    all_keywords = ["全部", "全都", "所有", "全都要"]
    
    if any(k + "确认" in s for k in all_keywords) or any("确认" + k in s for k in all_keywords):
        return [], [], "all_confirm"
    
    if any(k + "忽略" in s for k in all_keywords) or any("忽略" + k in s for k in all_keywords):
        return [], [], "all_ignore"

    # 特殊处理：如果回复只有“全部确认”或“确认全部”
    if s in ["全部确认", "确认全部", "全部写入", "全都写入"]:
        return [], [], "all_confirm"

    confirm_ids = []
    ignore_ids = []

    # 尝试提取“确认”后面的部分，排除包含“全部”的情况
    m_confirm = re.search(r"确认([^忽略\n]*)", s)
    if m_confirm:
        raw_ids = m_confirm.group(1).strip()
        if not any(k in raw_ids for k in all_keywords):
            confirm_ids = extract_ids(raw_ids)

    # 尝试提取“忽略”后面的部分
    m_ignore = re.search(r"忽略([^\n]*)", s)
    if m_ignore:
        raw_ids = m_ignore.group(1).strip()
        if not any(k in raw_ids for k in all_keywords):
            ignore_ids = extract_ids(raw_ids)

    return confirm_ids, ignore_ids, "partial"

def load_all_ids() -> list[str]:
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
    return [str(x["id"]) for x in data if isinstance(x, dict) and "id" in x]

def load_reply(args: argparse.Namespace) -> str:
    if args.reply is not None and args.reply_file:
        raise ValueError("reply and --reply-file cannot be used together")
    if args.reply_file:
        try:
            return Path(args.reply_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"failed to read reply file: {exc}") from exc
    if args.reply is not None:
        return args.reply
    raise ValueError("reply text is required")

def main() -> int:
    parser = argparse.ArgumentParser(description="parse reply like: 确认 1 2，忽略 3")
    parser.add_argument("reply", nargs="?", help="reply text")
    parser.add_argument("--reply-file", help="path to a file containing the raw reply text")
    parser.add_argument("--run", action="store_true", help="actually execute apply script")
    args = parser.parse_args()

    try:
        reply_text = load_reply(args)
        confirm_ids, ignore_ids, mode = parse_reply(reply_text)
        if mode == "all_confirm":
            all_ids = load_all_ids()
            cmd = ["python3", str(APPLY_SCRIPT), "--confirm", *all_ids]
        elif mode == "all_ignore":
            all_ids = load_all_ids()
            cmd = ["python3", str(APPLY_SCRIPT), "--ignore", *all_ids]
        else:
            all_ids = load_all_ids()
            # 只说了忽略没说确认 → 其余自动确认
            if ignore_ids and not confirm_ids:
                confirm_ids = [i for i in all_ids if i not in set(ignore_ids)]
            # 只说了确认没说忽略 → 其余自动忽略
            elif confirm_ids and not ignore_ids:
                ignore_ids = [i for i in all_ids if i not in set(confirm_ids)]
            cmd = ["python3", str(APPLY_SCRIPT)]
            if confirm_ids:
                cmd += ["--confirm", *confirm_ids]
            if ignore_ids:
                cmd += ["--ignore", *ignore_ids]
    except (QueueLoadError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Parsed Action: {mode}")
    if confirm_ids:
        print(f"Confirm IDs: {confirm_ids}")
    if ignore_ids:
        print(f"Ignore IDs: {ignore_ids}")
    print(f"Executing: {' '.join(cmd)}")

    if args.run:
        print("\n--- Execution Start ---\n")
        completed = subprocess.run(cmd, check=False)
        print("\n--- Execution Finished ---")
        return completed.returncode
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
