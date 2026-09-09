# 主机启停、PLR 分配与运行时间优先级

外层 GA 优化温度序列，内层按候选水温计算每种主机组合的供冷分配及全站功率，再通过动态规划选择整段启停路径。以下约束只针对主机，泵和塔仍按各时段所需流量/散热量选择。

## 配置

下面是顶层 `chiller_scheduling` 配置示例，数组按 `chillers` 的设备顺序填写，长度必须等于主机台数：

```json
{
  "chiller_scheduling": {
    "min_on_hours": 2,
    "min_off_hours": 2,
    "initial_on": [false, false, false, false],
    "initial_state_hours": [5, 3, 2, 4],
    "runtime_hours": [1200, 800, 450, 1000],
    "prefer_shorter_runtime": true,
    "terminal_policy": "require_complete"
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `min_on_hours` | 一次启动后至少连续运行多久，默认 2 小时 |
| `min_off_hours` | 一次停机后至少连续停机多久，默认 2 小时 |
| `initial_on` | 第一时段开始前的运行状态，`true` 开机，`false` 停机 |
| `initial_state_hours` | 初始状态已经连续保持的小时数；开机设备填连续开机时长，停机设备填连续停机时长 |
| `runtime_hours` | 历史累计开机小时数，用于运行均衡；不是本次连续状态时长 |
| `prefer_shorter_runtime` | 默认 `true`，在总电耗相同时优先较短累计运行时间 |
| `terminal_policy` | `require_complete` 或显式选择 `carry_over`，见下文 |

三个数组默认是 `null`：默认全停；默认当前状态已保持到允许切换；历史运行小时未知时，停机设备按 0，运行设备按其已知连续开机时长计。真实项目应填写实际数据，不能把这些示例假设当作现场状态。已开机设备的累计运行小时不能小于已连续开机时长。

若只想传入累计运行小时，可以直接使用命令行，它覆盖配置中的 `runtime_hours`，并写入本次配置快照：

```bash
python main.py --load examples/load.csv --runtime-hours 1200 800 450 1000 --output outputs/runtime_plan
```

Python 调用方式：

```python
from config import load_config
from ga_series import SeriesGA
from load_generate import read_load_csv

cfg = load_config()
cfg["chiller_scheduling"]["runtime_hours"] = [1200, 800, 450, 1000]
result = SeriesGA(cfg).optimize(read_load_csv("examples/load.csv"))
```

## 2 小时如何计算

状态在时段开始处切换，然后连续保持整个 `duration_hours`。计时使用实际小时，不按行数计数。例如：

- 1 小时间隔：启动后至少跨过两个完整时段才允许停机。
- 15 分钟间隔：启动后至少持续八个时段；停机时间同理。
- 初始已开机 1.5 小时：至少再运行 0.5 小时；若下个可切换边界在 1 小时后，则必须保持这一整小时。
- 已停机 0.5 小时：未来还需停机至少 1.5 小时，即使它的累计运行小时很少，也不能提前启动。

零负荷仍要求所有设备关闭。若上一状态的最小开机时间尚未满足、又遇到零负荷，程序报告无可行方案，不擅自提前停机、丢弃冷量或虚构卸载运行。模型没有蓄冷和无负荷空转状态。

## 计划末尾如何处理

默认 `require_complete` 要求计划结束时各机没有未完成的最小开/停时长，包括从初始状态继承的剩余时间。因此，最后只剩 1 小时时不允许新启动一台要求至少运行 2 小时的主机；也不允许在最后 1 小时新停机而留下不足 2 小时的停机段。计划结束不等于自动关闭所有设备。

滚动规划可显式设置 `carry_over`，允许末尾保留未到期的状态，但返回 `terminal_chiller_state.remaining_lock_hours`。必须把末态传入紧接着的下一段规划，例如：

```python
state = result["terminal_chiller_state"]
# 先确认下一段配置的设备 ID/顺序与 state["chiller_ids"] 完全一致。
assert [d["id"] for d in cfg["chillers"]] == state["chiller_ids"]
for key in ("initial_on", "initial_state_hours", "runtime_hours"):
    cfg["chiller_scheduling"][key] = state[key]
next_result = SeriesGA(cfg).optimize(next_load_rows)
```

首段末尾仍有 1 小时开机锁定时，下一段不能立即停机。`carry_over` 不会自动执行后续控制，跨段承诺只有在后续规划继承这些状态时才能得到约束；实际执行偏离计划时应输入真实设备状态。连续规划还应将最后一行的四个水温写入 `optimization.initial_temperatures`，以延续水温变化限制。两段时间必须连续，末态只适用于紧接其后的下一段。

## 运行时间优先级

顺序为：**满足硬约束 → 总电耗最低 → 在同耗电方案中优先运行时数较短的机组**。运行均衡不会覆盖供冷能力、最小开停时间或温度限制，也不会默认多付电耗。

输入 `[1200, 800, 450, 1000]` 时，其他条件和耗电相同，会倾向优先使用第三台。规划过程中按开机时段累加小时，停机不累加。状态路径的同耗电比较采用累计小时平方和的增量：`Σ[2 × 历史小时 × 新增小时 + 新增小时²]`，以偏向使用历史小时较短的设备并分散新增运行时间。关闭 `prefer_shorter_runtime` 可取消该偏好。

能耗比较使用 1e-9 kWh 数值容差。动态规划在同一启停/剩余锁定状态保留一条最低电耗路径，能耗相同再比较运行均衡分数。因此，固定温度和分配模型下，能耗按动态规划求解；运行均衡属于同耗电路径的选择偏好，不保证在所有等能耗路径中得到全局最优的运行时数均衡。若不同主机效率明显不同，较短运行时数的机组可能不会入选。

## 默认 PLR 分配

配置 `load_allocation: null` 使用额定冷量加权分配：

```text
单机冷量 Q_i = 当前总冷量 × 单机额定冷量 / 当前组合额定冷量之和
单机 PLR_i = Q_i / 单机额定冷量
```

例如 1000 kW 与 2000 kW 两台机组承担 1500 kW，分配为 500 kW 与 1000 kW，各自 PLR 都是 50%。默认分配不会因为某台效率更高就额外给它负荷；不同分配逻辑可以通过下一节的接口替换。

## 自定义分配模型

`load_allocation` 与主机 `predictor` 是两个接口：前者决定各机承担多少冷量，后者预测给定冷量和水温下的功率。自定义分配不必修改 GA。

顶层配置示例：

```json
{
  "load_allocation": {
    "factory": "examples.load_allocators:create_priority_allocator",
    "options": {"priority_ids": ["CH-2", "CH-1", "CH-3", "CH-4"]}
  }
}
```

完整示例实现位于 [`examples/load_allocators.py`](../examples/load_allocators.py)：先为当前组合的每台主机保留最小 PLR 冷量，再按指定顺序增加冷量至各自最大 PLR。两台相同的 1000 kW 主机、最小 PLR 15%，承担总冷量 1000 kW 时，上述优先顺序会给 CH-2 分配 850 kW、CH-1 分配 150 kW，而默认是各 500 kW。

`priority_ids` 只影响组合内分配，不能强制主机提前启动。未包含的设备按 ID 顺序排在后面，不属于当前候选组合的 ID 不参与分配。

工厂接口：

```python
def create_allocator(options):
    # 一次加载你的分配参数或模型。
    def allocate(inputs):
        # inputs.load_kw: 当前时段全站需求 kW。
        # inputs.chillers: 当前候选组合，元素有 id/capacity_kw/min_plr/max_plr。
        # 还有四个水温与 wet_bulb_c。
        # 返回所有且仅当前组合的 ID -> 单机冷量(kW)。
        return {device.id: ... for device in inputs.chillers}
    return allocate
```

如果自己的模型输出 PLR，返回前转换为 `load_kw = plr × capacity_kw`。接口校验所有设备键、有限非负输出、总冷量守恒及每台 PLR。不能正常分配时可抛出 `load_allocation.AllocationInfeasible` 拒绝该组合；键缺失、NaN、负数、总量不守恒等程序错误会中止规划。不会静默归一化错误结果。

分配器在每次规划中加载一次。它可以是公式、优化求解器或学习模型，但必须是确定性的单点分配，不应在内部累积运行时间、隐状态或在线训练。当前动态规划假设同一时段、同一组合和水温对应固定冷量及功率；跨时段状态相关的分配模型需要进一步扩展状态空间。最终复核会再次调用同一个分配器并检查各机冷量一致。

## 输出与检查

每行输出四个按配置顺序排列的数组，均表示该时段结束时：

- `chiller_on`：开关状态。
- `chiller_runtime_hours`：历史累计小时加本段已规划运行小时。
- `chiller_state_hours`：当前开/停状态已连续保持的小时。
- `chiller_remaining_lock_hours`：还需保持当前状态的小时。

`result.json` 的 `terminal_chiller_state` 提供最终状态以及明确的 `chiller_ids` 顺序。程序在导出前独立回放全时段，检查首时段切换、连续开停时长、末尾策略、小时累计、冷量分配和已有热平衡。

专项测试：

```bash
python -m unittest discover -s tests -p test_chiller_scheduling.py -v
```

测试包括短时段、混合间隔、初始锁定、零负荷冲突、跨段继承、运行小时优先级，以及小规模全枚举与动态规划能耗结果比较。设备较多或时段很细时，启停状态空间会增大；此实现适合小型离线示例，未加入启停电耗、故障可用性或现场联锁。
