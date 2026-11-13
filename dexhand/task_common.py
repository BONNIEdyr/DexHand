"""赛项脚本共用的命令行框架：参数加载、串口生命周期、急停、操作员提示和状态监视。"""

import argparse
import logging
from pathlib import Path
import sys
import threading
import time
import tomllib

import serial

from dexhand.gripper import GripperParams
from dexhand.servo_controller import ServoController
from dexhand.settings import load_config, number

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "params" / "config.toml"


def make_parser(description: str, default_params: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--params", type=Path, default=default_params, help="本赛项参数文件")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="临时覆盖夹爪参数，例如 --set grip_duty=400 --set open_position=[600,580]")
    parser.add_argument("--dry-run", action="store_true", help="只显示报文，不打开串口、不等待")
    parser.add_argument("--verbose", action="store_true", help="显示通信日志")
    return parser


def load_params(path: Path) -> dict:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    if "gripper" not in data:
        raise ValueError(f"{path} 缺少 [gripper] 分组")
    return data


def parse_overrides(items: list[str]) -> dict:
    overrides = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--set 需要 KEY=VALUE 形式，收到 {item!r}")
        try:
            overrides[key.strip()] = tomllib.loads(f"v = {value.strip()}")["v"]
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"--set {key} 的值无法解析：{exc}") from exc
    return overrides


def gripper_params(data: dict, overrides: dict, *extra_tables: tuple[dict, str]) -> GripperParams:
    """默认 [gripper] → 赛项/物体专用覆盖 → 命令行 --set，逐层叠加。"""
    params = GripperParams.from_table(data["gripper"])
    for table, name in extra_tables:
        params = params.with_overrides(table, name)
    if overrides:
        params = params.with_overrides(overrides, "--set")
    return params


def run(args: argparse.Namespace, body) -> int:
    """打开串口并执行 body(controller, params_data)；Ctrl+C 时停止全部舵机。"""
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    try:
        config = load_config(DEFAULT_CONFIG)
        data = load_params(args.params)
        overrides = parse_overrides(args.set)
        with ServoController(config, dry_run=args.dry_run) as controller:
            try:
                return body(controller, data, overrides) or 0
            except KeyboardInterrupt:
                print("\n急停：停止并卸力全部舵机。", flush=True)
                controller.stop()
                return 130
    except (OSError, ValueError, RuntimeError, serial.SerialException) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


def confirm(controller: ServoController, prompt: str):
    """等待操作员回车；离线预览时直接继续。"""
    if controller.bus.dry_run:
        print(f"[预览] {prompt}", flush=True)
        return
    input(f"{prompt}（回车继续，Ctrl+C 急停）")


def status_line(controller: ServoController, servo_id: int) -> str:
    try:
        status = controller.read_status(servo_id)
    except (TimeoutError, RuntimeError, serial.SerialException) as exc:
        return f"ID {servo_id}: 读取失败 {exc}"
    return (f"ID {servo_id}: 位置 {status.position} ({status.angle_deg:.1f}°) "
            f"电压 {status.voltage_mv / 1000:.2f} V 温度 {status.temperature_c} ℃ "
            f"{'上力' if status.loaded else '卸力'}")


def hold_until_enter(controller: ServoController, prompt: str, ids, interval_s: float,
                     target_s: float | None = None) -> float:
    """保持当前状态并周期性打印状态，直到操作员回车；返回保持秒数。"""
    number(interval_s, "status_interval_s", 0.2, 60)
    if controller.bus.dry_run:
        print(f"[预览] {prompt}", flush=True)
        return 0.0
    done = threading.Event()

    def wait_input():
        try:
            input()
        finally:
            done.set()

    print(f"{prompt}（回车继续，Ctrl+C 急停）", flush=True)
    threading.Thread(target=wait_input, daemon=True).start()
    start = time.monotonic()
    reached = False
    while not done.wait(interval_s):
        elapsed = time.monotonic() - start
        lines = "; ".join(status_line(controller, servo_id) for servo_id in ids)
        print(f"已保持 {elapsed:5.1f} 秒 | {lines}", flush=True)
        if target_s is not None and not reached and elapsed >= target_s:
            reached = True
            print(f">>> 已达到 {target_s:g} 秒", flush=True)
    return time.monotonic() - start
