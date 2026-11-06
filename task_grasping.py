"""赛项 II 多物体抓取：按物体编号选择参数，抓取 → 独立保持 5 秒 → 听口令释放。

    py task_grasping.py list                       列出各物体及其生效参数
    py task_grasping.py run --object G03           完整流程
    py task_grasping.py open|close|release|park|home --object G03
    py task_grasping.py run --object G11 --set grip_duty=250   现场临时调参
"""

from dataclasses import asdict
from pathlib import Path
import sys

from dexhand.gripper import Gripper
from dexhand import task_common
from dexhand.task_common import confirm, gripper_params, hold_until_enter

DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "task_grasping.toml"
ACTIONS = ("home", "open", "close", "release", "park", "run", "list")


def make_parser():
    parser = task_common.make_parser("赛项 II 多物体抓取", DEFAULT_PARAMS)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--object", help="物体编号，如 G01；省略时使用 [gripper] 默认值")
    return parser


def object_params(data, overrides, code):
    objects = data.get("objects", {})
    if code is None:
        return gripper_params(data, overrides), "默认参数"
    if code not in objects:
        raise ValueError(f"参数文件中没有物体 {code}；已有：{', '.join(objects) or '无'}")
    table = dict(objects[code])
    name = table.pop("name", code)
    return gripper_params(data, overrides, (table, f"objects.{code}")), name


def list_objects(data, overrides):
    default = gripper_params(data, overrides)
    keys = ("open_time_s", "open_position", "close_time_s", "close_position", "grip_duty")
    print("默认：" + ", ".join(f"{k}={getattr(default, k)}" for k in keys))
    for code in data.get("objects", {}):
        params, name = object_params(data, overrides, code)
        diff = {k: v for k, v in asdict(params).items() if v != getattr(default, k)}
        print(f"{code} {name}：" + (", ".join(f"{k}={v}" for k, v in diff.items()) or "同默认"))


def body(args):
    def inner(controller, data, overrides):
        if args.action == "list":
            list_objects(data, overrides)
            return 0
        params, name = object_params(data, overrides, args.object)
        hold = data.get("hold", {})
        interval = hold.get("status_interval_s", 1.0)
        target = hold.get("hold_target_s", 5)
        gripper = Gripper(controller, params)
        print(f"物体：{name}", flush=True)
        action = args.action
        if action == "home":
            gripper.home()
        elif action == "open":
            gripper.open()
        elif action == "close":
            gripper.close()
            hold_until_enter(controller, "夹持中，回车释放", params.ids, interval, target_s=target)
            gripper.release()
        elif action == "release":
            gripper.release()
        elif action == "park":
            gripper.park()
        else:
            gripper.home()
            gripper.open()
            confirm(controller, "物体已送入夹爪范围，确认可以闭合")
            gripper.close()
            seconds = hold_until_enter(
                controller, "夹持中。撤去支撑后开始计时；听到“释放”口令后回车",
                params.ids, interval, target_s=target)
            print(f"本次保持 {seconds:.1f} 秒，开始释放", flush=True)
            gripper.release()
            gripper.open()
            confirm(controller, "确认物体已落入托盘，夹爪将回到零位")
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
