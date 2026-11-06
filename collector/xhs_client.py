# 小红书只读采集客户端（纯 HTTP，无需浏览器）
#
# 用途：GitHub Actions 云端任务使用。仅做只读请求：
#   - 账号笔记索引  /api/sns/web/v1/user_posted
#   - 登录态探针    /api/sns/web/v2/user/me
#   - 笔记详情      笔记页 SSR（window.__INITIAL_STATE__）
# 不点赞、不关注、不发布，不绕过任何验证码或风控。
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone

import requests
from xhshow import Xhshow

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)


def _warn(msg):
    print(msg, file=sys.stderr, flush=True)


def _tb(limit=3):
    return "".join(traceback.format_exc(limit=limit)).replace("\n", " | ")[-400:]

INDEX_URI = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
ME_URI = "https://edith.xiaohongshu.com/api/sns/web/v2/user/me"

_client = Xhshow()


class LoginExpired(RuntimeError):
    """登录态失效或需要人机验证，必须由用户处理，不得绕过。"""


class FetchError(RuntimeError):
    pass


def _session(cookies):
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.xiaohongshu.com/",
        }
    )
    s.cookies.update(cookies)
    return s


def _signed_get(session, uri, params, cookies):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.xiaohongshu.com",
        **_client.sign_headers_get(uri=uri, cookies=cookies, params=params),
    }
    r = session.get(uri, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def check_login(cookies):
    """用 user/me 判断登录态。游客态同样带 web_session cookie，不能只看 cookie 存在与否。"""
    session = _session(cookies)
    try:
        body = _signed_get(session, ME_URI, {}, cookies)
    except Exception as e:
        raise FetchError(f"login probe failed: {type(e).__name__}") from e
    data = body.get("data") or {}
    guest = data.get("guest")
    if body.get("code") not in (0, None) or guest is True or not data.get("user_id"):
        raise LoginExpired(f"login probe rejected: code={body.get('code')} guest={guest}")
    return {"user_id": data.get("user_id"), "nickname": data.get("nickname")}


def fetch_index(cookies, user_id, max_pages=40, delay=1.2):
    """分页拉取账号笔记索引。返回 {note_id: note}。"""
    session = _session(cookies)
    notes = {}
    cursor = ""
    seen = set()
    for page in range(1, max_pages + 1):
        params = {"num": "30", "cursor": cursor, "user_id": user_id, "image_formats": "jpg"}
        try:
            body = _signed_get(session, INDEX_URI, params, cookies)
        except Exception as e:
            if notes:
                break  # 已拿到部分索引，保留并返回，由上层判定 partial
            raise FetchError(f"index page {page} failed: {type(e).__name__}: {e} :: {_tb()}") from e

        if body.get("code") not in (0, None):
            msg = json.dumps(body, ensure_ascii=False)[:200]
            if body.get("code") in (300011, 300012, -100):
                raise LoginExpired(f"index rejected: {msg}")
            if notes:
                break
            raise FetchError(f"index page {page} api error: {msg}")

        data = body.get("data") or {}
        items = data.get("notes") or []
        for n in items:
            nid = n.get("note_id") or n.get("id")
            if nid:
                notes[nid] = n
        next_cursor = data.get("cursor") or ""
        if not data.get("has_more") or not next_cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
        time.sleep(delay)
    return notes


def _parse_state(html):
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return None
    raw = m.group(1)
    # SSR 里会直接写 undefined，不是合法 JSON
    raw = re.sub(r"\bundefined\b", "null", raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


def _pick_image_url(im):
    for k in ("urlDefault", "urlPre", "url"):
        v = im.get(k)
        if isinstance(v, str) and v:
            return v.replace("http://", "https://")
    info = im.get("infoList") or []
    for i in info:
        if i.get("url"):
            return i["url"].replace("http://", "https://")
    return None


def _pick_video_url(video):
    media = (video or {}).get("media") or {}
    stream = media.get("stream") or {}
    for codec in ("h264", "h265", "av1"):
        for item in stream.get(codec) or []:
            if item.get("masterUrl"):
                return item["masterUrl"]
    return None


def fetch_detail(cookies, note_id, xsec_token=None, retries=2):
    """抓单篇详情。返回与源库单篇同构的字段（不含 completeness/mirror，由构建脚本补齐）。"""
    session = _session(cookies)
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_user"

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = session.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://www.xiaohongshu.com/"},
                timeout=30,
            )
            if r.status_code == 404:
                raise FetchError("note_not_found(404)")
            r.raise_for_status()
            state = _parse_state(r.text)
            if not state:
                raise FetchError("ssr_state_missing")
            detail = (state.get("note") or {}).get("noteDetailMap") or {}
            entry = detail.get(note_id) or next(iter(detail.values()), None)
            if not entry or not entry.get("note"):
                # 页面能开但无数据：多为登录态失效或需要验证
                if "登录" in r.text[:20000] or "验证" in r.text[:20000]:
                    raise LoginExpired("detail requires login/verification")
                raise FetchError("note_payload_missing")
            return _normalize(entry["note"], note_id, xsec_token)
        except LoginExpired:
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 + attempt * 3)
    raise FetchError(f"detail failed: {type(last_err).__name__}: {last_err}")


def _normalize(note, note_id, xsec_token):
    imgs = []
    for i, im in enumerate(note.get("imageList") or [], start=1):
        u = _pick_image_url(im)
        imgs.append(
            {
                "position": i,
                "source_url": u,
                "width": im.get("width"),
                "height": im.get("height"),
                "extracted_text": None,
                "extraction_status": "pending" if u else "source_unavailable",
            }
        )

    video = note.get("video") or {}
    vurl = _pick_video_url(video) if video else None
    is_video = note.get("type") == "video" or bool(video)

    ts = note.get("time")
    published_at = None
    date_text = None
    if isinstance(ts, (int, float)) and ts > 0:
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
        published_at = dt.isoformat(timespec="seconds")
        date_text = dt.strftime("%Y-%m-%d")

    interact = note.get("interactInfo") or {}
    return {
        "note_id": note_id,
        "title": (note.get("title") or "").strip() or None,
        "body_text": (note.get("desc") or "").strip() or None,
        "content_type": "video" if is_video else "image",
        "published_at": published_at,
        "date_text": date_text,
        "images": imgs,
        "video_url": vurl,
        "video_poster": None,
        "metrics": {
            "likes": interact.get("likedCount") or None,
            "favorites": interact.get("collectedCount") or None,
            "comments": interact.get("commentCount") or None,
        },
        "author": (note.get("user") or {}).get("nickname"),
        "xsec_token": note.get("xsecToken") or xsec_token,
    }
