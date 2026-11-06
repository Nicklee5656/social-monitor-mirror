# social-monitor 数据镜像

小红书账号「Kikstart Ecom 凯客数智」（`6245580f000000001000fce7`）内容数据集静态镜像。

- 数据版本：`2026.11.06-1`
- 内容指纹：`8e7edb363d58`
- 更新时间：`2026-11-06T14:32:50+08:00`
- 本仓库含 **173** 篇（累计已采集 173 篇）

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

当前统计：`{"text_only": 173}`

**本数据集没有任何一篇达到 `all_materials`**：图片 OCR 与视频转录尚未执行，`published_at` 平台不提供。
