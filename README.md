# DexHand 灵巧手控制程序

依据本目录的《02 总线舵机通信协议.pdf》，使用 Python 3.11+ 和 pyserial 控制 ID 1、2、3 三只总线舵机。`main.py` 提供位置模式、减速电机模式、状态查询、ID 管理和离线报文预览等底层操作；`task_*.py` 按《灵巧手比赛内容与规则.md》的三个赛项封装夹爪开合和腕部动作，参数放在 `params/`。

第一次使用请按顺序看第 1、2、3 节完成接线、配置和基本验证，再看第 6 节运行赛项脚本。

## 0. 目录结构

```
DexHand/
├─ main.py                 底层调试：status / move / motor / set-id 等
├─ task_load.py            赛项 I  悬挂负载
├─ task_grasping.py        赛项 II 多物体抓取
├─ task_manipulation.py    赛项 III 捏夹子 / 拧旋钮
├─ config.toml             串口与舵机 ID（硬件配置，比赛前改好）
├─ params/                 三个赛项的动作参数，赛场上只改这里
├─ dexhand/                库代码：协议、配置校验、夹爪动作、命令行框架
├─ documents/              本说明、协议 PDF、比赛规则、requirements.txt
└─ tests/                  无硬件单元测试
```

根目录只放会直接运行或编辑的文件；所有脚本都在根目录执行。

## 1. 接线和安装

使用与舵机匹配的 **USB 转半双工总线舵机接口**，接口需自动处理收发方向；舵机连接外部适配电源并与接口共地。PDF 第 1 页有半双工接口电路，不能把普通全双工 USB-TTL 的 TX/RX 任意并接到舵机总线。本程序不实现 RTS 手动切换或特定厂商控制板的额外封装协议。

先确认三个舵机的实际 ID 已分别设为 1、2、3。协议说明出厂 ID 为 1，修改 `config.toml` 不会改写硬件 ID。现在可用 `read-id` 查询、`set-id` 改写硬件 ID；操作时建议每次只接一只舵机，改写后同步更新配置文件。程序不会自动修改角度偏差或其他 EEPROM 参数。

在项目根目录的 PowerShell 终端执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r documents/requirements.txt
.\.venv\Scripts\python.exe main.py ports
```

把 `config.toml` 中的 `serial.port` 改为实际串口，例如 `COM5`。协议规定的波特率为 `115200`，程序默认串口格式为 8N1。修改 `baudrate` 只改变电脑串口设置，不能通过此配置修改舵机硬件波特率。

## 2. 配置

`config.toml` 中的 `[serial]` 可修改串口、波特率、数据位、校验位、停止位、应答超时、写超时、通信间隔及读重试次数。

每个 `[[servos]]` 对应一个舵机：

| 参数                                | 含义                                                                |
| ----------------------------------- | ------------------------------------------------------------------- |
| `id`                              | 硬件 ID，当前为 1、2、3                                             |
| `mode`                            | `servo`：位置模式；`motor`：减速电机模式                        |
| `position`                        | 0～1000，对应 0～240°；500 对应 120°                              |
| `move_time_ms`                    | 0～30000 毫秒；默认 1000；0 的实际运动行为由固件决定                |
| `min_position` / `max_position` | 软件位置限制，默认 0～1000；按机构范围收窄                          |
| `motor_control`                   | `duty`：固定占空比；`speed`：固定转速                           |
| `motor_speed`                     | `duty` 为 −1000～1000；`speed` 为 −50～50；负数反转，正数正转 |

协议未注明固定转速参数的物理单位，本程序直接使用协议值，不将其标为 RPM。模式及速度按协议不支持掉电保存。配置文件不会被运行命令自动改写。

`apply` 才会执行配置中的 `mode`、`position`、`motor_speed`。`move` / `motor` 显式进入对应模式，使用命令行目标值及配置中的默认动作时间、控制方式和位置限制。命令行覆盖仅对该次运行有效。

`sequence` 可让同一批舵机在一次运行中先执行位置动作，再切换到电机模式；同一只舵机不能在同一时刻处于两种模式。

## 3. 底层命令（main.py）

`main.py` 用于接线验证、标定和应急操作；比赛动作请用第 6 节的赛项脚本。以下命令均在项目根目录执行。全局选项 `--config`、`--dry-run`、`--verbose` 放在子命令前；`--ids` 默认选中配置中的全部 ID。

先离线查看报文，不打开串口、不产生运动：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run move --positions 300 500 700 --time-ms 1000
```

读取三个舵机的状态，建议第一次连接时先运行：

```powershell
.\.venv\Scripts\python.exe main.py status
```

读取已知 ID，或在总线上只连接一只舵机时发现未知 ID：

```powershell
.\.venv\Scripts\python.exe main.py read-id --id 1
.\.venv\Scripts\python.exe main.py read-id --discover
```

例如将硬件 ID 1 改为尚未占用的 4；先预览报文，再实际写入并验证：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run set-id 1 4
.\.venv\Scripts\python.exe main.py set-id 1 4
```

`read-id --discover` 只能在单舵机总线上使用，否则多个应答会冲突。`set-id` 先验证旧 ID 有响应、新 ID 无响应，再停止卸力、写入并用新 ID 复查；ID 写入掉电保存。若复查失败，硬件 ID 可能已经改变，应先单独查询，不要盲目重发。真实写入后须修改 `config.toml` 中对应的 `id`；`read-id` 不支持 `--dry-run`。

控制三个舵机分别到位置 300、500、700，动作时间 1000 毫秒：

```powershell
.\.venv\Scripts\python.exe main.py move --positions 300 500 700 --time-ms 1000
```

控制单个舵机，或令三个舵机到相同位置：

```powershell
.\.venv\Scripts\python.exe main.py move --ids 2 --position 500 --time-ms 1000
.\.venv\Scripts\python.exe main.py move --position 500
```

位置参数按 `--ids` 的顺序对应；未指定 ID 时按配置文件顺序对应。默认等待设定的最长动作时间再退出；该等待不等于检测到位，可用 `status` 检查实际位置。`--no-wait` 可发送后直接退出。动作完成后保持力矩，使用 `stop` 卸力。

只切换模式，不开始运动；切换后保持卸力，电机速度设为 0：

```powershell
.\.venv\Scripts\python.exe main.py mode servo
.\.venv\Scripts\python.exe main.py mode motor --ids 1 2 3 --control duty
```

三个舵机进入减速电机模式，以固定占空比值 200 正转 3 秒，之后停止并卸力：

```powershell
.\.venv\Scripts\python.exe main.py motor --speed 200 --control duty --duration 3
```

ID 1 反转，以及固定转速控制示例：

```powershell
.\.venv\Scripts\python.exe main.py motor --ids 1 --speed -200 --duration 2
.\.venv\Scripts\python.exe main.py motor --ids 1 2 3 --speed -20 --control speed --duration 3
```

不带 `--duration` 时电机持续运行，程序等待 **Ctrl+C**，然后尝试停止并卸力。速度为 0 时直接返回。离线预览不会等待运行时长，并会显示正常退出时的电机停止报文。

同一只舵机先在位置模式移动到 500（设定动作时间 1 秒），再切换电机模式反转 3 秒：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run sequence --ids 1 --position 500 --time-ms 1000 --speed -20 --control speed --duration 3
.\.venv\Scripts\python.exe main.py sequence --ids 1 --position 500 --time-ms 1000 --speed -20 --control speed --duration 3
```

`sequence` 的两个阶段使用相同目标 ID；先按设定时间等待，再切换模式。等待不等于确认到位；切换时会停止并卸力，因此不会继续保持原来的位置力矩。

按照配置文件运行，可分别给三个舵机设置不同模式、位置和速度：

```powershell
.\.venv\Scripts\python.exe main.py apply
.\.venv\Scripts\python.exe main.py apply --duration 3
.\.venv\Scripts\python.exe main.py --config config.toml --verbose apply --ids 1 2
```

混合模式下，先完成位置模式舵机的设定时间，再启动电机模式舵机；`--duration` 从所有非零速电机指令发送完毕后计时，各电机启动时刻会有通信间隔。若配置的电机速度均为 0，则不等待。

停止并卸力：

```powershell
.\.venv\Scripts\python.exe main.py stop
.\.venv\Scripts\python.exe main.py stop --ids 2
```

`stop` 会发送停止及卸载指令，**停止后不保持力矩**，不是机械制动。停止不会清除所有固件历史参数；再次运动请使用 `move` / `motor` 重新设置。运行中的 `motor` 占用串口，请在原终端按 Ctrl+C，而不是同时另开进程访问同一串口。关闭串口本身不会让硬件自动停止；程序退出时会尝试发送停止指令，但断线、断电或进程被强制终止时无法保证送达。

## 4. 协议实现说明

- 帧：`55 55 ID Length Cmd Parameters Checksum`；`Length = 参数数量 + 3`，完整帧长度为 `Length + 3`。
- 校验：`(~sum(ID, Length, Cmd, Parameters)) & 0xFF`。多字节参数低字节在前，负速度使用 16 位有符号补码。
- 模式写指令 `29 / 0x1D`：`[mode, motor_control, speed_low, speed_high]`。两种控制方式按 PDF 第 9 页实现。
- ID 写入指令 `13 / 0x0D` 和读取指令 `14 / 0x0E`：广播读取只适用于单舵机总线；写入 ID 会掉电保存。协议的其他角度、电压、LED 等指令尚未封装为高级接口。
- 位置预设指令 `7` + 启动指令 `11`：选中配置中所有舵机时广播 `0xFE` 启动；仅选中部分舵机时逐个定向启动，避免启动未选中舵机的旧预设。定向启动存在通信时间差。
- 使用广播同步运动的前提是总线上只有配置中的舵机；广播无法限制接收者，不要在同一总线上另接未配置的舵机。
- 模式切换先卸力、停止，再写零速的新模式；`move` / `motor` 在准备目标后装载。切换不是连续无扰的运动控制，位置模式装载瞬间的行为仍需在实际机构上验证。
- 写指令没有应答，程序只能确认串口发送结果。读指令会检查 ID、指令、参数长度及校验和，处理串口碎片、噪声、回显、超时和有限重试。
- `status` 返回模式、速度参数、位置原始值、角度换算、电压 mV、温度 °C 和装载状态。位置按有符号 16 位数解析，允许负读数；位置和速度参数不应等同于独立测速结果。
- PDF 第 9 页将模式参数 2 定义为控制方式，但第 14 页的应答说明仍称其为空值。故 `status.mode_parameter_2` 保留原始应答，不据此保证判断实际控制方式。

程序不擅自修改舵机硬件的过压、温度或角度保护参数。ID、速度和位置等参数会在发送整组运动指令前校验；一个 ID 的停止报文发送失败时，仍会继续尝试停止其他 ID。

## 5. 在自己的 Python 程序中调用

```python
from pathlib import Path
import time

from dexhand.settings import load_config
from dexhand.servo_controller import ServoController

config = load_config(Path("config.toml"))
with ServoController(config) as controller:
    controller.move({1: 300, 2: 500, 3: 700}, time_ms=1000)
    print(controller.read_status(1))
    controller.run_motor(-20, [1, 2, 3], control="speed")
    time.sleep(2)
    controller.stop()
```

同一只舵机也可以在一个 `with` 块中直接调用 `controller.move(...)` 后调用 `controller.run_motor(...)`；`move_then_motor()` 把这一顺序封装为一个方法，并在位置运动前检查电机参数。已知 ID 可用 `controller.read_id(1)` 查询；未知 ID 可用 `controller.read_id()` 广播发现（只接一只舵机）；`controller.set_id(1, 4)` 会改写硬件并复查，但不会保存到 TOML。

`run_motor()` 发送后立即返回，库调用方负责运行时长；`speed` 也可以传按 ID 的映射（如 `{1: 20, 2: -20}`）给各舵机不同速度，速度报文会连续发送。退出 `with` 时会自动尝试停止本实例启动的非零速电机并关闭串口。位置动作正常退出后保持位置。共享同一串口的整组动作应由同一个控制线程调度。

脚本需放在项目根目录运行，或把根目录加入 `PYTHONPATH`，否则找不到 `dexhand` 包。夹爪开合可以直接复用 `dexhand.gripper`：

```python
from dexhand.gripper import Gripper, GripperParams
from dexhand.task_common import load_params

params = GripperParams.from_table(load_params("params/task_load.toml")["gripper"])
with ServoController(config) as controller:
    gripper = Gripper(controller, params)
    gripper.open()
    gripper.close()      # 闭合并保持 grip_duty 夹紧力
    gripper.release()
    gripper.park()
```

## 6. 比赛赛项脚本

三个赛项各有一个脚本和一个参数文件；夹爪开合动作在 `dexhand/gripper.py` 中只实现一次，命令行框架在 `dexhand/task_common.py`。硬件参数（串口、ID、`min/max_position`）仍在 `config.toml`，动作参数在各赛项的 TOML 中。

| 赛项             | 脚本                     | 参数文件                          | 动作                                                     |
| ---------------- | ------------------------ | --------------------------------- | -------------------------------------------------------- |
| I 悬挂负载       | `task_load.py`         | `params/task_load.toml`         | `home` `open` `close` `release` `park` `run` |
| II 多物体抓取    | `task_grasping.py`     | `params/task_grasping.toml`     | 同上，外加 `list`；用 `--object G03` 选物体          |
| III 日常手部动作 | `task_manipulation.py` | `params/task_manipulation.toml` | `home` `open` `park` `wrist` `clip` `knob`   |

### 夹爪开合原理

夹爪由 ID 1、2 两只舵机通过蜗杆带动平行杆机构，行程超过位置模式的 240°，因此每次开合分两段：

1. **电机阶段**：两只舵机进入减速电机模式（默认 `speed` 固定转速），`mirror = true` 时 2 号方向与 1 号相反，按 `open_time_s` / `close_time_s` 计时后同时停转。两条速度报文连续发送，启动时刻只差一个通信间隔。
2. **位置阶段**：切回位置模式，同步移动到 `open_position` / `close_position`。

闭合后若 `grip_duty` 非零，再切电机占空比模式沿闭合方向输出该占空比作为夹紧力，并一直保持到 `release`。蜗杆自锁，`release` 只撤去夹紧力，物体不会自行松脱，需随后 `open`。`park` 用于从张开状态回零：电机反转 `close_time_s`，再位置模式到 0。`home` 只做位置模式回 0，仅适用于夹爪已在零位附近时。

电机阶段是**开环计时**，转过的角度受负载、电压影响，所有时间和位置数值都需实机标定。

### 常用操作

```powershell
# 先离线预览报文，不开串口、不等待
.\.venv\Scripts\python.exe task_load.py --dry-run run

# 赛项 I 完整流程：回零 → 张开 → 回车 → 夹紧并显示电压/温度 → 回车释放 → 张开 → 回零
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

所有脚本支持 `--config`、`--params`、`--dry-run`、`--verbose` 和 `--set KEY=VALUE`（临时覆盖 `[gripper]` 参数，不改文件）。流程中每个需要人工配合的节点都会等待回车；保持阶段每秒打印位置、电压、温度。**任何时候按 Ctrl+C 即急停**：停止并卸力全部舵机，返回码 130。

### 参数说明

`[gripper]` 各项含义见各 TOML 文件注释。`open_position` / `close_position` 写一个整数表示两只相同，写 `[a, b]` 分别给 1、2 号。`params/task_grasping.toml` 的 `[objects.Gxx]` 只写需要覆盖的项，`ids` 和 `mirror` 不允许覆盖；`params/task_manipulation.toml` 的 `[clip.gripper]` / `[knob.gripper]` 同理。

### 第一次上机

1. `main.py status` 确认三只舵机读数在 0 附近，否则先 `main.py move --position 0`。
2. 把 `open_time_s`、`close_time_s` 改成 0.3，`grip_duty` 改成 100，运行 `task_load.py open`，观察两只舵机方向是否对称；不对称就改 `mirror`，整体反向就把 `transport_speed` 取负。
3. 电机阶段结束后立刻 `main.py status --ids 1 2`，确认位置读数在 0~1000 内。若落在舵机电位器盲区，位置阶段的 `move` 不可靠，需调整时长让停点落在有效范围内。
4. 逐步加大时间和占空比，记录各物体的参数。

## 7. 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

当前测试使用模拟串口，不连接硬件。`test_id_sequence.py` 覆盖 ID 定向/广播读取、ID 改写验证、写入失败不重发，以及先位置后电机的顺序和参数预校验；`test_tasks.py` 覆盖夹爪开合的报文顺序、镜像方向、夹紧与释放、参数叠加规则、三个赛项脚本的离线预览和 Ctrl+C 急停。真实舵机的接线、模式支持、方向、机械限位及运动效果需要接上设备后验证。
