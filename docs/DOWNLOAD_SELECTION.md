# 下载画质与音轨选择（2026-11-03）

本轮在原有队列、下载器和缓存上增加任务级偏好，继续复用已锁定的 yt-dlp MPD 解析、ISO639 语言映射和 FFmpeg 合并能力，没有增加依赖或转码流程。

## 可用流程

- 单个视频：详情 → 加入下载队列 → 读取当前清单选项 → 选择最高画质、音轨语言 → 确认。
- 多素材或目录选集：统一设置本批偏好，各任务根据实际源执行。
- 失败或等待授权的视频任务：调整选项后重试。正在下载、排队或暂停的任务不能直接改选项；旧任务默认保持原有自动选择。
- 队列和完成详情分别展示请求偏好与实际结果。参数随任务持久化，暂停、服务重启、链接刷新后保持一致。

最高画质表示从源站已有表示中选择不超过上限的版本，不是转码、超分或精确目标尺寸。匹配不到明确尺寸或语言时明确失败；不会静默回退到不符合要求的版本。HTTP 文件和图片保持源字节，不应用这些偏好。

语言支持常见两位/三位代码及地区标签，最多 35 字符。`eng` 规范化为 `en`；基础语言可选择同语种地区音轨。指定 `en-US` 不会自动改选明确标为 `en-GB` 的音轨；仅提供基础 `en` 时可兼容，并显示说明。语言依据清单元数据，不是语音识别。

HLS 外置音轨可按语言选择；无 URI 的内嵌音轨仅支持自动保留，不作为可选语言。固定 HLS 子清单缺少选择依据时不接受显式偏好。DASH 仍限受控静态、单 Period 格式。探测只读取最多 2 MiB 清单，不取媒体片段；最多并发 2 次、API 总预算 20 秒（含等待），下载器内部预算 15 秒。任意音轨 NAME 不回传，只使用规范化语言标签；临时探测文件在成功、失败和取消后清理。

偏好与具体表示参与缓存身份校验。更换画质或语言不复用旧表示的片段；相同偏好仍可校验恢复。输出再次经过 FFprobe 尺寸检查，超过上限不会替换原件。已下载文件仍受原件保护，不会因为重新提交其他偏好被覆盖。

## 本轮验收

| 项目 | 实测结果 |
|---|---|
| 全量后端 | 270 passed，151.64 秒；含 25 项引擎选择、14 项 API/队列新增回归 |
| 前端 | 54 passed；全部模块语法、Ruff 与 diff 检查通过 |
| 合成 HLS / DASH | 多画质、多语言源实际输出 144p 中文音轨，FFprobe 与全片解码通过；覆盖无匹配、地区语言、未知尺寸、改选项/同选项恢复 |
| 混合批量 | 5 轮、120 任务、15.405 秒；110 正常输出逐个全解码，5 个预设权限等待和 5 个故意损坏媒体失败，峰值并发 4；包含瞬态重试和 DASH 队列重建恢复 |
| Lustflix | 已保存会话在独立测试目录恢复；面板读到 360/480/720p，实际选择 360p 下载免费第 1 集 |
| 实际文件 | 640×360 H.264 + AAC，31.232 秒，3,076,052 字节，32/32 片段；网络重试 2 次后完成，全解码与文件 Range 206 通过 |
| 本地播放器 | 从头播放到片尾：currentTime=duration=31.232，ended=true，readyState=4，muted=false，error=null |
| 主服务 | 4173 已加载本轮代码；localhost/127.0.0.1 健康检查通过；254 条素材、2 个原文件和 Lustflix 会话保留，原文件 SHA 未变 |

Lustflix 样本是《Vampire Boyfriend》源站完整免费首集，页面路由 `/#/watch/C04/1`；不是 10 秒首页预览，也不能代表长篇或付费内容已验收。本轮 360p 文件单独保留为 `test-output/download-selection-sample.mp4`，没有覆盖主库的 720p 原件。其 SHA256 为 `f92474cf20b54e09ffe7891a977b26cbfe7f48e78c7f95f65327c17fa03adc23`。

Computer Use 另外验证主面板批量选择两个已下载文件、设置 480p 后提交，明确显示“0 项已加入，2 项已存在”。选集请求和重试选项通过前端请求合同与 API 集成测试；原件保护未因选项变化失效。

独立复核曾发现 HLS 内嵌音轨只改语言标签却没有真正选流，以及音轨名称可带签名地址；两项均已复现、修复、加入回归。真实 API 并发探针确认取消释放槽位，挂起请求 20.01 秒返回 504，后续请求正常。

本机证据：`test-output/download-selection-backend.log`、`download-selection-batch/video-batch.json`、`download-selection-live-lustflix.json`、`download-selection-playback.json`、`download-selection-main.json`、`download-selection-panel.jpg`。这些文件忽略提交，不包含会话令牌。更新前的 SQLite 与加密会话备份位于 `.framekeep/backups/download-selection-20261103`，权限受限。

## API 与复跑入口

`GET /api/assets/{id}/download-options` 返回协议、真实尺寸列表、音轨语言和说明。三个提交入口共用 `options`：

```json
{"maxHeight": 720, "audioLanguage": "en"}
```

- `POST /api/downloads`：`ids` + `options`。
- `POST /api/scans/{id}/downloads`：`episodeKeys` + `options`。
- `POST /api/jobs/{id}/action`：`action:"retry"` + `options`；其他动作不接受修改。

`maxHeight:null` 与 `audioLanguage:"auto"` 保持默认。任务记录 `downloadOptions`，完成结果记录 `selection`；素材记录 `downloadSelection`，私有传输字段不进入公开状态。

```sh
uv run pytest tests_backend -q
uv run ruff check backend tests_backend scripts
npm test
npm run check
uv run python scripts/video_batch_acceptance.py --rounds 5 --output test-output/download-selection-batch
```

排查顺序：确认源清单实际提供尺寸/语言 → 检查任务偏好与实际结果 → 需要时调整终态任务后重试。不要通过修改标签来代替真正选流；不要以短样本或合成批次推断全站覆盖。

余下验证包括较长真实剧集、其他三站账号、更多分页与持续运行。服务器 Docker 构建此前受 ENOSPC 阻塞，本轮未重试或清理用户镜像/卷；仍未通过容器运行验收。水印处理和超分不在本轮范围。
