"""三个总线舵机的命令行入口。运行 py main.py --help 查看用法。"""

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
import time

import serial
from serial.tools import list_ports

from dexhand.servo_controller import ServoController
from dexhand.settings import load_config, number


DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.toml")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="总线舵机：位置控制 / 减速电机控制 / ID 管理")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只显示发送报文，不打开串口")
    parser.add_argument("--verbose", action="store_true", help="显示通信日志")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="列出可用串口")

    def command(name, help_text):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--ids", nargs="+", type=int, help="目标 ID，默认配置中的全部舵机")
        return item

    command("status", "读取模式、位置、电压、温度和装载状态")
    command("stop", "停止并卸力（不保持力矩）")
    read_id = commands.add_parser("read-id", help="读取硬件 ID；广播发现时只能连接一只舵机")
    read_target = read_id.add_mutually_exclusive_group(required=True)
    read_target.add_argument("--id", type=int, help="已知的当前硬件 ID")
    read_target.add_argument("--discover", action="store_true", help="广播发现未知 ID，仅连接一只舵机")
    set_id = commands.add_parser("set-id", help="修改硬件 ID（掉电保存），建议只连接目标舵机")
    set_id.add_argument("old_id", type=int, help="当前硬件 ID")
    set_id.add_argument("new_id", type=int, help="新的空闲硬件 ID，0~253")
    mode = command("mode", "切换模式，零速并保持卸力；用 move/motor 开始运动")
    mode.add_argument("mode", choices=("servo", "motor"))
    mode.add_argument("--control", choices=("duty", "speed"), help="电机控制方式")
    move = command("move", "进入位置模式并移动；多个 ID 使用统一启动指令")
    positions = move.add_mutually_exclusive_group(required=True)
    positions.add_argument("--position", type=int, help="所有目标使用相同位置，0~1000")
    positions.add_argument("--positions", type=int, nargs="+", help="按 --ids 顺序指定各目标位置")
    move.add_argument("--time-ms", type=int, help="动作时间 0~30000ms；默认各舵机配置值")
    move.add_argument("--no-wait", action="store_true", help="发送后立即退出，舵机继续执行动作")
    motor = command("motor", "进入减速电机模式运行，Ctrl+C 停止")
    motor.add_argument("--speed", type=int, required=True, help="正转为正，反转为负，0 停止")
    motor.add_argument("--control", choices=("duty", "speed"), help="默认各舵机配置值")
    motor.add_argument("--duration", type=float, help="运行秒数；省略时等待 Ctrl+C")
    sequence = command("sequence", "同一批舵机先到指定位置，再切换电机模式运行")
    sequence_positions = sequence.add_mutually_exclusive_group(required=True)
    sequence_positions.add_argument("--position", type=int, help="所有目标使用相同位置，0~1000")
    sequence_positions.add_argument("--positions", type=int, nargs="+", help="按 --ids 顺序指定位置")
    sequence.add_argument("--time-ms", type=int, help="位置动作时间；默认各舵机配置值")
    sequence.add_argument("--speed", type=int, required=True, help="电机控制值；范围由 --control 决定")
    sequence.add_argument("--control", choices=("duty", "speed"), help="默认各舵机配置值")
    sequence.add_argument("--duration", type=float, help="电机运行秒数；省略时等待 Ctrl+C")
    apply = command("apply", "执行配置文件中各舵机的模式和运动参数")
    apply.add_argument("--duration", type=float, help="非零速电机运行秒数；省略时等待 Ctrl+C")
    return parser


def wait_for_motors(controller: ServoController, duration: float | None):
    if controller.bus.dry_run or not controller.has_running_motors:
        return
    if duration is None:
        print("电机运行中，按 Ctrl+C 停止并卸力。", flush=True)
        while True:
            time.sleep(0.1)
    else:
        print(f"电机运行 {duration:g} 秒后停止并卸力。", flush=True)
        time.sleep(duration)


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    try:
        if args.command == "ports":
            ports = sorted(list_ports.comports(), key=lambda p: p.device)
            for port in ports:
                print(f"{port.device}\t{port.description}")
            if not ports:
                print("未发现串口，请连接 USB 转总线舵机接口。")
            return 0
        config = load_config(args.config)
        duration = getattr(args, "duration", None)
        if duration is not None:
            number(duration, "duration", 0.001, 86400)
        if args.command in ("status", "read-id") and args.dry_run:
            raise ValueError(f"{args.command} 需要读取硬件应答，不支持 --dry-run")
        with ServoController(config, dry_run=args.dry_run) as controller:
            if args.command == "read-id":
                print(f"当前硬件 ID：{controller.read_id(None if args.discover else args.id)}")
                return 0
            if args.command == "set-id":
                controller.set_id(args.old_id, args.new_id)
                if args.dry_run:
                    print("仅预览 ID 修改报文，硬件 ID 未改变。")
                else:
                    print(f"已验证硬件 ID 从 {args.old_id} 改为 {args.new_id}；请同步修改配置文件中的 ID。")
                return 0
            ids = controller.selected_ids(args.ids)
            if args.command == "status":
                failed = False
                for servo_id in ids:
                    try:
                        status = controller.read_status(servo_id)
                        print(json.dumps({**asdict(status), "angle_deg": status.angle_deg},
                                         ensure_ascii=False))
                    except (TimeoutError, RuntimeError, serial.SerialException) as exc:
                        failed = True
                        print(f"ID {servo_id}: {exc}", file=sys.stderr)
                return 1 if failed else 0
            if args.command == "mode":
                controller.set_mode(args.mode, ids, control=args.control)
                print(f"已{'预览' if args.dry_run else '发送'}模式设置：{args.mode}；保持卸力。")
            elif args.command == "move":
                values = args.positions if args.positions is not None else [args.position] * len(ids)
                if len(values) != len(ids):
                    raise ValueError("--positions 数量必须与目标 ID 数量一致")
                controller.move(dict(zip(ids, values)), time_ms=args.time_ms, wait=not args.no_wait)
                print("位置控制报文已" + ("预览。" if args.dry_run else "发送；到位情况可用 status 查询。"))
            elif args.command == "motor":
                controller.run_motor(args.speed, ids, control=args.control)
                wait_for_motors(controller, duration)
            elif args.command == "sequence":
                values = args.positions if args.positions is not None else [args.position] * len(ids)
                if len(values) != len(ids):
                    raise ValueError("--positions 数量必须与目标 ID 数量一致")
                controller.move_then_motor(dict(zip(ids, values)), args.speed,
                                           time_ms=args.time_ms, control=args.control)
                print("已预览位置与电机阶段报文。" if args.dry_run else
                      "位置阶段计时结束，已切换至电机模式。")
                wait_for_motors(controller, duration)
            elif args.command == "apply":
                controller.apply_config(ids)
                wait_for_motors(controller, duration)
            elif args.command == "stop":
                controller.stop(ids)
                print("已" + ("预览" if args.dry_run else "发送") + "停止和卸力指令。")
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已对本次运动涉及的舵机尝试发送停止指令。")
        return 130
    except (OSError, ValueError, RuntimeError, serial.SerialException) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
