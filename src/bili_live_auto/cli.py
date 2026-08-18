from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from .api import BilibiliLiveAPI
from .app import Application
from .config import ConfigError, load_config


def configure_logging(work_dir: Path, level: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers.append(logging.FileHandler(work_dir / "bili-live-auto.log", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        handlers=handlers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bilibili 直播自动录制与投稿工具")
    parser.add_argument("-c", "--config", default="config.toml", help="TOML 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="启动监控")
    run.add_argument("--once", action="store_true", help="只检查一次；若已开播会录至下播")
    sub.add_parser("check", help="仅查询直播间状态")
    sub.add_parser("doctor", help="检查配置和外部程序")
    sub.add_parser("login", help="调用 biliup 扫码登录并保存凭据")
    return parser


def _doctor(config) -> int:
    ok = True
    print(f"配置文件：{config.source_path}")
    print(f"工作目录：{config.app.work_dir}")
    print(f"录制后端：{config.recording.backend}")
    if config.recording.backend in {"bililiverecorder", "livehime"}:
        watch_exists = config.recording.watch_dir.is_dir()
        print(f"录播姬工作目录：{config.recording.watch_dir}（{'存在' if watch_exists else '不存在'}）")
        ok = ok and watch_exists
    else:
        found = shutil.which(config.recording.executable)
        print(f"yt-dlp：{found or '未找到'}")
        ok = ok and bool(found)
        ffmpeg = shutil.which("ffmpeg")
        print(f"ffmpeg：{ffmpeg or '未找到'}")
        ok = ok and bool(ffmpeg)
    if config.upload.enabled:
        found = shutil.which(config.upload.executable)
        print(f"biliup：{found or '未找到'}")
        ok = ok and bool(found)
    print(f"投稿：{'已启用' if config.upload.enabled else '未启用（安全默认值）'}")
    if config.upload.enabled:
        exists = config.upload.cookie_file.is_file()
        print(f"登录凭据：{config.upload.cookie_file}（{'存在' if exists else '不存在'}）")
        ok = ok and exists
    rooms = [room for room in config.rooms if room.enabled]
    print(f"启用直播间：{len(rooms)}")
    ok = ok and bool(rooms)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return _doctor(config)
    if args.command == "login":
        config.upload.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        command = [config.upload.executable, "--user-cookie", str(config.upload.cookie_file), "login"]
        try:
            return subprocess.run(command, check=False).returncode
        except FileNotFoundError:
            print(f"找不到投稿程序：{config.upload.executable}", file=sys.stderr)
            return 1
    if args.command == "check":
        api = BilibiliLiveAPI()
        status = 0
        for item in (room for room in config.rooms if room.enabled):
            try:
                room = api.get_room(item.id, item.name)
                state = "直播中" if room.is_live else "未开播"
                print(f"[{state}] {room.streamer} | {room.title} | {room.url}")
            except Exception as exc:
                status = 1
                print(f"[查询失败] 房间 {item.id}：{exc}", file=sys.stderr)
        return status

    configure_logging(config.app.work_dir, config.app.log_level)
    try:
        Application(config).run(once=args.once)
    except Exception:
        logging.exception("程序退出")
        return 1
    return 0
