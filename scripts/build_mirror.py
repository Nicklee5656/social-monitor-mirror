# 构建静态镜像 mirror/（GitHub Pages + jsDelivr + raw）
#
# 用法:
#   python scripts/build_mirror.py --mode sample
#   python scripts/build_mirror.py --mode full --repo owner/repo
#
# 产出:
#   manifest.json          数据集入口：账号/总量/版本/更新时间/文件入口/完整性
#   data/version.json      极小版本探针（用于绕过 CDN 缓存判断最新版本）
#   data/posts.json        全部正文汇总（可变，别名）
#   data/posts-<fp>.json   全部正文汇总（不可变，按内容指纹命名，CDN 缓存安全）
#   data/index.json        索引（每篇含完整地址与完整性摘要）
#   data/updates.json      最新一次增量
#   posts/<post_id>.json   单篇完整内容（含 completeness 块）
#   urls.txt               全部可读地址清单
import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "posts"
DATA = ROOT / "data"
MIRROR = ROOT / "mirror"
STATE = DATA / "mirror-state.json"
UPDATES_SRC = DATA / "updates.json"

SAMPLE_IDS = [
    "638c1939000000001f00d6fa",
    "674b06d60000000008006d11",
    "6a01a73e0000000036030173",
]

ACCOUNT_ID = "6245580f000000001000fce7"
ACCOUNT_LABEL = "Kikstart Ecom 凯客数智"
PROFILE_URL = f"https://www.xiaohongshu.com/user/profile/{ACCOUNT_ID}"


def write_json(path: Path, obj, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        obj,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
    )
    path.write_text(text, "utf-8")


def read_json(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def completeness_of(p):
    """按实际采集内容判定素材完整度，绝不把「只有正文」当成「全部素材完整」。"""
    ctype = p.get("content_type")
    body = (p.get("body_text") or "").strip()
    images = p.get("images") or []
    video = p.get("video") or {}
    date_text = (p.get("extra") or {}).get("date_text")

    captured = [im for im in images if im.get("source_url")]
    ocr_done = [im for im in images if im.get("extracted_text")]

    gaps = []
    if not body:
        gaps.append("body_text")
    if p.get("published_at") is None:
        gaps.append("published_at")

    image_block = {"expected": len(images), "captured": len(captured), "ocr_done": len(ocr_done)}
    if images:
        if len(captured) == len(images):
            image_block["status"] = "captured"
        elif captured:
            image_block["status"] = "partial"
            gaps.append("images_partial")
        else:
            image_block["status"] = "failed"
            gaps.append("images_missing")
    else:
        image_block["status"] = "not_applicable" if ctype == "video" else "empty"

    if images:
        if ocr_done:
            image_text = {"status": "present", "count": len(ocr_done)}
        else:
            image_text = {"status": "missing", "note": "图片文字未做 OCR"}
            gaps.append("image_text")
    else:
        image_text = {"status": "not_applicable"}

    if ctype == "video":
        video_block = {"status": "captured" if video.get("source_url") else "url_missing"}
        if not video.get("source_url"):
            gaps.append("video_url")
        if video.get("transcript"):
            transcript = {"status": "present"}
        else:
            transcript = {"status": "missing", "note": "视频语音转录未执行"}
            gaps.append("video_transcript")
    else:
        video_block = {"status": "not_applicable"}
        transcript = {"status": "not_applicable"}

    if p.get("content_status") == "failed":
        level = "failed"
    elif not body:
        level = "partial"
    elif gaps:
        level = "text_only"
    else:
        level = "all_materials"

    return {
        "level": level,
        "body_text": bool(body),
        "images": image_block,
        "image_text": image_text,
        "video": video_block,
        "video_transcript": transcript,
        "published_at": {
            "status": "present" if p.get("published_at") else "missing",
            "note": "网页端仅提供相对/编辑时间，原文见 date_text",
            "date_text": date_text,
        },
        "all_materials_complete": level == "all_materials",
        "gaps": sorted(set(gaps)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sample", "full"], default="sample")
    ap.add_argument("--repo", default="")
    ap.add_argument("--branch", default="main")
    # 云端（GitHub Actions）单仓库布局用：源单篇与发布产物在同一棵树下
    ap.add_argument("--posts-dir", default="", help="源单篇目录，默认 ROOT/posts")
    ap.add_argument("--mirror-dir", default="", help="发布目录，默认 ROOT/mirror")
    ap.add_argument("--updates-src", default="", help="增量记录来源，默认 data/updates.json")
    ap.add_argument("--state-file", default="", help="版本状态文件，默认 data/mirror-state.json")
    ap.add_argument(
        "--keep",
        default=".git,collector,scripts,state,.github",
        help="当发布目录等于仓库根时，这些顶层条目不清空",
    )
    args = ap.parse_args()

    global POSTS, MIRROR, UPDATES_SRC, STATE
    if args.posts_dir:
        POSTS = Path(args.posts_dir).resolve()
    if args.mirror_dir:
        MIRROR = Path(args.mirror_dir).resolve()
    if args.state_file:
        STATE = Path(args.state_file).resolve()
    UPDATES_SRC = Path(args.updates_src).resolve() if args.updates_src else DATA / "updates.json"
    keep = {k.strip() for k in args.keep.split(",") if k.strip()}

    # 保留 .git（避免每次强制推送丢失历史）和历史冻结快照 data/posts-<旧指纹>.json
    if MIRROR.exists():
        for child in sorted(MIRROR.rglob("*"), reverse=True):
            if ".git" in child.parts:
                continue
            # 源单篇目录永不清空（云端布局下它就在发布目录里）
            if child == POSTS or POSTS in child.parents:
                continue
            # 发布根=仓库根时，保留脚本与状态目录（含其下所有层级）
            try:
                top = child.relative_to(MIRROR).parts[0]
            except ValueError:
                top = ""
            if top in keep:
                continue
            if child.is_file() and child.parent == MIRROR / "data" and child.name.startswith("posts-"):
                continue  # 历史冻结快照永久保留
            if child.is_dir():
                if not any(child.iterdir()):
                    child.rmdir()
            else:
                child.unlink()
    (MIRROR / "data").mkdir(parents=True, exist_ok=True)
    (MIRROR / "posts").mkdir(parents=True, exist_ok=True)

    all_ids = sorted(p.stem for p in POSTS.glob("*.json"))
    ids = SAMPLE_IDS if args.mode == "sample" else all_ids
    ids = [i for i in ids if (POSTS / f"{i}.json").exists()]

    repo = args.repo
    if repo:
        owner, name = repo.split("/")[0], repo.split("/")[1]

        def main_url(p):
            return f"https://cdn.jsdelivr.net/gh/{repo}@{args.branch}/{p}"

        def pages_url(p):
            return f"https://{owner.lower()}.github.io/{name}/{p}"

        def raw_url(p):
            return f"https://raw.githubusercontent.com/{repo}/{args.branch}/{p}"
    else:

        def main_url(p):
            return p

        pages_url = raw_url = main_url

    # ---- 指纹与版本号 ----
    pairs = []
    for pid in ids:
        p = read_json(POSTS / f"{pid}.json", {})
        pairs.append(f"{pid}:{p.get('content_hash') or ''}")
    fingerprint = hashlib.sha256("|".join(sorted(pairs)).encode()).hexdigest()[:12]

    prev_state = read_json(STATE, {}) or {}
    prev_rev = int(prev_state.get("revision") or 0)
    prev_fp = prev_state.get("fingerprint")
    now = datetime.now().astimezone()
    unchanged = prev_fp == fingerprint and prev_state.get("dataset_version")
    if unchanged:
        # 内容未变：沿用原版本号与更新时间，保证构建幂等、不产生无意义提交
        revision = prev_rev or 1
        dataset_version = prev_state["dataset_version"]
        updated_at = prev_state.get("updated_at") or now.isoformat(timespec="seconds")
    else:
        revision = 1 if prev_fp is None else prev_rev + 1
        revision = revision or 1
        if args.mode != "full":
            revision = prev_rev or 1
        dataset_version = f"{now.strftime('%Y.%m.%d')}-{revision}"
        updated_at = now.isoformat(timespec="seconds")

    posts_by_id = {pid: read_json(POSTS / f"{pid}.json", {}) for pid in ids}

    # ---- 逐篇：写入单篇文件 + 计算完整性 ----
    index_entries = []
    digest = []
    level_counts = {}
    gap_counts = {}

    for pid in ids:
        p = posts_by_id[pid]
        comp = completeness_of(p)
        level_counts[comp["level"]] = level_counts.get(comp["level"], 0) + 1
        for g in comp["gaps"]:
            gap_counts[g] = gap_counts.get(g, 0) + 1

        rel = f"posts/{pid}.json"
        out = dict(p)
        out["completeness"] = comp
        # 与全量 posts.json 保持同名扁平字段，便于同一个解析器直接读
        out["completeness_level"] = comp["level"]
        out["all_materials_complete"] = comp["all_materials_complete"]
        out["gaps"] = comp["gaps"]
        out["mirror"] = {
            "dataset_version": dataset_version,
            "content_fingerprint": fingerprint,
            "post_url": main_url(rel),
            "post_url_pages": pages_url(rel),
        }
        write_json(MIRROR / rel, out)

        index_entries.append(
            {
                "post_id": pid,
                "content_type": p.get("content_type"),
                "title": p.get("title"),
                "body_length": len((p.get("body_text") or "")),
                "image_count": len(p.get("images") or []),
                "image_with_url": sum(1 for im in (p.get("images") or []) if im.get("source_url")),
                "has_video": bool((p.get("video") or {}).get("source_url")),
                "collected_at": p.get("collected_at"),
                "completeness_level": comp["level"],
                "all_materials_complete": comp["all_materials_complete"],
                "gaps": comp["gaps"],
                "path": rel,
                "url": main_url(rel),
                "url_pages": pages_url(rel),
            }
        )

        v = p.get("video") or {}
        digest.append(
            {
                "post_id": pid,
                "url": p.get("url"),
                "content_type": p.get("content_type"),
                "title": p.get("title"),
                "body_text": p.get("body_text"),
                "metrics": p.get("metrics"),
                "image_count": len(p.get("images") or []),
                "images": [
                    {
                        "position": im.get("position"),
                        "source_url": im.get("source_url"),
                        "extracted_text": im.get("extracted_text"),
                        "extraction_status": im.get("extraction_status"),
                    }
                    for im in (p.get("images") or [])
                ],
                "video": {
                    "source_url": v.get("source_url"),
                    "transcript": v.get("transcript"),
                    "transcript_status": v.get("transcript_status"),
                    "poster_url": v.get("poster_url"),
                },
                "published_at": p.get("published_at"),
                "date_text": (p.get("extra") or {}).get("date_text"),
                "content_status": p.get("content_status"),
                "completeness_level": comp["level"],
                "all_materials_complete": comp["all_materials_complete"],
                "gaps": comp["gaps"],
            }
        )

    access = {
        "login_required": False,
        "cookie_required": False,
        "sharecode_required": False,
        "auth_note": "静态镜像：匿名 HTTPS GET 即可读取，无需任何查询参数、Cookie 或登录",
    }

    # ---- data/posts-<fp>.json（不可变）+ data/posts.json（别名） ----
    posts_doc = {
        "schema_version": "1.0",
        "dataset": "social-monitor",
        "dataset_version": dataset_version,
        "content_fingerprint": fingerprint,
        "generated_at": updated_at,
        "mirror_mode": args.mode,
        "access": access,
        "counts": {"posts": len(digest)},
        "completeness_summary": {
            "levels": level_counts,
            "gap_counts": gap_counts,
            "all_materials_complete_posts": sum(1 for d in digest if d["all_materials_complete"]),
        },
        "posts": digest,
    }
    write_json(MIRROR / "data" / f"posts-{fingerprint}.json", posts_doc, compact=True)
    write_json(MIRROR / "data" / "posts.json", posts_doc, compact=True)

    # 清掉旧指纹的冻结文件，避免读者误取过期快照
    for stale in (MIRROR / "data").glob("posts-*.json"):
        if stale.name != f"posts-{fingerprint}.json":
            stale.unlink()

    # ---- data/index.json ----
    index_doc = {
        "schema_version": "1.0",
        "platform": "xiaohongshu",
        "dataset": "social-monitor",
        "account_id": ACCOUNT_ID,
        "account_label": ACCOUNT_LABEL,
        "profile_url": PROFILE_URL,
        "dataset_version": dataset_version,
        "content_fingerprint": fingerprint,
        "generated_at": updated_at,
        "mirror_mode": args.mode,
        "access": access,
        "counts": {"posts": len(ids), "total_collected": len(all_ids)},
        "completeness_summary": {
            "levels": level_counts,
            "gap_counts": gap_counts,
            "all_materials_complete_posts": sum(
                1 for e in index_entries if e["all_materials_complete"]
            ),
            "note": "text_only = 正文完整但图片文字/视频转录缺失，不代表全部素材采集完整",
        },
        "posts": index_entries,
    }
    write_json(MIRROR / "data" / "index.json", index_doc)

    # ---- data/updates.json ----
    src_upd = read_json(UPDATES_SRC, {}) or {}
    updates_doc = {
        "schema_version": "1.0",
        "dataset": "social-monitor",
        "dataset_version": dataset_version,
        "content_fingerprint": fingerprint,
        "generated_at": updated_at,
        "task_id": src_upd.get("task_id"),
        "task_status": src_upd.get("status"),
        "started_at": src_upd.get("started_at"),
        "finished_at": src_upd.get("finished_at"),
        "accounts_monitored": src_upd.get("accounts_monitored", [ACCOUNT_ID]),
        "new_posts": src_upd.get("new_posts", []),
        "updated_posts": src_upd.get("updated_posts", []),
        "unchanged_posts_count": len(
            src_upd.get("unchanged_posts") or src_upd.get("known_posts_skipped") or []
        ),
        "known_posts_skipped_count": len(src_upd.get("known_posts_skipped") or []),
        "partial_posts": src_upd.get("partial_posts", []),
        "failed_posts": src_upd.get("failed_posts", []),
        "deleted_or_unavailable": src_upd.get("deleted_or_unavailable", []),
        "errors": src_upd.get("errors", []),
        "post_urls": {
            nid: main_url(f"posts/{nid}.json")
            for nid in (src_upd.get("new_posts", []) + src_upd.get("updated_posts", []))
        },
        "note": "new_posts / updated_posts 为空表示本次未发现新笔记或内容变化",
    }
    write_json(MIRROR / "data" / "updates.json", updates_doc)

    images_total = sum(e["image_count"] for e in index_entries)
    images_with_url = sum(e["image_with_url"] for e in index_entries)

    # ---- manifest.json ----
    manifest = {
        "schema_version": "1.0",
        "dataset": "social-monitor",
        "platform": "xiaohongshu",
        "mirror_mode": args.mode,
        "dataset_version": dataset_version,
        "revision": revision,
        "content_fingerprint": fingerprint,
        "updated_at": updated_at,
        "access": access,
        "account": {
            "account_id": ACCOUNT_ID,
            "label": ACCOUNT_LABEL,
            "profile_url": PROFILE_URL,
        },
        "accounts": [
            {"account_id": ACCOUNT_ID, "label": ACCOUNT_LABEL, "profile_url": PROFILE_URL}
        ],
        "counts": {
            "posts": len(ids),
            "total_collected": len(all_ids),
            "image_posts": sum(1 for e in index_entries if e["content_type"] == "image"),
            "video_posts": sum(1 for e in index_entries if e["content_type"] == "video"),
            "with_body_text": sum(1 for e in index_entries if e["body_length"] > 0),
            "with_video_url": sum(1 for e in index_entries if e["has_video"]),
            "images_total": images_total,
            "images_with_url": images_with_url,
        },
        "completeness_summary": {
            "levels": level_counts,
            "gap_counts": gap_counts,
            "all_materials_complete_posts": sum(
                1 for e in index_entries if e["all_materials_complete"]
            ),
            "definition": {
                "all_materials": "正文 + 图片地址 + 图片文字 OCR + 视频流 + 视频转录 + 绝对发布时间 全部具备",
                "text_only": "正文完整、图片地址已抓取，但缺少图片文字 OCR 或视频转录或绝对发布时间",
                "partial": "正文缺失或图片地址抓取不全",
                "failed": "采集失败",
            },
            "warning": "本数据集当前没有任何一篇达到 all_materials：图片 OCR 与视频转录未执行，published_at 平台不提供",
        },
        "entrypoints": {
            "version_probe": main_url("data/version.json"),
            "single_file_everything": main_url("data/posts.json"),
            "single_file_frozen": main_url(f"data/posts-{fingerprint}.json"),
            "index": main_url("data/index.json"),
            "updates": main_url("data/updates.json"),
            "one_post_example": main_url(f"posts/{ids[0]}.json"),
            "all_urls_plaintext": main_url("urls.txt"),
            "same_via_pages": pages_url("data/posts.json"),
            "same_via_raw_github": raw_url("data/posts.json"),
        },
        "cache_policy": {
            "read_order": [
                "1) GET data/version.json（约 300B，读 dataset_version 与 content_fingerprint）",
                "2) GET data/posts-<content_fingerprint>.json（文件名含指纹，内容永不变，CDN 缓存安全）",
                "3) 若需最新可变文件，改用 GitHub Pages 地址校验",
            ],
            "mutable_files": [
                "manifest.json",
                "data/version.json",
                "data/posts.json",
                "data/index.json",
                "data/updates.json",
            ],
            "immutable_files": [f"data/posts-{fingerprint}.json"],
            "note": "jsDelivr 会缓存 @main 上的可变文件；用带指纹的文件名可彻底避开旧缓存",
        },
        "how_to_read": [
            "最省事：直接 GET data/posts.json —— 一个文件拿到全部笔记标题+正文+互动数",
            "需要逐篇细节：先 GET data/index.json，每条记录的 url 字段是完整可读地址",
            "所有地址都不需要加任何查询参数",
            "判断是否最新：读 data/version.json 的 content_fingerprint，与 posts-<指纹>.json 的文件名比对",
        ],
        "known_limitations": [
            "published_at 为 null：网页端只提供相对时间或编辑时间，原文保留在 date_text",
            "图片文字 OCR 未执行：images[].extracted_text 为 null，extraction_status=pending",
            "视频语音转录与字幕未执行：video.transcript 为 null，transcript_status=missing",
            "视频流地址为带签名的 CDN 链接，会过期",
            "counts.posts 是本系统实际收录数，不等于账号在平台上的全部笔记数",
        ],
        "files": {
            "manifest": "manifest.json",
            "version": "data/version.json",
            "posts": "data/posts.json",
            "posts_frozen": f"data/posts-{fingerprint}.json",
            "index": "data/index.json",
            "updates": "data/updates.json",
        },
    }
    write_json(MIRROR / "manifest.json", manifest)

    # ---- data/version.json（极小，用于绕过缓存判断最新） ----
    write_json(
        MIRROR / "data" / "version.json",
        {
            "dataset": "social-monitor",
            "dataset_version": dataset_version,
            "revision": revision,
            "content_fingerprint": fingerprint,
            "updated_at": updated_at,
            "posts": len(ids),
            "posts_frozen_file": f"data/posts-{fingerprint}.json",
            "posts_frozen_url": main_url(f"data/posts-{fingerprint}.json"),
            "posts_json_sha256": hashlib.sha256(
                (MIRROR / "data" / "posts.json").read_bytes()
            ).hexdigest(),
            "note": "先读本文件取 content_fingerprint，再读 posts-<指纹>.json，可确保不受 CDN 旧缓存影响",
        },
    )

    # ---- urls.txt ----
    lines = [
        "# social-monitor 数据集全部可读地址（无需任何参数）",
        f"# dataset_version: {dataset_version}  fingerprint: {fingerprint}",
        main_url("data/version.json"),
        main_url("manifest.json"),
        main_url("data/posts.json"),
        main_url(f"data/posts-{fingerprint}.json"),
        main_url("data/index.json"),
        main_url("data/updates.json"),
        "",
        f"# 单篇笔记 {len(ids)} 个",
    ]
    lines += [main_url(f"posts/{pid}.json") for pid in ids]
    (MIRROR / "urls.txt").write_text("\n".join(lines) + "\n", "utf-8")

    (MIRROR / ".nojekyll").write_text("", "utf-8")

    (MIRROR / "README.md").write_text(
        f"""# social-monitor 数据镜像

小红书账号「{ACCOUNT_LABEL}」（`{ACCOUNT_ID}`）内容数据集静态镜像。

- 数据版本：`{dataset_version}`
- 内容指纹：`{fingerprint}`
- 更新时间：`{updated_at}`
- 本仓库含 **{len(ids)}** 篇（累计已采集 {len(all_ids)} 篇）

## 读取方式

无需登录、Cookie、Token 或任何查询参数，直接 HTTPS GET：

| 文件 | 内容 |
|---|---|
| `data/version.json` | 版本探针（约 300B），含 `content_fingerprint` |
| `data/posts-<指纹>.json` | 全量正文（文件名含指纹，内容永不变） |
| `data/posts.json` | 全量正文（可变别名） |
| `data/index.json` | 索引 + 每篇完整地址 + 完整性摘要 |
| `data/updates.json` | 最新一次增量 |
| `posts/<post_id>.json` | 单篇完整内容 |
| `manifest.json` | 数据集入口与统计 |

## 完整性说明

每篇含 `completeness` 块，`level` 取值：

- `all_materials`：全部素材齐备
- `text_only`：正文完整，但缺图片文字 OCR / 视频转录 / 绝对发布时间
- `partial`：正文缺失或图片抓取不全
- `failed`：采集失败

当前统计：`{json.dumps(level_counts, ensure_ascii=False)}`

**本数据集没有任何一篇达到 `all_materials`**：图片 OCR 与视频转录尚未执行，`published_at` 平台不提供。
""",
        "utf-8",
    )

    (MIRROR / "index.html").write_text(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>social-monitor 静态镜像 {dataset_version}</title>
<style>body{{font-family:-apple-system,"Segoe UI",sans-serif;max-width:780px;margin:50px auto;padding:0 20px;line-height:1.7;color:#222}}
code{{background:#f4f4f5;padding:2px 6px;border-radius:4px;word-break:break-all}}a{{color:#c1362f}}
.tag{{display:inline-block;background:#f4f4f5;border-radius:10px;padding:1px 8px;font-size:12px}}</style></head><body>
<h1>social-monitor 静态镜像</h1>
<p>小红书账号「{ACCOUNT_LABEL}」内容数据集，共 <b>{len(ids)}</b> 篇（累计采集 {len(all_ids)} 篇）。无需登录、Cookie 或分享码。</p>
<p><span class="tag">version {dataset_version}</span> <span class="tag">fingerprint {fingerprint}</span> <span class="tag">{updated_at}</span></p>
<ul>
<li><a href="data/version.json">data/version.json</a> — 版本探针（判断是否最新）</li>
<li><a href="data/posts.json">data/posts.json</a> — 全部正文汇总（单文件，推荐）</li>
<li><a href="data/posts-{fingerprint}.json">data/posts-{fingerprint}.json</a> — 同上，带指纹、缓存安全</li>
<li><a href="data/index.json">data/index.json</a> — 索引 + 每篇地址</li>
<li><a href="data/updates.json">data/updates.json</a> — 最新增量</li>
<li><a href="manifest.json">manifest.json</a> — 数据集入口</li>
</ul>
<h2>完整性</h2>
<p>{json.dumps(level_counts, ensure_ascii=False)}</p>
<p>没有任何一篇达到 all_materials：图片 OCR、视频转录未执行，发布时间平台不提供。</p>
</body></html>""",
        "utf-8",
    )

    if args.mode == "full":
        write_json(
            STATE,
            {
                "revision": revision,
                "fingerprint": fingerprint,
                "dataset_version": dataset_version,
                "updated_at": updated_at,
                "posts": len(ids),
            },
        )

    print(
        f"MIRROR_BUILT mode={args.mode} posts={len(ids)} version={dataset_version} fp={fingerprint}"
    )
    for f in (
        "manifest.json",
        "data/version.json",
        "data/index.json",
        "data/updates.json",
        "data/posts.json",
        f"data/posts-{fingerprint}.json",
    ):
        print(f"  {f:<32} {(MIRROR / f).stat().st_size:>9}B")
    print("  completeness:", json.dumps(level_counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
