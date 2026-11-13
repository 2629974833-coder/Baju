# Framekeep 个人服务器部署

本服务按单管理员、单后端进程运行：FastAPI 提供前端与 API，Playwright 提供站点会话，SQLite 保存任务和素材记录，CPU 上的 Pillow/FFmpeg 处理图片与视频。不需要 Redis 或 GPU。工作区进程锁会阻止另一个服务使用相同数据目录；不要增加 Uvicorn worker 或 Compose 副本，当前队列与浏览器会话协调只在一个进程内完成。

2026-11-04：运行诊断、容量保护、完整备份恢复及 CPU 水印/版本功能已完成本地实现与受控验收，见 [OPERATIONS.md](OPERATIONS.md)、[PROCESSING.md](PROCESSING.md)。本轮 Docker Compose 配置解析通过，未构建镜像、未启动服务器容器；用户将后续提供服务器实测环境，不能据此声称生产部署已经验收。

## 本机直接运行

安装 Python 3.12+、uv、FFmpeg/FFprobe 后，在仓库目录执行：

```sh
uv sync --frozen
uv run playwright install chromium
uv run python -m backend
```

`npm run dev` 是相同真实后端的快捷入口。Linux 首次安装浏览器及系统依赖可使用 `uv run playwright install --with-deps chromium`。默认地址是 [http://127.0.0.1:4173](http://127.0.0.1:4173)，仅监听回环接口，无口令时不要求管理员登录。启动会加载 `.env`，现有进程环境变量优先。

| 环境变量 | 原生默认值 | 用途 |
| --- | --- | --- |
| `FRAMEKEEP_HOST` | `127.0.0.1` | 监听地址；非回环地址必须配置至少 24 字符口令 |
| `FRAMEKEEP_PORT` | `4173` | 服务端口 |
| `FRAMEKEEP_DATA_DIR` | `.framekeep` | SQLite、浏览器会话和媒体文件目录 |
| `FRAMEKEEP_ADMIN_TOKEN` | 空 | 设置后全站与 API 使用 HTTP Basic 认证 |
| `FRAMEKEEP_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],testserver` | 逗号分隔的请求 Host 白名单；域名不含协议、端口或路径 |
| `FRAMEKEEP_MAX_DOWNLOAD_BYTES` | `2147483648` | 单个任务的传输/文件上限，默认 2 GiB |
| `FRAMEKEEP_MIN_FREE_BYTES` | `536870912` | 磁盘保留空间，默认 512 MiB；下载、处理、备份写入前及过程中检查 |
| `FRAMEKEEP_FONT_PATH` | 自动寻找本机字体 | 原生环境可指定覆盖所需文字的字体文件；Compose 指向 Noto CJK |
| `FRAMEKEEP_TRUST_FAKE_DNS` | `0` | 只在明确使用并信任自己的 Fake-IP/TUN 代理时设为 `1` |

启用 HTTP Basic 时，用户名惯例填 `admin`，密码填管理员口令；服务目前只校验密码，不把用户名作为独立身份。`.env.example` 面向服务器部署，本机默认运行不必创建它。

## Docker Compose

镜像使用 Python 3.12、固定版本 uv，按 `uv.lock` 安装生产依赖，然后安装对应 Chromium、系统浏览器依赖、FFmpeg 和 `fonts-noto-cjk`。Compose 将字体路径设为 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`；缺字时处理会明确失败，不静默生成方框。安装方式参考 [uv Docker 文档](https://docs.astral.sh/uv/guides/integration/docker/) 与 [Playwright 浏览器文档](https://playwright.dev/python/docs/browsers)。

先建立配置，生成自己的随机口令填入 `.env`，不要保留空值或使用示例字符串：

```sh
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
chmod 600 .env
```

将输出的口令写入 `.env` 的 `FRAMEKEEP_ADMIN_TOKEN=` 后。Compose 会拒绝空口令；服务启动入口还会拒绝不足 24 字符的口令。`.env` 已被 Git 和 Docker 构建上下文忽略。如果已存在本机 `.env`，先保留原配置，再按 `.env.example` 补充服务器参数。

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 framekeep
```

首次构建会下载 Chromium 与系统依赖，耗时和镜像体积均大于纯 API 服务。容器内监听 `0.0.0.0:4173`，宿主机只发布 **`127.0.0.1:4173`**。如宿主机端口已占用，在 `.env` 修改 `FRAMEKEEP_PUBLISH_PORT`，内部端口仍为 4173。

容器使用 UID/GID 10001 的 `framekeep` 用户。镜像预先创建 `/var/lib/framekeep`，设置为该用户所有且权限为 0700；新建命名卷继承该目录内容与所有权。Compose 使用 `framekeep_data` 命名卷持久化数据，并提供 512 MiB 共享内存和 init 进程供 Chromium 使用，停止宽限期为 60 秒。如果改成已有宿主机目录挂载，先确保该目录归 UID/GID 10001 所有并可写。

在服务器本机验证认证和健康状态，`curl` 会交互提示输入口令：

```sh
curl -fsS --user admin http://127.0.0.1:4173/api/health
curl -fsS --user admin http://127.0.0.1:4173/api/ready
```

存活检查 `/api/health` 预期返回 `{"status":"ok","mode":"real"}`；就绪检查 `/api/ready` 必须返回 200 且 `ready: true`。Compose 使用就绪检查，从容器环境读取口令，不把口令写入命令输出。工作循环退出、心跳超时、依赖未发现、数据库读取失败或低磁盘会返回 503；详情在面板运行维护区域或 `/api/operations`。就绪通过后，还需实际打开浏览器、扫描、下载及处理样例验证完整环境。

## 从个人电脑访问服务器

推荐保留回环端口发布，通过 SSH 隧道访问：

```sh
ssh -N -L 4173:127.0.0.1:4173 your-user@your-server
```

保持隧道终端运行，在个人电脑打开 [http://127.0.0.1:4173](http://127.0.0.1:4173)，输入管理员口令。若个人电脑已占用 4173，把 `-L` 第一个端口换成 4174，然后访问本机 4174。

需要域名时，把对应主机名加入 `FRAMEKEEP_ALLOWED_HOSTS`，在受控反向代理后提供 HTTPS。代理应保留 `Host` 和认证头、正确传递协议，避免 HTTPS 页面请求被当成跨站 HTTP 操作。当前代码针对单一同源入口；自定义代理配置需另行验证登录、写操作与媒体 Range 请求，不把 Compose 解析当成代理验收。

不要将无 TLS 的 Basic 认证端口直接暴露到公网。该服务拥有用户保存的站点会话，适合个人受控访问，不适合开放给不可信用户提交任意目标。

## 数据、停止和恢复

卷内包括 SQLite 数据库及 WAL/SHM 文件、加密浏览器状态与本机加密密钥、`assets/<asset-id>/original.<ext>` 原件、`assets/<asset-id>/versions/<version-id>/` 处理版本、缩略图和未完成下载的临时文件。完整备份需一并保留；密钥与会话在同一受限目录中，磁盘或完整备份泄露时不构成独立密钥隔离。管理员口令与站点登录状态用途不同。

优先使用面板创建一致快照：暂停活动任务后进入维护状态，保存打开的会话，生成 SQLite 快照并复制文件，核对哈希、数据库引用与会话密钥。普通备份列表只展示元数据，显式校验才会重新读取全部文件。完整 CLI、空目录原子恢复和故障排查见 [运行维护说明](OPERATIONS.md)。以下是容器安装后的离线备份命令模板，服务器尚未实测：

```sh
docker compose stop
docker compose run --rm --no-deps framekeep python -m backend.backup create \
  --data-dir /var/lib/framekeep \
  --output /var/lib/framekeep/backups/before-upgrade
docker compose run --rm --no-deps framekeep python -m backend.backup verify \
  --backup /var/lib/framekeep/backups/before-upgrade
docker compose start
```

每次使用新的快照目录名；备份位于同一卷时还需复制到另一受控位置。CLI 不允许绕过服务持有的工作区锁。恢复始终到新空目录，完整验证后才发布，再用隔离实例检查并切换挂载目录。下面保留原来的停机完整目录复制方式；这种文件副本没有上述快照清单和自动完整性校验：

```sh
docker compose stop
docker compose cp framekeep:/var/lib/framekeep ./framekeep-backup
docker compose start
```

使用尚不存在的备份目标目录，并把备份当成包含登录状态的私有数据。以上在停止服务后复制完整目录，避免只复制活动数据库主文件而遗漏 WAL。恢复时也先停止服务，确保恢复后的卷内文件仍由 UID/GID 10001 所有。需要保留数据时不要执行带 `-v` 的 `docker compose down`。

网页关闭不会结束后端工作。服务停止或重启后，运行中的下载和处理为暂停状态，未开始的 queued 任务继续排队。下载继续时校验本地检查点：HTTP 在强 ETag（或符合强验证条件的 Last-Modified）及 Range 可用时从断点传输；HLS 重新读取清单并验证已缓存片段。源文件改变、检查点损坏、服务器忽略 Range 或无可靠验证器时安全重下。CPU 处理继续时从原件重新处理，不提供编码帧断点续算。显式取消只清理本次未完成输出，原件与已登记版本保留。扫描中断会失败，需重新发起。升级前备份，再运行 `docker compose up -d --build`；当前没有多实例滚动升级能力。

下载重试针对连接失败、408 / 429 / 5xx，每个资源最多尝试 4 次，退避等待及 Retry-After 上限 5 秒；401 / 403 不自动反复尝试。任务失败后先查看原因，再手动重试；可恢复网络失败保留检查点。默认单任务上限 2 GiB，可用 `FRAMEKEEP_MAX_DOWNLOAD_BYTES` 调整。HLS 缓存、解密和合并需要额外磁盘空间，按最终文件的数倍预留，不只预留原件大小。

检查点 `.download.json` 和 `.hls/` 属于同一私有数据卷，升级与迁移时不要遗漏。不要手工编辑检查点；损坏数据会触发安全重下。下载并发在面板设置中配置为 1–4，CPU 处理为单任务且 FFmpeg 线程有界。共享磁盘阈值在开始和写入过程中复查；释放空间后再继续或重试。完整复跑与已知范围见 [下载验收](DOWNLOAD_ACCEPTANCE.md) 和 [CPU 处理](PROCESSING.md)。

## 授权与媒体范围

用户必须亲自在站点窗口完成登录、验证码、同意声明、年龄确认和付费操作。程序不代为付款，也不会根据保存会话推断会员权限。四个预设站点 Lustflix、Candy AI、春楼、Moolight 尚未逐一完成真实登录、权限、下载与全站覆盖验收；扫描有深度与页数上限。

已通过本地真实样本验证的是 HTTP 图片链路和 HLS 点播：包括相对/签名 URL、普通 AES-128、独立音轨、有效 MP4 封装与音视频轨道。HLS 清单、片段、密钥和重定向逐一进入 URL 策略；外部 yt-dlp 进程只读取重写后的本地缓存，不接收站点 Cookie 或远程媒体链接。AES-128 仅处理会话能够获取的普通授权密钥，不绕过 DRM。

已接入 Generic/HTML5 网页提取、抖音/TikTok/Instagram 单作品解析与静态单 Period DASH，范围见 [开源接入与验收](OPEN_SOURCE_DOWNLOADS.md)。当前明确不支持：任意站点专用 extractor、动态/多 Period/字节范围 DASH、直播清单、HLS 字节范围、变量、会话密钥、低延迟片段等扩展。CPU 加水印与人工选择固定区域的去除已接入；移动水印跟踪、AI 无痕修复和超分未启用。后台定时扫描及自动下载见[自动化使用说明](AUTOMATION.md)。站点更换播放器、链接过期或服务端校验可能导致失败，应以实际任务错误和媒体验证结果为准。

## 出站网络与 Fake-IP

浏览器和 HTTP 下载使用基础 URL 策略与固定 IP 出口代理，检查目标地址和重定向，不向外部下载进程开放任意远程 URL。服务仍应运行在个人专用环境，通过服务器网络出口策略隔离内部管理接口与云元数据服务；这不是公开多租户网络沙箱。

出口代理支持 HTTP、HTTPS CONNECT 和 WebSocket Upgrade。普通 HTTP 请求仅支持 Content-Length 分帧，请求体上限 64 MiB；Transfer-Encoding 和 Expect 暂不支持。HTTPS 隧道内的请求不受这一 HTTP 分帧限制。连接使用空闲超时，不用短时间的连接总时限截断持续下载。

部分本机代理把公网域名映射到 `198.18.0.0/15` Fake-IP 地址。默认服务器配置不会信任这类地址。只有管理员明确知道当前网络由自己的可信 Fake-IP/TUN 代理接管，才可设置 `FRAMEKEEP_TRUST_FAKE_DNS=1`；此时代理负责映射实际目标，安全边界包含该代理，不能据此声称拥有独立的强网络隔离。不要为绕过未知地址错误在服务器盲目开启该开关。

## 验证与排障

```sh
uv run python -m pytest tests_backend -q
uv run ruff check backend tests_backend
npm test
npm run check
docker compose config --quiet
uv run python scripts/operations_acceptance.py --seconds 180
uv run python scripts/processing_acceptance.py
```

- `401`：没有提供正确管理员口令。
- `Invalid host header`：将实际入口主机名加入 `FRAMEKEEP_ALLOWED_HOSTS`。
- Chromium 启动失败：原生环境重新安装浏览器及系统依赖；容器环境查看构建日志并重新构建。
- `ffmpeg`/`ffprobe` 缺失：确认二者位于服务进程的 `PATH` 中；镜像已声明安装。
- 登录页、权限或短效链接错误：回到站点窗口检查实际权限，保存会话后重新扫描。
- 数据卷写入失败：检查挂载路径与 UID/GID 10001 的权限。
- `/api/ready` 为 503：查看运行维护的依赖、工作循环心跳、数据库与磁盘项；维护备份期间未就绪是预期状态。
- 工作区正在使用：停止实际持锁进程，或给第二个实例指定独立数据目录，不删除锁文件来绕过占用。
- 处理缺字或字体不可用：确认字体文件实际存在并覆盖输入文字，原生环境可设置 `FRAMEKEEP_FONT_PATH`；容器安装与实际渲染须现场验证。

本地测试证据和历史原型验收见 [QA 记录](QA.md)。真实目标站点与服务器容器的端到端运行需要分别验收。

2026-11-03 容器构建尝试：Docker daemon可用，但完整浏览器及精简headless-shell两次构建均遇到Docker存储ENOSPC，后者在导出层时失败。配置已保留，未通过容器运行验收；需在有足够存储的环境重新构建。

## 独立生产入口：Caddy HTTPS（2026-11-13）

新增 `deploy/compose.production.yaml` 是**独立配置**，不要与仓库根目录的 `compose.yaml` 合并使用。它启动单个 Framekeep 后端和 Caddy：只有 Caddy 发布 80/443，后端不发布任何宿主机端口。前端与 API 均使用入口根路径，不部署到 `/framekeep/` 子路径。后端数据、Caddy 证书和配置分别使用命名卷；保留固定 Compose 项目名 `framekeep-production`，升级或切换入口时不要执行 `down -v`。

域名必须解析到服务器；开放 TCP 80/443，UDP 443 仅用于 HTTP/3。若存在不正确的 AAAA 记录，先修正，避免证书验证和用户访问走错地址。Caddy 会自动申请和续期证书，配置依据 [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https) 与 [反向代理文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。配置默认固定 `caddy:2.11.9-alpine`，升级需先验证，也可通过 `FRAMEKEEP_CADDY_IMAGE` 固定到经验证的镜像摘要。

### 生成配置并检查网络

以下命令从仓库根目录运行。环境文件已有内容时不要覆盖；`deploy/.gitignore` 忽略实际口令文件，只保留空值示例：

```sh
cp -n deploy/.env.production.example deploy/.env.production
chmod 600 deploy/.env.production
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将随机输出写入 `FRAMEKEEP_ADMIN_TOKEN`，将实际域名写入 `FRAMEKEEP_DOMAIN`，不填写协议、端口或路径。不要把解析后的 `docker compose config` 输出或环境文件发送到公开日志；下面的校验脚本不会显示口令。

专用网络默认 `172.30.246.0/24`，Caddy 固定 `172.30.246.2`，后端固定 `172.30.246.3`。部署前在服务器检查现有路由和 Docker 网段，保证该网段不覆盖宿主机、VPN、云内网或已有容器网络：

```sh
ip -4 route
docker network ls -q | xargs -r docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}'
```

如有重叠，**一起修改** `FRAMEKEEP_NETWORK_SUBNET`、`FRAMEKEEP_PROXY_IP`、`FRAMEKEEP_BACKEND_IP`。两个地址须处于新子网，互不相同，且不使用网络地址、广播地址或默认网关 `.1`。这些固定地址用于避免动态分配抢占代理地址；服务间仍通过 `framekeep:4173` 名称连接。Compose 对静态地址的要求见 [Docker 网络说明](https://docs.docker.com/reference/compose-file/services/#ipv4_address-ipv6_address)。

后端的 `FORWARDED_ALLOW_IPS` 被明确设置为 **Caddy 单个地址**，没有信任整个子网或 `*`。这使 Uvicorn 能识别代理传来的 HTTPS 协议，避免页面能打开而同源 POST 被拒绝。Caddy 保留 `Host`、`Authorization`、`Range`，覆盖协议头，并及时转发 ZIP 和媒体数据。

### 校验、启动和验收

```sh
python3 deploy/validate-production.py --env-file deploy/.env.production
docker compose --env-file deploy/.env.production -f deploy/compose.production.yaml config --quiet
docker compose --env-file deploy/.env.production -f deploy/compose.production.yaml run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file deploy/.env.production -f deploy/compose.production.yaml up -d --build
docker compose --env-file deploy/.env.production -f deploy/compose.production.yaml ps
docker compose --env-file deploy/.env.production -f deploy/compose.production.yaml logs --tail=80 framekeep caddy
```

前两条只解析配置，**不依赖 Docker daemon**。脚本额外检查入口格式、口令长度、固定网络地址、单一代理信任、后端无发布端口及数据持久卷；它不能证明宿主机网段无冲突，也不会验证 DNS、端口、证书、镜像构建或容器运行。

上线验收必须在真实 HTTPS 地址完成：未认证返回 401；输入管理员口令后 `/api/health`、`/api/ready` 返回 200；面板同源写操作成功而错误 Origin 被拒绝；使用受控样例完成扫描、下载、视频 Range 播放及 ZIP 导出；重启后数据保留。确认 Caddy 日志签发成功并检查证书有效期，不使用忽略证书错误的选项作为验收。

容器数据卷 UID/GID 为 10001。机器内存约 2 GiB 时先将面板下载并发设为 1、缩小首次扫描范围；构建层、Chromium、原件、HLS 分片和处理临时文件均占空间，不能把剩余磁盘全部当作成品容量。当前配置未接入 R2。

### 只有公网 IPv4 时

可以先使用公信 IP HTTPS。Let's Encrypt 自 2026-01-15 起普遍提供 IPv4/IPv6 证书，IP 证书使用 `shortlived` profile，有效期 160 小时，因此自动续期和持续开放验证端口尤为重要。参见 [Let's Encrypt 官方公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) 和 [Caddy ACME profile 配置](https://caddyserver.com/docs/caddyfile/directives/tls)。本项目的临时 IP 配置目前限定公网 IPv4。

```sh
cp -n deploy/.env.ip.example deploy/.env.ip
chmod 600 deploy/.env.ip
```

在此文件中，`FRAMEKEEP_DOMAIN` **改填服务器公网 IPv4**（沿用变量名），其余参数与域名模式相同；口令仍需独立生成。`compose.ip.yaml` 只替换 Caddy 配置文件，显式选择 Let's Encrypt 公信签发及 `shortlived`，并为 IP 地址访问通常不带 SNI 的浏览器设置默认服务器名。没有使用 `tls internal`，也不要求用户忽略证书警告。

```sh
python3 deploy/validate-production.py --env-file deploy/.env.ip --ip
docker compose --env-file deploy/.env.ip -f deploy/compose.production.yaml -f deploy/compose.ip.yaml config --quiet
docker compose --env-file deploy/.env.ip -f deploy/compose.production.yaml -f deploy/compose.ip.yaml run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file deploy/.env.ip -f deploy/compose.production.yaml -f deploy/compose.ip.yaml up -d --build
```

证书申请需要服务器公网 IP 的 80/443 可达；签发或续期失败时应修复验证链路，不能退回公网明文 HTTP Basic。可以先采用本文已有的根目录 Compose 回环端口 + SSH 隧道方案验收，此时公网不开放 4173。域名准备好后，保留项目名和命名卷，改用 `.env.production`，运行不带 `compose.ip.yaml` 的域名启动命令即可切换入口。

以上部署不会自动迁移本机账号和文件。本机 Chrome/Google 授权及春楼书签导入仍有回环和桌面限制，不能通过远程面板直接调用；通用站点截图授权可以远程操作，但 Google/Cloudflare 是否接受需逐站验收。迁移已保存会话时应使用完整备份恢复，同时保留数据库、`sessions/.key` 和加密会话文件。不要通过放开本机授权接口来临时绕过限制。

## 公开下载工具：免注册、独立访客工作区

使用 `FRAMEKEEP_PUBLIC_MODE=1` 时，入口为匿名访客应用，不是共享的管理员面板。每个浏览器用 HttpOnly Cookie 选择自己的服务器工作区，素材、任务、来源会话和导出文件均在该工作区中。不要把已有私有 `.framekeep` 目录挂到这个入口；公开部署使用新的命名卷。清除浏览器数据或换浏览器会打开新空间，没有账号找回功能。

公开入口只提供拾取、剧集、下载、文件和个人来源授权；服务器维护、自动化、CPU 后处理及自动花费金币的操作由后端禁止。下载使用服务器网络和临时磁盘，用户点击“保存到本机”或导出 ZIP 时，成品才传到用户电脑。此版本尚未接入 R2。

对于 1 核 / 2 GB / 20 GB 的服务器，示例配置保留 1 GB 空闲磁盘、单文件最多 1 GB、每个访客空间配置 2 GB 软额度、所有访客空间共用 6 GB 软额度；最多两个工作区常驻，扫描和下载各有一个全局执行名额。达到额度时停止继续处理或接收新任务，并提示等待或空间不足。额度由下载期间和请求前后检查，进行中的一次写入可能短暂超出，不能视为文件系统硬配额。工作区默认在 24 小时未使用、且没有活动任务后删除；保留时长可配置，不应把临时空间当作长期网盘。

```bash
cp deploy/.env.public.example deploy/.env.public
chmod 600 deploy/.env.public
# 填写 FRAMEKEEP_DOMAIN 为服务器公网 IPv4，其余容量根据实际磁盘调整。
python3 deploy/validate-production.py --env-file deploy/.env.public --ip --public
docker compose --env-file deploy/.env.public -f deploy/compose.production.yaml -f deploy/compose.ip.yaml -f deploy/compose.public.yaml up -d --build
```

绑定域名时先设置 DNS A 记录，再把 `FRAMEKEEP_DOMAIN` 改为该域名，去掉 `compose.ip.yaml`，保留 `compose.public.yaml` 后重新 `up -d`。Caddy 自动申请域名证书。域名变更会改变 Cookie 来源：旧 IP 浏览器空间不会自动转到新域名，用户应先把已完成文件保存到自己的电脑。

部署验收必须包括两个独立 Cookie 会话：一方创建的素材/来源不得出现在另一方状态中；另一方直接访问已知素材 ID、缩略图、单集及 ZIP 导出也不得获得文件。还应验证同一 Cookie 的刷新和重启恢复、过期清理、取消排队以及 HTTPS/Range 下载。只通过配置解析不能视作上线完成。
