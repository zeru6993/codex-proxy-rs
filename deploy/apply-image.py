#!/usr/bin/env python3
"""校验 dev 构建清单，仅替换应用镜像并核对运行进程的源码身份"""

import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SERVICE = "codex-proxy-rs"
DEFAULT_REPOSITORY = "zeru6993/codex-proxy-rs"


def run(arguments, timeout=60, input=None):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, input=input)
    if result.returncode:
        # Compose 的诊断可能展开凭据，不把原始输出写入终端或发布记录
        raise RuntimeError("命令失败，请在服务器检查：" + " ".join(arguments[:3]))
    return result.stdout


def load_manifest(path, repository):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("repository") != repository:
        raise ValueError("部署清单的格式或发布仓库不匹配")
    sha = data.get("git_sha", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("部署清单必须包含完整提交 SHA")
    image = data.get("image")
    if not isinstance(image, str) or not re.fullmatch(re.escape("ghcr.io/" + repository) + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError("只接受自有仓库的固定镜像摘要")
    if data.get("platform") != "linux/amd64" or data.get("build_type") != "source":
        raise ValueError("部署清单的平台或构建类型不匹配")
    version = data.get("version")
    if not isinstance(version, str) or not version.endswith("+git." + sha[:12]):
        raise ValueError("构建版本与源码提交不匹配")
    return data


def render_compose(original, manifest):
    text = original.decode("utf-8")
    pattern = r"(?ms)(^  codex-proxy-rs:\r?\n)(.*?)(?=^(?:\S|  \S)|\Z)"
    blocks = list(re.finditer(pattern, text))
    if len(blocks) != 1:
        raise ValueError("无法唯一定位应用服务，请检查 Compose 格式")
    block = blocks[0]
    body = block.group(2)
    for key, value, indent in [
        ("image", manifest["image"], "    "),
        ("CPR_UPDATE_REPOSITORY", manifest["repository"], "      "),
        ("CPR_ENABLE_SELF_RESTART", "false", "      "),
    ]:
        body, count = re.subn(r"(?m)^" + indent + key + r":[^\r\n]*", indent + key + ": " + json.dumps(value), body)
        if count != 1:
            raise ValueError("无法唯一定位应用配置：" + key)
    return (text[:block.start(2)] + body + text[block.end(2):]).encode("utf-8")


def compose_command(path):
    return ["docker", "compose", "--project-directory", str(path.parent), "-f", str(path)]


def verify_config_change(before, after, manifest):
    if after["services"][SERVICE]["image"] != manifest["image"]:
        raise ValueError("Compose 未使用目标镜像摘要")
    for document in (before, after):
        app = document["services"][SERVICE]
        app.pop("image")
        for key in ("CPR_UPDATE_REPOSITORY", "CPR_ENABLE_SELF_RESTART"):
            app.get("environment", {}).pop(key, None)
    if before != after:
        raise ValueError("检测到应用镜像与更新入口之外的部署变化")


def verify_image(manifest):
    image = json.loads(run(["docker", "image", "inspect", manifest["image"]]))[0]
    labels = image["Config"].get("Labels") or {}
    expected = {
        "org.opencontainers.image.revision": manifest["git_sha"],
        "org.opencontainers.image.version": manifest["version"],
        "org.opencontainers.image.source": "https://github.com/" + manifest["repository"],
        "io.codex-proxy.build-type": "source",
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValueError("拉取的镜像身份与验收清单不匹配")
    if (image["Os"], image["Architecture"]) != ("linux", "amd64"):
        raise ValueError("镜像平台不匹配")


def read_admin_key(compose):
    key = run(compose_command(compose) + ["exec", "-T", "postgres", "sh", "-c", 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT admin_api_key FROM runtime_settings WHERE id = 1"']).strip()
    if not key:
        raise ValueError("运行身份验收需要管理员 API Key，请先在管理端配置后再部署")
    return key


@contextlib.contextmanager
def deployment_lock(compose, apply):
    if not apply:
        yield None
        return
    import fcntl

    root = compose.parent.parent / ".runtime" / "deployments"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root


def deploy(manifest, compose, apply=False, wait_timeout=600):
    with deployment_lock(compose, apply) as history:
        original = compose.read_bytes()
        rendered = render_compose(original, manifest)
        descriptor, temporary = tempfile.mkstemp(prefix=".compose-image-", suffix=".yaml", dir=compose.parent)
        candidate = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
            before = json.loads(run(compose_command(compose) + ["config", "--format", "json"]))
            after = json.loads(run(compose_command(candidate) + ["config", "--format", "json"]))
            verify_config_change(before, after, manifest)
            result = {"image": manifest["image"], "git_sha": manifest["git_sha"], "version": manifest["version"], "applied": False}
            if not apply:
                return result
            container = run(compose_command(compose) + ["ps", "-q", SERVICE]).strip()
            if not container:
                raise ValueError("应用尚未运行，请先按部署文档完成初始化")
            current = json.loads(run(["docker", "inspect", container]))[0]
            # 在拉取镜像和修改部署文件前核对验收凭据，避免健康实例因缺少凭据而中途失败
            read_admin_key(compose)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            record = history / (stamp + "-" + manifest["git_sha"][:12])
            record.mkdir(mode=0o700)
            (record / "compose.before.yaml").write_bytes(original)
            (record / "deployment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result.update(previous_image=current["Image"], previous_image_reference=current["Config"]["Image"], record=str(record))
            try:
                run(["docker", "pull", manifest["image"]], timeout=600)
                verify_image(manifest)
                if compose.read_bytes() != original:
                    raise ValueError("部署文件已被其他操作修改，请重新检查")
                metadata = compose.stat()
                os.chmod(candidate, metadata.st_mode & 0o777)
                os.chown(candidate, metadata.st_uid, metadata.st_gid)
                os.replace(candidate, compose)
                result["compose_updated"] = True
                # 依赖服务和数据目录保持现状，仅重建应用，避免顺带升级数据库或 Redis
                run(compose_command(compose) + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", str(wait_timeout), SERVICE], timeout=wait_timeout + 30)
                running = run(compose_command(compose) + ["ps", "-q", SERVICE]).strip()
                admin_key = read_admin_key(compose)
                curl = ["docker", "exec", "-i", running, "curl", "--config", "-", "--fail", "--silent", "--show-error", "--max-time", "15"]
                header = "header = " + json.dumps("x-api-key: " + admin_key) + "\n"
                identity = json.loads(run(curl + ["http://127.0.0.1:8080/api/admin/system/version"], input=header))["data"]
                if (identity["gitSha"], identity["version"]) != (manifest["git_sha"], manifest["version"]):
                    raise ValueError("运行进程的版本或提交与目标不匹配")
                policy = json.loads(run(curl + ["http://127.0.0.1:8080/api/admin/system/update/detail"], input=header))["data"]
                if policy["buildType"] != "source" or policy["updateSupported"]:
                    raise ValueError("运行进程允许在线替换自有镜像中的程序，请检查系统更新配置")
                run(["docker", "exec", running, "curl", "--fail", "--silent", "--show-error", "--max-time", "15", "http://127.0.0.1:8080/readyz"])
                result["applied"] = True
                return result
            except Exception:
                # 新进程可能已执行迁移，失败时保留现场，不能自动换回旧二进制
                result["failed"] = True
                raise
            finally:
                (record / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        finally:
            candidate.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--compose", type=Path, default=Path("deploy/compose.yaml"))
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--apply", action="store_true", help="执行升级；省略时只检查部署计划")
    parser.add_argument("--wait-timeout", type=int, default=600)
    args = parser.parse_args()
    if args.wait_timeout <= 0:
        parser.error("--wait-timeout 必须大于零")
    manifest = load_manifest(args.manifest, args.repository)
    result = deploy(manifest, args.compose.resolve(), args.apply, args.wait_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print("部署未完成：" + str(error), file=sys.stderr)
        sys.exit(1)
