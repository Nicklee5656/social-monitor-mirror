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
import re
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


def stable_url(u):
    """去掉 CDN URL 里的时效签名（路径中的时间戳/32位hex段、查询参数），
    只保留稳定部分用于内容哈希。否则每次请求签名变化都会被误判为内容更新。"""
    if not u:
        return None
    from urllib.parse import urlparse

    p = urlparse(str(u))
    segs = [s for s in p.path.split("/") if s]
    while segs and (
        re.fullmatch(r"\d{12,14}", segs[0]) or re.fullmatch(r"[0-9a-f]{32}", segs[0])
    ):
        segs = segs[1:]
    # 主机名是按区域/节点动态分配的 CDN 边缘（如 sns-video-zl / sns-video-qc），不参与比对
    return "/" + "/".join(segs)


def hash_payload(p):
    """与本地 postprocess.py 完全一致，保证 hash 跨环境可比。

    v3：内容哈希只覆盖「作者可控的内容」——标题、正文、图片、视频流，
    且 URL 先经 stable_url 归一化（剔除时效签名）。
    metrics（互动数）与 published_at（元数据）不参与，
    从而区分 content_updated / metadata_updated。
    """
    return {
        "title": p.get("title"),
        "body_text": p.get("body_text"),
        "images": [
            {"position": im.get("position"), "source_url": stable_url(im.get("source_url")), "text": im.get("extracted_text")}
            for im in (p.get("images") or [])
        ],
        "video": {
            "source_url": stable_url((p.get("video") or {}).get("source_url")),
            "transcript": (p.get("video") or {}).get("transcript"),
        },
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
        "content_updated_posts": [],
        "metadata_updated_posts": [],
        "unchanged_posts": [],
        "known_posts_skipped": [],
        "partial_posts": [],
        "failed_posts": [],
        "deleted_or_unavailable": [],
        "index_missing_but_accessible": [],
        "possibly_missing": [],
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

    # 此前收录、本次索引未出现的笔记：逐篇访问详情接口核实。
    # 平台的 user_posted 索引存在可见性差异（部分笔记不出现在索引但仍可正常访问），
    # 因此「索引缺失」不等于「已删除」——只有详情也拿不到的才保留待人工确认。
    raw_missing = sorted(known - set(index))
    for pid in raw_missing:
        prev = read_json(POSTS / f"{pid}.json", {})
        xs = (prev.get("extra") or {}).get("index_xsec_token")
        try:
            d = xhs_client.fetch_detail(cookies, pid, xs)
        except LoginExpired as e:
            update["possibly_missing"].append(pid)
            update["errors"].append(f"login_expired_during_missing_check: {e}")
            break
        except Exception as e:
            update["possibly_missing"].append(pid)
            print(f"  UNVERIFIED {pid} {type(e).__name__}: {str(e)[:120]}")
            time.sleep(1)
            continue
        entry = {
            "post_id": pid,
            "verified_at": now_iso(),
            "content_type": d.get("content_type"),
            "published_at": d.get("published_at"),
            "body_length": len(d.get("body_text") or ""),
        }
        update["index_missing_but_accessible"].append(entry)
        print(f"  MISSING_IN_INDEX_BUT_ACCESSIBLE {pid} type={d.get('content_type')}")
        # 顺手补全缺失的发布时间（元数据更新，不动内容哈希）
        if d.get("published_at") and not prev.get("published_at"):
            merged = dict(prev)
            merged["published_at"] = d["published_at"]
            merged.setdefault("extra", {})["previous_published_at"] = None
            merged["extra"]["last_metadata_sync"] = task_id
            write_json(POSTS / f"{pid}.json", merged)
            update["metadata_updated_posts"].append(pid)
        time.sleep(1)
    if update["possibly_missing"]:
        update["errors"].append(
            f"index_missing_notes: {len(update['possibly_missing'])} 篇未出现在本次索引且详情不可访问，已保留原数据待人工确认"
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
            # 内容哈希（v2，不含 metrics/published_at）变化 = 正文/图片/视频实际变化
            content_changed = rec["content_hash"] != prev.get("content_hash")
            # 元数据变化 = 发布时间补全、互动数刷新
            meta_changes = {}
            if rec.get("published_at") != prev.get("published_at"):
                meta_changes["published_at"] = rec.get("published_at")
            if rec["metrics"] != prev.get("metrics"):
                meta_changes["metrics"] = rec["metrics"]

            if content_changed:
                rec["extra"]["previous_published_at"] = prev.get("published_at")
                rec["extra"]["change_type"] = "content"
                write_json(f, rec)
                update["updated_posts"].append(pid)
                update["content_updated_posts"].append(pid)
                print(f"  UPDATED(content) {pid}")
            elif meta_changes:
                merged = dict(prev)
                if "published_at" in meta_changes:
                    merged["published_at"] = meta_changes["published_at"]
                if "metrics" in meta_changes:
                    merged["metrics"] = meta_changes["metrics"]
                merged.setdefault("extra", {})["last_metadata_sync"] = task_id
                if "published_at" in meta_changes:
                    merged["extra"]["previous_published_at"] = prev.get("published_at")
                merged["extra"]["change_type"] = "metadata"
                write_json(f, merged)
                update["updated_posts"].append(pid)
                update["metadata_updated_posts"].append(pid)
                print(f"  UPDATED(metadata) {pid} fields={sorted(meta_changes)}")
            else:
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

    # ---- 判定最终状态 ----
    status = "success"
    if update["failed_posts"] or update["errors"]:
        status = "partial"
    exit_code = 0 if status == "success" else 3

    # ---- 重建聚合文件 ----
    # 关键顺序：先把本轮结果写入 state/updates.json，再跑构建器，
    # 构建器读它生成 data/updates.json —— 保证公开文件与内部状态永远同轮。
    if args.build:
        import subprocess

        update["status"] = status
        update["finished_at"] = now_iso()
        write_json(STATE / "updates.json", update)

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
            update["status"] = "failed"
            write_json(STATE / "updates.json", update)
            print(json.dumps({"status": "failed", "exit": 4}, ensure_ascii=False))
            return 4
        print(json.dumps({
            "status": status, "exit": exit_code,
            "new_posts": update["new_posts"], "updated_posts": update["updated_posts"],
            "content_updated_posts": update["content_updated_posts"],
            "metadata_updated_posts": update["metadata_updated_posts"],
            "unchanged_posts": update["unchanged_posts"], "failed_posts": update["failed_posts"],
            "index_missing_but_accessible": len(update["index_missing_but_accessible"]),
            "possibly_missing": update["possibly_missing"],
        }, ensure_ascii=False))
        return exit_code
    return finish(status, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
