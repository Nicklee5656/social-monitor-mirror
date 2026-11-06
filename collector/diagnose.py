# 云端环境自检：确认依赖版本、签名可用性、平台可达性
# 只做只读请求，输出诊断信息到 stdout，便于在 Actions 日志里定位问题
import json
import os
import platform
import sys
import traceback

sys.path.insert(0, str(os.path.dirname(os.path.abspath(__file__))))

print(f"python={sys.version.split()[0]} platform={platform.platform()}")

try:
    import requests
    print(f"requests={requests.__version__}")
except Exception as e:
    print(f"requests_import_error={e}")

try:
    from xhshow import Xhshow
    import xhshow

    ver = getattr(xhshow, "__version__", None)
    if not ver:
        try:
            from xhshow._version import __version__ as ver
        except Exception:
            ver = "unknown"
    print(f"xhshow={ver}")
except Exception:
    print("xhshow_import_failed")
    traceback.print_exc()
    raise SystemExit(0)

raw = os.environ.get("XHS_COOKIES") or "{}"
try:
    cookies = json.loads(raw)
except Exception as e:
    print(f"cookie_json_invalid={e}")
    cookies = {}
print(f"cookie_keys={sorted(cookies.keys())}")

uri = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"
params = {"num": "30", "cursor": "", "user_id": os.environ.get("XHS_ACCOUNT_ID", "6245580f000000001000fce7"), "image_formats": "jpg"}
try:
    h = Xhshow().sign_headers_get(uri=uri, cookies=cookies, params=params)
    print(f"sign_ok headers={sorted(h.keys())}")
except Exception:
    print("sign_failed")
    traceback.print_exc()

# 平台可达性（不带签名，只看网络层）
try:
    r = requests.get("https://www.xiaohongshu.com/explore", timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    print(f"reachability status={r.status_code} bytes={len(r.content)}")
except Exception as e:
    print(f"reachability_failed={type(e).__name__}: {e}")
