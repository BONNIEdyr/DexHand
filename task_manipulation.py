"""赛项 III 日常手部动作：夹爪张开到指定角度后，由 3 号舵机完成捏夹子 / 拧旋钮。

    py task_manipulation.py clip     捏夹子：张开 → 3 号往返 cycles 次（每次保持 hold_s）→ 回零
    py task_manipulation.py knob     拧旋钮：张开 → 3 号转到 turn_position 保持 → 转回 → 回零
    py task_manipulation.py wrist --position 375   单独转动 3 号舵机，用于标定
    py task_manipulation.py home|open|park
"""

from pathlib import Path
import sys

from dexhand.gripper import Gripper
from dexhand import task_common
from dexhand.task_common import confirm, gripper_params
from dexhand.settings import integer, number

DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "task_manipulation.toml"
ACTIONS = ("home", "open", "park", "wrist", "clip", "knob")


def make_parser():
    parser = task_common.make_parser("赛项 III 日常手部动作", DEFAULT_PARAMS)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--position", type=int, help="wrist 动作的 3 号舵机目标位置 0~1000")
    return parser


class Wrist:
    def __init__(self, controller, table, log):
        self.controller = controller
        self.id = integer(table.get("id", 3), "wrist.id", 0, 253)
        self.move_time_ms = integer(table.get("move_time_ms", 800), "wrist.move_time_ms", 0, 30000)
        self.log = log

    def move(self, position: int, label: str):
        integer(position, "wrist position", 0, 1000)
        self.log(f"{label}：3 号舵机位置模式到 {position}")
        self.controller.move({self.id: position}, time_ms=self.move_time_ms)


def action_table(data, key, overrides):
    table = dict(data.get(key, {}))
    gripper_table = table.pop("gripper", {})
    params = gripper_params(data, overrides, (gripper_table, f"{key}.gripper"))
    return table, params


def body(args):
    def inner(controller, data, overrides):
        log = lambda text: print(text, flush=True)
        wrist = Wrist(controller, data.get("wrist", {}), log)
        action = args.action
        if action in ("home", "open", "park"):
            gripper = Gripper(controller, gripper_params(data, overrides), log)
            if action == "home":
                gripper.home()
                wrist.move(0, "回零")
            elif action == "open":
                gripper.open()
            else:
                gripper.park()
        elif action == "wrist":
            if args.position is None:
                raise ValueError("wrist 动作需要 --position")
            wrist.move(args.position, "转动")
        elif action == "clip":
            table, params = action_table(data, "clip", overrides)
            gripper = Gripper(controller, params, log)
            pinch = integer(table.get("pinch_position", 400), "clip.pinch_position", 0, 1000)
            hold_s = number(table.get("hold_s", 2.5), "clip.hold_s", 0, 30)
            rest_s = number(table.get("rest_s", 1.0), "clip.rest_s", 0, 30)
            cycles = integer(table.get("cycles", 2), "clip.cycles", 1, 10)
            gripper.home()
            wrist.move(0, "回零")
            gripper.open()
            confirm(controller, "夹子已固定在工作范围内，确认开始捏合")
            for index in range(1, cycles + 1):
                wrist.move(pinch, f"第 {index} 次捏合")
                log(f"夹口张开保持 {hold_s:g} 秒")
                gripper.sleep(hold_s)
                wrist.move(0, f"第 {index} 次松开")
                if index < cycles:
                    gripper.sleep(rest_s)
            confirm(controller, "动作完成，夹爪将回到零位")
            gripper.park()
        elif action == "knob":
            table, params = action_table(data, "knob", overrides)
            gripper = Gripper(controller, params, log)
            turn = integer(table.get("turn_position", 375), "knob.turn_position", 0, 1000)
            hold_s = number(table.get("hold_s", 1.0), "knob.hold_s", 0, 30)
            gripper.home()
            wrist.move(0, "回零")
            gripper.open()
            confirm(controller, "旋钮已固定在工作范围内，确认开始转动")
            wrist.move(turn, f"转到目标（{turn * 0.24:.0f}°）")
            log(f"保持 {hold_s:g} 秒")
            gripper.sleep(hold_s)
            wrist.move(0, "转回起始位置")
            confirm(controller, "动作完成，夹爪将回到零位")
            gripper.park()
        if controller.bus.dry_run:
            print("以上为离线预览报文，未打开串口。", flush=True)
        return 0
    return inner


def main(argv=None) -> int:
    args = make_parser().parse_args(argv)
    return task_common.run(args, body(args))


if __name__ == "__main__":
    sys.exit(main())
