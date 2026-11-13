# 生产部署配置校验记录

日期：2026-11-13。本文件以下内容是部署前的静态校验记录；具体服务器地址、环境配置和私人运行记录不随源码发布。部署步骤见 [部署说明](../docs/DEPLOYMENT.md)，代表样本的验证范围见 [社交视频下载](../docs/SOCIAL_DOWNLOADS.md)。

## 已执行

- Docker Compose CLI `5.1.3`，对域名版及 IPv4 overlay 分别执行 `docker compose ... config --quiet`，退出码均为 0。
- 校验时把 `DOCKER_HOST` 指向不存在的 Unix socket，确认解析不依赖 Docker daemon。
- `validate-production.py` 的 10 组输入通过预期判定：域名、公网 IPv4、替换网段可用；缺域名、缺口令、短口令、代理超出网段、地址重复、域名模式误填 IP、IP 模式误填回环地址被拒绝。
- 检查合并后的 IP 配置：只有一个 `/etc/caddy/Caddyfile` 挂载，指向 `Caddyfile.ip`；构建上下文为仓库根目录；后端不发布端口；`FORWARDED_ALLOW_IPS` 只等于 Caddy 固定 IP。
- `.venv/bin/ruff check deploy/validate-production.py` 与 `ruff format --check` 通过。
- `docker manifest inspect caddy:2.11.9-alpine` 成功，镜像索引包含 `linux/amd64`；本步骤仅查询公开镜像元数据，没有构建或启动容器。
- 只使用示例域名、测试 IP 和明确标记为测试的口令，临时输入在结束后删除。没有输出、读取或复制用户真实密钥。

## 未执行

未运行 Caddy 二进制配置解析、镜像构建、容器启动、宿主机网段冲突检查、DNS/端口验证、ACME 签发/续期、HTTPS 面板、媒体 Range、ZIP 或站点登录验收。上述静态结果不能视为部署成功。

上服务器后先按 `docs/DEPLOYMENT.md` 新节运行 `caddy validate`，再进行实际签发与应用验收；IP 证书必须检查公信链和有效期，不能使用跳过证书校验代替成功。

## 配置依据

- [Caddy v2.11.9 官方发行](https://github.com/caddyserver/caddy/releases/tag/v2.11.9)
- [Caddy 的 ACME profile 语法](https://caddyserver.com/docs/caddyfile/directives/tls)
- [Caddy 的 default_sni 语法](https://caddyserver.com/docs/caddyfile/options#default_sni)
- [Let's Encrypt 公信 IP 证书公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)
- [Docker Compose 静态 IP 与 IPAM](https://docs.docker.com/reference/compose-file/services/#ipv4_address-ipv6_address)
