"""赛项 I 悬挂负载：抓握 φ40×100 圆柱侧面，悬挂配重并保持 10 秒。

比赛流程（运行前夹爪须处于闭合的机械零位）：

    py task_load.py run
        1. 夹爪自动张开
        2. 放入圆柱后【回车】→ 闭合并持续夹紧；每隔 status_interval_s 显示状态，
           夹紧满 10 秒提示达标
        3. 承托负载后【回车】→ 按 [release_open] 参数释放张开
        4. 取出圆柱后【回车】→ 夹爪闭合回到零位，程序结束
    任何时候按 Ctrl+C 急停：停止并卸力全部舵机。

调试与恢复（单独执行某一步，用于标定参数或回到零位）：

    py task_load.py open            按 [gripper] 参数张开
    py task_load.py close           闭合并夹紧，回车后切换为实际位置保持
    py task_load.py release-open    按 [release_open] 参数张开
    py task_load.py release         撤去夹紧力并卸力（蜗杆自锁，夹爪不会自行张开）
    py task_load.py park            从张开状态闭合回到零位；run 中途急停后可先执行此命令
    py task_load.py home            位置模式回零（须已在零位附近）

任何命令前加 --dry-run 可离线预览报文，例如 py task_load.py --dry-run run。
"""

from pathlib import Path
import sys

from dexhand.gripper import Gripper
from dexhand import task_common
from dexhand.task_common import confirm, gripper_params, hold_until_enter

DEFAULT_PARAMS = Path(__file__).resolve().parent / "params" / "task_load.toml"


def make_parser():
    parser = task_common.make_parser("赛项 I 悬挂负载", DEFAULT_PARAMS)
    parser.add_argument("action", choices=("home", "open", "release-open", "close", "release", "park", "run"))
    return parser


def release_open_params(data, overrides):
    """释放张开使用独立覆盖表；旧参数文件省略此表时沿用原张开参数。"""
    table = data.get("release_open", {})
    allowed = {"transport_control", "transport_speed", "settle_s", "move_time_ms",
               "open_time_s", "open_position"}
    for values, name in ((table, "release_open"), (overrides, "--set release_open")):
        if not isinstance(values, dict):
            raise ValueError(f"{name} 必须为 TOML 表")
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"{name} 存在不可覆盖或未知的参数：{', '.join(sorted(unknown))}")
    return gripper_params(data, overrides, (table, "release_open"))


def body(args):
    def inner(controller, data, overrides):
        release_overrides = {key.removeprefix("release_open."): value
                             for key, value in overrides.items() if key.startswith("release_open.")}
        base_overrides = {key: value for key, value in overrides.items()
                          if not key.startswith("release_open.")}
        params = gripper_params(data, base_overrides)
        # 在任何动作前验证第二套参数，避免夹紧后才发现释放参数错误。
        if args.action in ("run", "release-open") or release_overrides:
            release_open_params(data, release_overrides)
        hold = data.get("hold", {})
        interval = hold.get("status_interval_s", 1.0)
        gripper = Gripper(controller, params)

        def release_gripper() -> Gripper:
            """释放张开使用 [release_open] 参数；该参数已在动作前验证。"""
            return Gripper(controller, release_open_params(data, release_overrides))

        action = args.action
        if action == "home":
            gripper.home()
        elif action == "open":
            gripper.open()
        elif action == "release-open":
            print("释放张开：使用 [release_open] 参数", flush=True)
            release_gripper().open()
        elif action == "close":
            gripper.close()
            hold_until_enter(controller, "夹紧中，回车停止持续夹紧并保持当前位置", params.ids, interval)
            gripper.hold_position()
        elif action == "release":
            gripper.release()
        elif action == "park":
            gripper.park()
        else:
            # 从闭合的机械零位开始：张开 → 回车夹紧 → 回车释放张开 → 回车闭合归零。
            gripper.open()
            confirm(controller, "把圆柱放入夹爪，夹爪将闭合并夹紧")
            gripper.close()
            seconds = hold_until_enter(
                controller, "持续夹紧中。负载离开支撑后计时 10 秒；测试结束先承托负载，再回车释放并张开",
                params.ids, interval, target_s=10)
            print(f"本次持续夹紧 {seconds:.1f} 秒", flush=True)
            print("释放张开：使用 [release_open] 参数", flush=True)
            release_gripper().open()
            confirm(controller, "取出圆柱，夹爪将闭合回到零位")
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
