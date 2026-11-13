"""Validate Compose interpolation and deployment invariants without a Docker daemon."""

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path


def validate(config, *, ip_mode=False, public_mode=False):
    services = config["services"]
    backend, proxy = services["framekeep"], services["caddy"]
    environment = backend["environment"]
    domain = proxy["environment"]["FRAMEKEEP_DOMAIN"]
    if ip_mode:
        if not ipaddress.IPv4Address(domain).is_global:
            raise ValueError("IP 模式需要服务器的公网 IPv4")
    else:
        labels = domain.split(".")
        if (
            len(domain) > 253
            or len(labels) < 2
            or any(
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in labels
            )
        ):
            raise ValueError("FRAMEKEEP_DOMAIN 必须是 DNS 域名，不含协议、端口或路径")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            raise ValueError("域名模式不能只填服务器 IP；公网 IPv4 请显式选择 --ip")
    if public_mode:
        if environment.get("FRAMEKEEP_PUBLIC_MODE") != "1":
            raise ValueError("公开模式必须启用访客工作区隔离")
        if environment.get("FRAMEKEEP_PUBLIC_SECURE_COOKIE") != "1":
            raise ValueError("公开部署必须使用 HTTPS Cookie")
        if environment.get("FRAMEKEEP_ADMIN_TOKEN"):
            raise ValueError("公开模式不能使用共享管理员登录")
    elif len(environment["FRAMEKEEP_ADMIN_TOKEN"]) < 24:
        raise ValueError("FRAMEKEEP_ADMIN_TOKEN 至少需要 24 字符；请生成随机口令")
    if backend.get("ports") or backend.get("network_mode") == "host":
        raise ValueError("后端不可直接发布宿主机端口")
    subnet = ipaddress.IPv4Network(config["networks"]["app"]["ipam"]["config"][0]["subnet"])
    address = ipaddress.IPv4Address(proxy["networks"]["app"]["ipv4_address"])
    backend_address = ipaddress.IPv4Address(backend["networks"]["app"]["ipv4_address"])
    reserved = {subnet.network_address, subnet.broadcast_address, subnet.network_address + 1}
    if any(ip not in subnet or ip in reserved for ip in (address, backend_address)):
        raise ValueError("代理和后端地址必须位于专用子网内，且不能是网络、广播或默认网关地址")
    if address == backend_address:
        raise ValueError("代理和后端必须使用不同地址")
    if environment["FORWARDED_ALLOW_IPS"] != str(address):
        raise ValueError("FORWARDED_ALLOW_IPS 必须且只能信任 Caddy 的固定地址")
    if domain not in environment["FRAMEKEEP_ALLOWED_HOSTS"].split(","):
        raise ValueError("入口域名必须包含在后端 Host 白名单")
    if not any(
        volume["type"] == "volume" and volume["target"] == "/var/lib/framekeep"
        for volume in backend["volumes"]
    ):
        raise ValueError("后端数据目录必须使用持久卷")
    if {port["target"] for port in proxy["ports"]} != {80, 443}:
        raise ValueError("Caddy 只应发布 HTTP/HTTPS 入口")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--ip", action="store_true", help="校验公信 IPv4 HTTPS 配置")
    parser.add_argument("--public", action="store_true", help="校验免注册的独立访客工作区")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("指定的环境配置文件不存在")
    compose = Path(__file__).with_name("compose.production.yaml")
    command = [
        "docker",
        "compose",
        "--env-file",
        str(args.env_file.resolve()),
        "-f",
        str(compose),
    ]
    if args.ip:
        command.extend(["-f", str(compose.with_name("compose.ip.yaml"))])
    if args.public:
        command.extend(["-f", str(compose.with_name("compose.public.yaml"))])
    command.extend(["config", "--format", "json"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        print("校验失败：需要 Docker CLI 和 Compose 插件，无需启动 daemon。", file=sys.stderr)
        return 1
    if result.returncode:
        # Do not print resolved configuration or subprocess output containing environment values.
        print("Compose 配置解析失败；检查必填域名、口令与配置语法。", file=sys.stderr)
        return 1
    try:
        validate(json.loads(result.stdout), ip_mode=args.ip, public_mode=args.public)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        print(f"部署配置校验失败：{error}", file=sys.stderr)
        return 1
    print("配置通过：双服务、入口地址、访问模式、持久卷和单一代理信任检查通过。")
    print("未访问 Docker daemon；未验证网络冲突、DNS、TLS、镜像构建或容器运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
