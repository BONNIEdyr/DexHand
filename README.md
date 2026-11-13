# DexHand 灵巧手控制程序

`main.py` 提供位置模式、减速电机模式、状态查询、ID 管理和离线报文预览等底层操作；`task_*.py` 按《灵巧手比赛内容与规则.md》的三个赛项封装夹爪开合和腕部动作，参数放在 `params/`。

## 0. 目录结构

```
DexHand/
├─ main.py                 底层调试：status / move / motor / set-id 等
├─ task_load.py            赛项 I  悬挂负载
├─ task_grasping.py        赛项 II 多物体抓取
├─ task_manipulation.py    赛项 III 捏夹子 / 拧旋钮
├─ params/
│  ├─ config.toml          串口与舵机 ID、位置限位（硬件配置，所有脚本共用，比赛前改好）
│  └─ task_*.toml          三个赛项的动作参数，赛场上只改这里
├─ dexhand/                库代码：协议、配置校验、夹爪动作、命令行框架
├─ documents/              本说明、协议 PDF、比赛规则、requirements.txt
└─ tests/                  无硬件单元测试
```

## 1. 环境配置

在项目根目录的 PowerShell 终端执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r documents/requirements.txt
.\.venv\Scripts\python.exe main.py ports
```

把 `params/config.toml` 中的 `serial.port` 改为实际串口，例如 `COM5`。协议规定的波特率为 `115200`，程序默认串口格式为 8N1。修改 `baudrate` 只改变电脑串口设置，不能通过此配置修改舵机硬件波特率。

## 2. 参数配置

`params/config.toml` 是 `main.py` 和三个赛项脚本共用的硬件配置，路径固定。其中 `[serial]` 可修改串口、波特率、数据位、校验位、停止位、应答超时、写超时、通信间隔及读重试次数。

每个 `[[servos]]` 对应一个舵机：

| 参数                                | 含义                                                             |
| ----------------------------------- | ---------------------------------------------------------------- |
| `id`                              | 硬件 ID，当前为 1、2、3；程序只操作这里列出的 ID                 |
| `min_position` / `max_position` | 软件位置限制，默认 0～1000（对应 0～240°）；按机构范围收窄      |
| `move_time_ms`（可选）            | `main.py move` 省略 `--time-ms` 时的动作时间，默认 1000 毫秒 |
| `motor_control`（可选）           | `main.py motor` 省略 `--control` 时的控制方式，默认 `duty` |

## 3. 底层命令（main.py）

`main.py` 用于接线验证、标定和应急操作；

命令格式：

```
py main.py [全局选项] 命令 [命令参数]
```

全局选项必须写在命令前面：

| 全局选项      | 作用                                                |
| ------------- | --------------------------------------------------- |
| `--dry-run` | 只打印要发送的报文，不打开串口、舵机不动            |
| `--verbose` | 显示通信日志                                        |
| `-h`        | 显示帮助；`py main.py move -h` 查看单个命令的参数 |

多数命令支持 `--ids`，用来选择要操作的舵机，例如 `--ids 1 2`；不写时默认选中 `params/config.toml` 中的全部舵机。

| 命令        | 作用                                 | 支持 `--dry-run` |
| ----------- | ------------------------------------ | :----------------: |
| `ports`   | 列出电脑上可用的串口                 |         —         |
| `status`  | 读取模式、位置、电压、温度、是否上力 |         ❌         |
| `read-id` | 读取舵机硬件 ID                      |         ❌         |
| `set-id`  | 修改舵机硬件 ID（掉电保存）          |         ✅         |
| `move`    | 进入位置模式，转到指定位置           |         ✅         |
| `motor`   | 进入减速电机模式，连续转动           |         ✅         |
| `stop`    | 停止并卸力                           |         ✅         |

### ports：列出串口

```powershell
py main.py ports
```

用来确认舵机接口对应的串口号，再填进 `params/config.toml` 的 `serial.port`。此命令不读取配置文件。

### status：读取状态

| 参数      | 含义                   |
| --------- | ---------------------- |
| `--ids` | 要读取的舵机，默认全部 |

```powershell
py main.py status
py main.py status --ids 1 2
```

每只舵机输出一行 JSON，包含模式、位置原始值 `position`（0~1000）、角度 `angle_deg`、电压 mV、温度 °C 和是否上力。需要读取舵机应答，不支持 `--dry-run`。

### read-id：读取硬件 ID

| 参数           | 含义                                            |
| -------------- | ----------------------------------------------- |
| `--id N`     | 检查已知 ID 为 N 的舵机是否有响应               |
| `--discover` | 广播查找未知 ID，**总线上只能接一只舵机** |

两者必须二选一。

```powershell
py main.py read-id --id 1
py main.py read-id --discover
```

总线上接了多只舵机时使用 `--discover`，多个应答会冲突。不支持 `--dry-run`。

### set-id：修改硬件 ID

| 参数     | 含义               |
| -------- | ------------------ |
| `旧ID` | 当前硬件 ID        |
| `新ID` | 新的空闲 ID，0~253 |

```powershell
py main.py --dry-run set-id 1 4
py main.py set-id 1 4
```

写入后须同步修改 `params/config.toml` 中对应的 `id`。

### move：位置模式

| 参数                  | 含义                                                           |
| --------------------- | -------------------------------------------------------------- |
| `--ids`             | 目标舵机，默认全部                                             |
| `--position N`      | 所有目标转到同一位置，0~1000 对应 0~240°                      |
| `--positions A B…` | 按 `--ids` 顺序分别指定位置；未写 `--ids` 时按配置文件顺序 |
| `--time-ms T`       | 动作时间 0~30000 毫秒，默认 1000                               |
| `--no-wait`         | 发送后立即退出，不等待动作时间                                 |

`--position` 和 `--positions` 必须二选一；位置还受 `min_position` / `max_position` 限制。

```powershell
py main.py --dry-run move --positions 300 500 700 --time-ms 1000
py main.py move --positions 300 500 700 --time-ms 1000
py main.py move --ids 2 --position 500 --time-ms 1000
py main.py move --ids 1 2 --positions 300 600
py main.py move --position 500
```

默认等待设定的动作时间后退出，但这只是计时，不代表已经到位，可用 `status` 检查实际位置。多只舵机同时启动。**动作完成后舵机保持力矩**，需要卸力时用 `stop`。

### motor：减速电机模式

| 参数             | 含义                                                                              |
| ---------------- | --------------------------------------------------------------------------------- |
| `--ids`        | 目标舵机，默认全部                                                                |
| `--speed V`    | 必填；正数正转，负数反转，0 停止                                                  |
| `--control`    | `duty` 固定占空比（−1000~1000）或 `speed` 固定转速（−50~50），默认 `duty` |
| `--duration S` | 运行秒数，到时自动停止并卸力；不写则一直转，按 **Ctrl+C** 停止              |

```powershell
py main.py motor --speed 200 --control duty --duration 3
py main.py motor --ids 1 --speed -200 --duration 2
py main.py motor --ids 1 2 3 --speed -20 --control speed --duration 3
py main.py motor --ids 1 --speed 150
```

协议没有注明固定转速数值的物理单位，不要当作 RPM。速度为 0 时直接返回。`--dry-run` 不会等待运行时长，并会显示结束时的停止报文。

### stop：停止并卸力

| 参数      | 含义                   |
| --------- | ---------------------- |
| `--ids` | 要停止的舵机，默认全部 |

```powershell
py main.py stop
py main.py stop --ids 2
py main.py --dry-run stop
```

`stop` 发送停止及卸力指令，**停止后不保持力矩**，不是机械制动。它用于程序已经退出、舵机仍在出力的情况，例如 `move` 之后、`task_load.py close` 进入位置保持之后，或程序异常退出之后。程序还在运行时（如 `motor` 正在转），它会占用串口，应在原终端按 Ctrl+C，而不是另开终端运行 `stop`。断线、断电或进程被强制终止时，退出时的停止指令无法保证送达。

## 5. 比赛赛项脚本

三个赛项各有一个脚本和一个参数文件；夹爪开合动作在 `dexhand/gripper.py` 中只实现一次，命令行框架在 `dexhand/task_common.py`。硬件参数（串口、ID、`min/max_position`）在 `params/config.toml`，动作参数在各赛项的 TOML 中。

| 赛项             | 脚本                     | 参数文件                          | 动作                                                     |
| ---------------- | ------------------------ | --------------------------------- | -------------------------------------------------------- |
| I 悬挂负载       | `task_load.py`         | `params/task_load.toml`         | `home` `open` `close` `release` `park` `run` |
| II 多物体抓取    | `task_grasping.py`     | `params/task_grasping.toml`     | 同上，外加 `list`；用 `--object G03` 选物体          |
| III 日常手部动作 | `task_manipulation.py` | `params/task_manipulation.toml` | `home` `open` `park` `wrist` `clip` `knob`   |

### 夹爪开合原理

夹爪由 ID 1、2 两只舵机通过蜗杆带动平行杆机构，行程超过位置模式的 240°，因此每次开合分两段：

1. **电机阶段**：两只舵机进入减速电机模式（默认 `speed` 固定转速），`mirror = true` 时 2 号方向与 1 号相反，按 `open_time_s` / `close_time_s` 计时后同时停转。两条速度报文连续发送，启动时刻只差一个通信间隔。
2. **位置阶段**：切回位置模式，同步移动到 `open_position` / `close_position`。

闭合后若 `grip_duty` 非零，再切电机占空比模式沿闭合方向输出该占空比作为夹紧力，并一直保持到 `release`。蜗杆自锁，`release` 只撤去夹紧力，物体不会自行松脱，需随后 `open`。`park` 用于从张开状态回零：电机反转 `close_time_s`，再位置模式到 `home_position`。`home` 只做位置模式回到 `home_position`，仅适用于夹爪已在机械零位附近时；省略该参数时仍回到 `[0, 0]`。

电机阶段是**开环计时**，转过的角度受负载、电压影响，所有时间和位置数值都需实机标定。

### 常用操作

```powershell
# 先离线预览报文，不开串口、不等待
.\.venv\Scripts\python.exe task_load.py --dry-run run

# 赛项 I 完整流程（夹爪从闭合零位开始）：张开 → 回车夹紧 → 回车释放张开 → 回车闭合归零
.\.venv\Scripts\python.exe task_load.py run

# 赛项 II：列出物体参数；按物体运行；现场临时改参数
.\.venv\Scripts\python.exe task_grasping.py list
.\.venv\Scripts\python.exe task_grasping.py run --object G03
.\.venv\Scripts\python.exe task_grasping.py run --object G11 --set grip_duty=250 --set open_position=[700,680]

# 赛项 III：捏夹子两个循环；拧旋钮 90° 并转回；单独标定 3 号舵机
.\.venv\Scripts\python.exe task_manipulation.py clip
.\.venv\Scripts\python.exe task_manipulation.py knob
.\.venv\Scripts\python.exe task_manipulation.py wrist --position 375
```

所有脚本支持 `--params`、`--dry-run`、`--verbose` 和 `--set KEY=VALUE`（临时覆盖 `[gripper]` 参数，不改文件）。流程中每个需要人工配合的节点都会等待回车；保持阶段每秒打印位置、电压、温度。**任何时候按 Ctrl+C 即急停**：停止并卸力全部舵机，返回码 130。

单独执行 `task_load.py close` 时，回车后停止持续夹紧输出，读取两只舵机的实际位置，并切到位置模式保持这些位置；不会回到配置的 `close_position`。命令退出后仍保持上力，可用 `task_load.py release` 或 `main.py stop` 卸力。切换沿用底层位置动作流程，会短暂卸力再上力，不保证负载下完全无位移；位置保持仍可能发热或震动。若位置读取失败或读数超出软件限位，会报错并尝试停止卸力，不使用猜测目标。`--dry-run` 只说明读取与保持计划，不虚构实际位置。

`task_load.py run` 从闭合的机械零位开始，共三次回车：启动后夹爪先张开；放入圆柱后第一次回车，闭合并持续夹紧（电机堵转出力，不切换位置保持），期间每隔 `status_interval_s` 显示状态，10 秒达标提示从夹紧开始计时；承托负载后第二次回车，用 `[release_open]` 参数释放张开；取出圆柱后第三次回车，夹爪闭合回到零位（`park`）。持续夹紧期间电机一直出力，注意观察温度。多物体抓取脚本的回车释放行为保持原样。

悬挂负载的两次张开分别调参：首次张开和单独 `task_load.py open` 使用 `[gripper]`；保持结束后的释放张开使用 `[release_open]`，也可用 `task_load.py release-open` 单独执行。两套可分别设置 `transport_control`、`transport_speed`、`open_time_s`、`open_position`、`settle_s`、`move_time_ms`；`ids`、`mirror` 和回零设置仍共用 `[gripper]`。新表初始值复制添加时的张开设置，需要独立标定。旧文件省略新表或某个字段时，该项沿用文件中的 `[gripper]` 值。`--set open_time_s=0.3` 只临时调整首次张开；`--set release_open.open_time_s=0.3` 只临时调整释放张开，例如 `task_load.py --dry-run release-open --set release_open.open_time_s=0.3`。两套位置参数均为绝对位置，改变释放张开幅度后也需核对 `park` 的回退行程。

### 参数说明

`[gripper]` 各项含义见各 TOML 文件注释。`home_position` / `open_position` / `close_position` 写一个整数表示两只相同，写 `[a, b]` 按 `ids` 顺序分别指定，范围为 0~1000，且受 `params/config.toml` 的软件位置限位约束。`params/task_grasping.toml` 的 `[objects.Gxx]` 只写需要覆盖的项，`ids` 和 `mirror` 不允许覆盖；`params/task_manipulation.toml` 的 `[clip.gripper]` / `[knob.gripper]` 同理。

机械零位标定：先将机构调整到期望的初始姿态，再用 `main.py status --ids 3 2`（以悬挂负载当前配置为例）读取两只舵机的 `position` 原始值，而非 `angle_deg`。例如读到 420、580，就在 `params/task_load.toml` 的 `[gripper]` 中填写 `home_position = [420, 580]`。可先运行 `task_load.py --dry-run home` 检查目标，也可用 `task_load.py --dry-run home --set 'home_position=[420,580]'` 临时预览。该设置只改变回零目标，不修改舵机内部零点，也不会平移 `open_position` / `close_position`；开闭位置仍需单独标定。它不提供自动寻零或多圈绝对定位，日常动作中的腕部回零仍使用位置 0。

### 第一次上机

1. `main.py status` 确认三只舵机读数在 0 附近，否则先 `main.py move --position 0`。
2. 把 `open_time_s`、`close_time_s` 改成 0.3，`grip_duty` 改成 100，运行 `task_load.py open`，观察两只舵机方向是否对称；不对称就改 `mirror`，整体反向就把 `transport_speed` 取负。
3. 电机阶段结束后立刻 `main.py status --ids 1 2`，确认位置读数在 0~1000 内。若落在舵机电位器盲区，位置阶段的 `move` 不可靠，需调整时长让停点落在有效范围内。
4. 逐步加大时间和占空比，记录各物体的参数。

## 6. 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

当前测试使用模拟串口，不连接硬件。`test_id_sequence.py` 覆盖 ID 定向/广播读取、ID 改写验证、写入失败不重发及命令行 ID 管理；`test_tasks.py` 覆盖夹爪开合的报文顺序、镜像方向、夹紧与释放、参数叠加规则、三个赛项脚本的离线预览和 Ctrl+C 急停。真实舵机的接线、模式支持、方向、机械限位及运动效果需要接上设备后验证。
