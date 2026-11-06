"""赛项 I 悬挂负载：抓握 φ40×100 圆柱侧面，悬挂配重并保持 10 秒。

    py task_load.py home            夹爪位置模式回零（须已在零位附近）
    py task_load.py open            张开
    py task_load.py close           闭合并夹紧
    py task_load.py release         撤去夹紧力并卸力
    py task_load.py park            从张开状态回到零位
    py task_load.py run             完整流程：回零 → 张开 → 放入圆柱 → 夹紧保持 → 释放 → 回零
"""

from pathlib import Path
import sys

from dexhand.gripper import Gripper
from dexhand import task_common
from dexhand.task_common import confirm, gripper_params, hold_until_enter

DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "task_load.toml"


def make_parser():
    parser = task_common.make_parser("赛项 I 悬挂负载", DEFAULT_PARAMS)
    parser.add_argument("action", choices=("home", "open", "close", "release", "park", "run"))
    return parser


def body(args):
    def inner(controller, data, overrides):
        params = gripper_params(data, overrides)
        hold = data.get("hold", {})
        interval = hold.get("status_interval_s", 1.0)
        gripper = Gripper(controller, params)
        action = args.action
        if action == "home":
            gripper.home()
        elif action == "open":
            gripper.open()
        elif action == "close":
            gripper.close()
            hold_until_enter(controller, "夹紧保持中，回车释放", params.ids, interval)
            gripper.release()
        elif action == "release":
            gripper.release()
        elif action == "park":
            gripper.park()
        else:
            gripper.home()
            gripper.open()
            confirm(controller, "把圆柱放入夹爪，确认可以闭合")
            gripper.close()
            seconds = hold_until_enter(
                controller, "夹紧保持中。负载离开支撑后计时 10 秒；测试结束先承托负载，再回车释放",
                params.ids, interval, target_s=10)
            print(f"本次保持 {seconds:.1f} 秒", flush=True)
            gripper.release()
            gripper.open()
            confirm(controller, "确认圆柱已取出，夹爪将回到零位")
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
