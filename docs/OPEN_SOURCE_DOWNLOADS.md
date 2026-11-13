# 下载模块：开源能力接入与验收（2026-11-03）

后续 2026-11-13 的抖音、YouTube、TikTok、Instagram 接入和目标服务器样本验收见[单条社交视频下载](SOCIAL_DOWNLOADS.md)。下文保留 11 月 3 日的历史验收范围；YouTube 后续增加了官方 EJS / Deno 运行组件。

本轮优先验收 Lustflix；Candy、春楼、Moolight 的账号内内容由用户明确延后。保留通用扫描，不推断四站已全量支持。

## 已接入的能力与边界

| 能力 | 实现与范围 |
|---|---|
| 通用网页提取 | 复用锁定的 yt-dlp 2026.10.26，启用经检查的 GenericIE / HTML5MediaEmbedIE；支持真实 HTML5、JSON-LD、部分列表/RSS与 HLS/DASH候选。不是启用了上游全部站点专用 extractor。 |
| 动态页面 | Playwright 观察 DOM/网络；对原页渲染 HTML 使用同一提取器补充解析。仅当前页面扫描保留 `#/watch/...` 等 SPA 路由。 |
| 单条短视频及帖子 | 复用 yt-dlp 的 TikTok、Douyin、Instagram 解析能力。按作品 ID 选择公开或当前会话可访问的附件，优先来源明确标注的无水印版本。各平台实测边界见[拾取链接](INTAKE.md)。 |
| 静态 DASH | 用 yt-dlp 的离线 MPD 解析器处理单 Period、SegmentList/SegmentTemplate/有界 Timeline，选择一条视频和可选音轨。初始化/媒体片段受统一下载策略保护，缓存身份包含清单、表示和最终 URL；FFmpeg 在本地合并。 |
| 访问错误恢复 | 401/402/403 后最多刷新原页面一次；同页面、同剧目媒体 slug、集数、主清单/画质/音轨角色需一致。Lustflix仅允许已知签名参数旋转；未知站点要求精确源URL一致。候选含糊、截断、权限门或再次拒绝访问时进入“等待授权”。 |
| 剧集目录 | 只收录实际可见链接和 Lustflix 已观察的选集条目，不根据最大集号编造目录。显示枚举/解析/保存数量与权限状态；总数没有可靠证据时显示未知。选集扫描保留完整目录上下文，选集下载每集仅选择一个主媒体。 |
| 现有下载能力 | 保留 HTTP / HLS 校验续传、限次重试、持久队列、单素材单写入者、暂停/取消、SHA256和媒体校验、原件保护。 |

提取器所有网络走同一受控 HTTP 适配器，逐跳验证目标与 Cookie/Authorization 作用域；禁用插件、额外运行时与外部命令。每响应 4 MiB、总计 16 MiB、32 个请求、20 个列表条目；30 秒提取预算之外初次 DNS 策略校验最多另 8 秒。为在解压前控制体积，目前要求 identity 元数据响应，压缩元数据响应明确不支持。

DASH 拒绝 DRM、直播/动态清单、多 Period、SegmentBase、字节范围、无界重复和超预算展开；清单最多 2 MiB、64 个表示、10000 个总展开片段、7 天声明时长。后续已加入画质上限与音轨语言偏好，见 [下载选项验收](DOWNLOAD_SELECTION.md)。HLS 的字节范围、变量、会话密钥、低延迟扩展仍不支持。会话与付款仍由用户管理；无去水印、超分或自动购买。

上游依据：[yt-dlp](https://github.com/yt-dlp/yt-dlp)。本轮没有添加尚无实际用途的 gallery-dl/aria2 依赖；内部解析 API 的兼容性由锁定版本的真实集成测试约束。

## 验收事实

- 真实受控浏览器：仅含 JSON-LD、没有 video DOM 的页面，在未接提取器时发现 0 个视频；接入后发现并通过普通下载器保存 1 秒 H264/AAC 视频，SHA、双轨、时长和 FFmpeg 全解码通过。
- DASH：FFmpeg 生成独立音视频清单，验证完整下载、双轨/时长/全解码；暂停后新下载器复用已确认片段，清单变更使旧片段失效；权限、私网、DRM、动态和展开预算有回归覆盖。
- 混合批次：`scripts/video_batch_acceptance.py --rounds 20` 实际 61.353 秒，480 个任务；440 个正常输出逐个验证大小、SHA、音视频轨道和完整解码。每轮 22 completed、1 needs-user（403）、1 failed（故意损坏媒体），峰值并发 4；每轮实际包含 429/503 后恢复以及 DASH 暂停、新队列实例恢复。共观察到 20 次429、20次503、20次403、158次304及1次416后安全重取。它是有界合成短视频批次，不代表连续数日或真实站点全站压力验收。
- 批次首次长跑在第3轮失败，诊断重现后确认是夹具对 EOF Range 错误断连。修复夹具为标准416并增加EOF/越界/有效Range自检后，完整20轮通过。失败证据保留，不作为通过证据。
- Lustflix：实际扫描已知 `/#/watch/C04/1`，自动展开只读选集控件，枚举5个可见条目。第1集有正片候选且已保存；第2–5集显示会员权限要求。没有可靠总数证据，目录仍显示“总数未知”，不宣称全站完整。
- Computer Use 面板实测：全选5集后下载，返回加入0、需权限/未解析4、已保存1；勾选第1集重新扫描，确实只访问1页并保留5集目录。
- Lustflix 第一集重新下载：新服务实例恢复、复用1,542,728字节，32片段全部完成，8,762,527字节；SHA256仍为 `0c6c58af0f29f4d0b48aa2893b1beeab1b334c7e61594da703cafa81e39b0ce5`，完整音视频解码和文件Range206通过。它是源站31.232秒的完整免费单集，不是10秒首页预览；也不是长篇付费剧集验收。

完整后端/前端测试计数见 [QA](QA.md)。本机证据（忽略提交）为 `test-output/opensource-expansion-backend-final.txt`、`opensource-expansion-frontend-final.txt`、`opensource-expansion-live-lustflix.json`、`opensource-expansion-catalog.json`、`video-batch-acceptance/video-batch.json`。

## 复跑与部署

```sh
uv run pytest tests_backend -q
uv run ruff check backend tests_backend scripts
npm test
npm run check
uv run python scripts/download_acceptance.py
uv run python scripts/video_batch_acceptance.py --rounds 20
```

视频批次只访问本地生成的媒体，逐轮清理临时媒体和SQLite，仅保存小型JSON；可以调整轮数（1–1000），必须报告实际运行时长。已有长视频、271MiB文件续传与真实SIGKILL恢复证据见 [下载验收](DOWNLOAD_ACCEPTANCE.md)。

服务器配置仍在 Dockerfile/compose.yaml。镜像已改为只安装实际使用的 Chromium headless shell，减少不必要浏览器体积；本机两次构建均因 Docker/containerd 存储不足（ENOSPC）失败，第二次在导出层时失败，因此没有服务器容器运行验收通过的结论。未清理用户已有镜像/卷；仅移除本轮失败构建容器。服务器部署前须在有足够Docker存储的环境重建并验证登录、媒体与重启恢复。
