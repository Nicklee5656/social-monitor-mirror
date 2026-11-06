# 云端增量同步：拉索引 → 与已收录笔记比对 → 只采新增/变更 → 重建聚合文件
#
# 设计原则：
#   - 只读平台接口，不点赞/不关注/不发布，不绕过验证码或风控
#   - 登录态失效直接失败退出，绝不把「采集失败」写成「零更新」
#   - 一次失败不清空已有数据；成功的笔记才落盘
#   - 无新增时也更新本次运行状态与检查时间
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import xhs_client
from xhs_client import FetchError, LoginExpired

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "posts"
DATA = ROOT / "data"
STATE = ROOT / "state"

ACCOUNT_ID = os.environ.get("XHS_ACCOUNT_ID", "6245580f000000001000fce7")
ACCOUNT_LABEL = "Kikstart Ecom 凯客数智"
PLATFORM = "xiaohongshu"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path, obj, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        obj,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
    )
    path.write_text(text, "utf-8")


def sha256_of(obj):
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def hash_payload(p):
    """与本地 postprocess.py 完全一致，保证 hash 跨环境可比。"""
    return {
        "title": p.get("title"),
        "body_text": p.get("body_text"),
        "images": [
            {"position": im.get("position"), "source_url": im.get("source_url"), "text": im.get("extracted_text")}
            for im in (p.get("images") or [])
        ],
        "video": {
            "source_url": (p.get("video") or {}).get("source_url"),
            "transcript": (p.get("video") or {}).get("transcript"),
        },
        "metrics": p.get("metrics"),
    }


def load_cookies():
    raw = os.environ.get("XHS_COOKIES")
    if raw:
        return json.loads(raw)
    f = DATA / "cookies.json"
    if f.exists():
        return read_json(f, {})
    raise SystemExit("no cookies: set XHS_COOKIES secret or provide data/cookies.json")


def to_post_record(detail, existing=None):
    """详情 → 源库单篇结构（与历史 174 篇同构）。"""
    images = detail["images"]
    if existing:
        # 保留已提取的图片文字，避免覆盖掉 OCR 结果
        prev_by_pos = {im.get("position"): im for im in (existing.get("images") or [])}
        for im in images:
            prev = prev_by_pos.get(im.get("position"))
            if prev and prev.get("extracted_text"):
                im["extracted_text"] = prev["extracted_text"]
                im["extraction_status"] = prev.get("extraction_status") or "done"

    video = {
        "source_url": detail.get("video_url"),
        "transcript": None,
        "transcript_status": "not_applicable",
        "subtitle": None,
        "subtitle_status": "not_applicable",
        "poster_url": detail.get("video_poster"),
    }
    if detail["content_type"] == "video":
        video["transcript_status"] = "missing"
        video["subtitle_status"] = "missing"

    errors = []
    if images and not any(im.get("source_url") for im in images):
        errors.append("images_all_missing")
    if detail["content_type"] == "video" and not video["source_url"]:
        errors.append("video_url_missing")

    rec = {
        "schema_version": "1.0",
        "platform": PLATFORM,
        "account_id": ACCOUNT_ID,
        "post_id": detail["note_id"],
        "url": f"https://www.xiaohongshu.com/explore/{detail['note_id']}",
        "title": detail["title"],
        "published_at": detail["published_at"],
        "content_type": detail["content_type"],
        "body_text": detail["body_text"],
        "images": images,
        "video": video,
        "metrics": detail["metrics"],
        "collected_at": now_iso(),
        "content_status": "complete" if detail["body_text"] else "partial",
        "content_hash": None,
        "errors": errors,
        "extra": {
            "author": detail.get("author") or ACCOUNT_LABEL,
            "date_text": detail.get("date_text"),
            "index_xsec_token": detail.get("xsec_token"),
            "index_type": "video" if detail["content_type"] == "video" else "normal",
            "published_at_source": "ssr_time" if detail["published_at"] else None,
        },
    }
    rec["content_hash"] = sha256_of(hash_payload(rec))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=40, help="单次最多采多少篇新笔记")
    ap.add_argument("--recheck", type=int, default=8, help="每轮复查最近 N 篇是否被编辑")
    ap.add_argument("--build", action="store_true", default=True)
    ap.add_argument("--no-build", dest="build", action="store_false")
    args = ap.parse_args()

    started_at = now_iso()
    task_id = f"run-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    update = {
        "schema_version": "1.0",
        "task_id": task_id,
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "accounts_monitored": [ACCOUNT_ID],
        "new_posts": [],
        "updated_posts": [],
        "unchanged_posts": [],
        "known_posts_skipped": [],
        "partial_posts": [],
        "failed_posts": [],
        "deleted_or_unavailable": [],
        "errors": [],
        "check_only": False,
    }

    def finish(status, code):
        update["status"] = status
        update["finished_at"] = now_iso()
        write_json(STATE / "updates.json", update)
        print(json.dumps({"status": status, "exit": code, **{k: update[k] for k in
              ("new_posts", "updated_posts", "unchanged_posts", "failed_posts")}}, ensure_ascii=False))
        return code

    cookies = load_cookies()
    # user/me 只作辅助探针；权威判定放在索引接口上，
    # 避免探针接口本身异常时把「可用」误判成「过期」
    try:
        me = xhs_client.check_login(cookies)
        print(f"LOGIN_OK user_id={me.get('user_id')}")
    except (LoginExpired, FetchError) as e:
        print(f"LOGIN_PROBE_INCONCLUSIVE {e} -> 以索引接口结果为准")

    try:
        index = xhs_client.fetch_index(cookies, ACCOUNT_ID)
    except LoginExpired as e:
        update["errors"].append(f"login_expired: {e}")
        return finish("login_expired", 2)
    except FetchError as e:
        update["errors"].append(f"index_failed: {e}")
        return finish("failed", 4)

    if not index:
        # 索引为空不是「全部删除」，按异常处理并保留数据
        update["errors"].append("index_empty: 未取到任何笔记，跳过差异判定以保护已有数据")
        return finish("failed", 4)

    known = {p.stem for p in POSTS.glob("*.json")}
    new_ids = [pid for pid in index if pid not in known]
    existing_ids = [pid for pid in index if pid in known]
    # 此前收录、本次索引未出现的笔记：仅记录供人工核查，不改状态不删数据
    update["possibly_missing"] = sorted(known - set(index))
    if update["possibly_missing"]:
        update["errors"].append(
            f"index_missing_notes: {len(update['possibly_missing'])} 篇未出现在本次索引，已保留原数据待人工确认"
        )

    # 索引顺序即发布时间倒序：最近 N 篇做内容复查
    recheck_ids = existing_ids[: max(0, args.recheck)]
    update["known_posts_skipped"] = [i for i in existing_ids if i not in recheck_ids]
    update["unchanged_posts"] = list(update["known_posts_skipped"])

    print(f"index={len(index)} known={len(known)} new={len(new_ids)} recheck={len(recheck_ids)}")

    # ---- 新笔记 ----
    for pid in new_ids[: args.max_new]:
        try:
            d = xhs_client.fetch_detail(cookies, pid, (index[pid] or {}).get("xsec_token"))
            rec = to_post_record(d)
            write_json(POSTS / f"{pid}.json", rec)
            update["new_posts"].append(pid)
            print(f"  NEW {pid} type={rec['content_type']} body={len(rec['body_text'] or '')}")
        except LoginExpired as e:
            update["errors"].append(f"login_expired_during_detail: {e}")
            update["failed_posts"].append({"post_id": pid, "reason": "login_expired"})
            break
        except FetchError as e:
            update["failed_posts"].append({"post_id": pid, "reason": str(e)})
            print(f"  FAIL {pid} {e}")
        time.sleep(1.0 + (0.5 * (len(update["new_posts"]) % 3)))

    # ---- 复查可能被编辑的笔记 ----
    for pid in recheck_ids:
        f = POSTS / f"{pid}.json"
        prev = read_json(f, {})
        try:
            d = xhs_client.fetch_detail(cookies, pid, (index[pid] or {}).get("xsec_token"))
            rec = to_post_record(d, existing=prev)
            if rec["content_hash"] != prev.get("content_hash"):
                rec["extra"]["previous_published_at"] = prev.get("published_at")
                write_json(f, rec)
                update["updated_posts"].append(pid)
                print(f"  UPDATED {pid}")
            else:
                # 内容未变，仅同步互动数（不进 hash 判定之外的字段）
                if rec["metrics"] != prev.get("metrics"):
                    prev["metrics"] = rec["metrics"]
                    write_json(f, prev)
                update["unchanged_posts"].append(pid)
        except LoginExpired as e:
            update["errors"].append(f"login_expired_during_recheck: {e}")
            break
        except FetchError as e:
            update["failed_posts"].append({"post_id": pid, "reason": str(e)})
        time.sleep(0.8)

    if new_ids[args.max_new :]:
        update["errors"].append(
            f"new_posts_truncated: 本轮仅采 {args.max_new} 篇，剩余 {len(new_ids) - args.max_new} 篇下轮继续"
        )

    # ---- 重建聚合文件 ----
    if args.build:
        import subprocess

        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "build_mirror.py"),
            "--mode", "full",
            "--repo", os.environ.get("MIRROR_REPO", "Nicklee5656/social-monitor-mirror"),
            "--posts-dir", str(POSTS),
            "--mirror-dir", str(ROOT),
            "--updates-src", str(STATE / "updates.json"),
            "--state-file", str(STATE / "mirror-state.json"),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(r.stdout.strip()[-800:])
        if r.returncode != 0:
            update["errors"].append(f"build_mirror_failed: {r.stderr.strip()[:300]}")
            return finish("failed", 4)

    status = "success"
    if update["failed_posts"]:
        status = "partial"
    elif update["errors"]:
        status = "partial"
    return finish(status, 0 if status == "success" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
