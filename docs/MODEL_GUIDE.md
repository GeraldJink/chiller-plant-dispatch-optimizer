# 接入自己的设备模型

GA 负责提出候选控制策略，设备模型负责回答“在这个工况下耗电多少”，调度层检查设备能力和热平衡。因此，机理公式、回归器和神经网络可以参与同一个优化流程。模型不必可微，但必须响应候选工况，并提供稳定、可信的功率预测。

## 1. 选择替换方式

| 需求 | 接入位置 | 是否修改 GA |
| --- | --- | --- |
| 内置主机公式合适，只换参数 | `chiller_model` 或每台主机的 `model` | 否 |
| 自己的机理公式、经验公式、性能表 | 主机的 `predictor` 工厂 | 否 |
| scikit-learn、XGBoost、LightGBM 回归器 | 用工厂加载模型并包装推理 | 否 |
| PyTorch、TensorFlow、ONNX 等模型 | 相同工厂接口，恢复预处理并返回 kW | 否 |
| 泵、塔或管网模型 | 第 7 节列出的求解和复核位置 | 通常不用；新增决策变量时需要 |
| LSTM、带蓄冷状态等动态模型 | 扩展整段序列评估及状态管理 | 需要扩展评估层 |

目前配置式插件接口已实现的是**主机功率模型**。泵塔的扩展步骤在后文说明，尚未提供相同的配置式插件。

## 2. 先运行一个完整的替换示例

从项目根目录执行，无需安装额外包：

```bash
python -m examples.use_custom_model
```

示例把四台主机切换成虚构的 Carnot 机理近似，再执行负荷生成、GA、约束复核和结果导出。代码见 [`use_custom_model.py`](../examples/use_custom_model.py) 与 [`model_adapters.py`](../examples/model_adapters.py)。它使用绝对温度计算 Carnot COP，乘虚构效率及部分负荷修正后返回 `P = Q / COP`。

结果及完整配置保存在 `outputs/custom_model/`。可以直接复用配置，更换负荷：

```bash
python main.py --config outputs/custom_model/config.json --load examples/load.csv --output outputs/replay_custom
```

## 3. 配置与输入输出约定

复制完整默认配置为 `config/local.json`，给需要替换的主机增加 `predictor`。下面是单台设备片段，不是完整项目配置：

```json
{
  "id": "CH-1", "capacity_kw": 1000,
  "min_plr": 0.15, "max_plr": 1.0, "cop_multiplier": 1.0,
  "predictor": {
    "factory": "examples.model_adapters:create_mechanistic",
    "options": {"efficiency": 0.6},
    "domain": {
      "plr": [0.15, 1.0],
      "chw_supply_c": [6, 12],
      "cw_return_c": [23, 42]
    }
  }
}
```

`factory` 格式为 `Python模块路径:工厂函数名`，模块必须可从项目根目录导入，不能直接填 `.py` 路径。工厂接收 `options` 字典，返回一个预测函数或可调用对象：

```python
def create_model(options):
    # 只在这里加载参数、预处理器、性能表或训练权重。
    def predict_power(inputs):
        power_kw = ...  # 使用你的公式或推理过程
        return float(power_kw)
    return predict_power
```

有 `predictor` 时使用其最终功率，不再应用内置 `chiller_model`、设备 `model` 或 `cop_multiplier`。需要额外修正时在适配器内完成。没有 `predictor` 的设备继续使用默认模型，可与自定义模型混用；原有内置模型配置字段仍需保留以兼容当前配置格式。

工厂在每次规划开始时按设备调用一次，实例供全部 GA 评估及最终复核复用。独立调用 `validate_schedule` 时会重新加载一次。同一文件用于多台设备时当前会按设备加载多份。

输入类型定义在 [`model_interface.py`](../model_interface.py)，为不可变的 `ChillerInputs`：

| 属性 | 含义 |
| --- | --- |
| `load_kw` | 该台主机分配到的候选冷量，kW，不是全站负荷 |
| `capacity_kw` | 该机额定冷量，kW |
| `plr` | `load_kw / capacity_kw`，0–1 比例，不是百分数 |
| `chw_supply_c` / `chw_return_c` | 冷冻供水/回水温度，°C |
| `cw_supply_c` | 冷却供水，即出塔/进冷凝器，°C |
| `cw_return_c` | 冷却回水，即出冷凝器/进塔，°C |
| `wet_bulb_c` | 当前时段室外湿球温度，°C |

返回值必须是该机**正、有限、未归一化的电功率 kW 标量**。模型输出 W 时除以 1000；输出 COP 时先检查正且有限，再返回 `inputs.load_kw / cop`；输出标准化功率时先反归一化。接口不能自动识别单位填错。

零负荷由调度层关闭设备，不调用预测器。运行设备返回 NaN、负功率、零功率或出现模型程序异常时，规划会中止并报告设备 ID，不会将异常预测当成低耗方案。

### 有效工况域

`domain` 必须至少包括 `plr`、`chw_supply_c`、`cw_return_c`，也可增加其他输入属性。范围应根据设备能力及训练/验证数据填写，不要照抄示例。模型域与优化边界同时生效。

超域时拒绝包含该机的当前组合，其他设备仍可参与。联合工况条件可在预测器中补充：

```python
from model_interface import ModelDomainError

def predict_power(inputs):
    lift = inputs.cw_return_c - inputs.chw_supply_c
    if not 15 <= lift <= 35:  # 虚构范围，请换成自己的验证条件
        raise ModelDomainError("lift outside validated range")
    return ...
```

只有明确不适用的工况使用 `ModelDomainError`。不要捕获全部异常后返回无穷大或极小功率；依赖缺失、损坏权重和代码错误需要修复。各特征区间的笛卡尔积不一定都在训练分布中，必要时增加联合区域检查。

## 4. 机器学习模型示例

`examples.model_adapters:create_sklearn` 加载一个已拟合的单输出回归 Pipeline，使用固定顺序：

```text
[load_kw, chw_supply_c, cw_return_c]
```

输入形状 `(1, 3)`，输出形状 `(1,)`，目标为原始 `power_kw`。示例按单机训练；跨容量模型可以在训练端和适配器中一起改用 PLR、容量等特征。

**训练表与规划负荷表不同。** 训练表需要设备输入工况和功率标签；规划表只有未来需求及湿球等外部条件。以下是保存预处理和模型的最小示例，需在自己的训练环境中运行：

```python
import csv
from pathlib import Path
import joblib
import numpy as np
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestRegressor
from examples.model_adapters import FEATURE_NAMES

# 同一台主机的稳态样本，按时间排列。
with open("inputs/chiller_training.csv", encoding="utf-8-sig") as file:
    rows = list(csv.DictReader(file))
X = np.array([[float(row[name]) for name in FEATURE_NAMES] for row in rows])
y = np.array([float(row["power_kw"]) for row in rows])
assert len(y) >= 20 and np.isfinite(X).all() and np.isfinite(y).all() and (y > 0).all()
split = int(len(y) * 0.8)
model = make_pipeline(StandardScaler(), RandomForestRegressor(n_estimators=100, random_state=42))
model.fit(X[:split], y[:split])
pred = model.predict(X[split:])
print("Holdout RMSE (kW):", np.sqrt(np.mean((pred - y[split:]) ** 2)))
Path("models").mkdir(exist_ok=True)
joblib.dump(model, "models/chiller_pipeline.joblib")
```

此处标准化对树模型并非必需，主要展示预处理随模型一起保存。还应分 PLR、水温等工况检查留出集误差，不能只看整体 RMSE。持久化、环境兼容及可信文件加载要求见 [scikit-learn 官方说明](https://scikit-learn.org/stable/model_persistence.html)。记录并复用训练依赖版本，仅加载自己生成或可信来源的 joblib 文件。

将该台主机的 `predictor` 替换为：

```json
{
  "factory": "examples.model_adapters:create_sklearn",
  "options": {"artifact": "models/chiller_pipeline.joblib"},
  "domain": {"plr": [0.2, 0.9], "chw_supply_c": [7, 11], "cw_return_c": [27, 38]}
}
```

示例域需换成自己的验证范围。模型路径相对**命令运行目录**解析，本文统一从项目根目录运行；也可填绝对路径。此适配器需要 numpy、scikit-learn、joblib，默认程序仍无额外依赖。

XGBoost、LightGBM、查表插值等同样可以包装为工厂：只替换加载与预测代码，无需改 GA。

## 5. 深度学习模型示例

`examples.model_adapters:create_torch` 展示 CPU 推理。示例结构固定为 `Linear(3,16) → ReLU → Linear(16,1)`，特征顺序同上，输入标准化，输出直接为 kW。只能加载结构完全匹配的权重；其他网络请复制工厂并更改结构定义。

在自己的训练程序中，使用训练集统计量进行标准化，并保存权重与统计量：

```python
from pathlib import Path
import json
import torch

# X_train: (N,3)；y_train_kw: (N,1)，原始 kW。
mean = X_train.mean(dim=0)
scale = X_train.std(dim=0).clamp_min(1e-6)
net = torch.nn.Sequential(torch.nn.Linear(3, 16), torch.nn.ReLU(), torch.nn.Linear(16, 1))
# 在自己的训练循环中，用 (X_train - mean) / scale 训练 net，目标为 y_train_kw。
# 完成独立验证后，再执行下面保存代码。不要将随机初始化权重用于规划。
Path("models").mkdir(exist_ok=True)
torch.save(net.state_dict(), "models/chiller_mlp.pt")
metadata = {"mean": mean.tolist(), "scale": scale.tolist()}
Path("models/chiller_scaler.json").write_text(json.dumps(metadata), encoding="utf-8")
```

将训练得到的统计量写入 `options.mean`、`scale`（以下数字仅展示格式）：

```json
{
  "factory": "examples.model_adapters:create_torch",
  "options": {
    "artifact": "models/chiller_mlp.pt",
    "mean": [500, 8, 34],
    "scale": [200, 2, 4]
  },
  "domain": {"plr": [0.2, 0.9], "chw_supply_c": [7, 11], "cw_return_c": [27, 38]}
}
```

工厂用 `state_dict` 恢复权重，指定 CPU、`weights_only=True`，设置 `eval()` 后在推理上下文中运行。保存与加载方式参考 [PyTorch 官方说明](https://docs.pytorch.org/tutorials/beginner/saving_loading_models)。如果训练的是对数功率、标准化目标或不同输出激活，在适配器内完成对应变换，并保证网络结构一致。

TensorFlow/Keras、ONNX Runtime 也可用同样的函数接口包装；本项目未附这两种后端的实现。仓库不提供训练权重，也不内置训练框架。

## 6. 验证接入正确性

先选择可手工核算、位于验证域内的工况，比较适配器与训练程序的原始预测：

```python
from config import load_config
from model_interface import ChillerInputs, load_chiller_models

cfg = load_config("config/local.json")
device = cfg["chillers"][0]
predictor = load_chiller_models(cfg)[device["id"]]
x = ChillerInputs(load_kw=650, capacity_kw=device["capacity_kw"],
                  chw_supply_c=8, chw_return_c=13,
                  cw_supply_c=29, cw_return_c=34, wet_bulb_c=24)
power = predictor.power_kw(x)
print("power_kw:", power, "COP:", x.load_kw / power)
assert power == predictor.power_kw(x)
```

替换示例输入，确认特征顺序、水温进出口含义、单位、PLR 比例、预处理与输出反变换。预测必须使用 GA 的候选水温，不能始终使用某条历史水温；不能把目标功率或由它算出的 COP 当作预测时可获得的特征。

再跑短时段计划，核查主机功率、`Q_reject = Q_load + P_chiller`、泵流量及总电耗。自定义模型同时用于搜索和最终复核，避免新旧模型混用；这种复核证明的是代码和模型一致，不能证明模型本身准确。

优化器会寻找低功率区域，也可能利用训练数据稀疏处的预测误差。因此应限制验证域，扫描 PLR/水温曲线并检查边界，用留出工况验证所选策略。不要靠裁剪异常预测来隐藏外推问题。

```bash
python -m unittest discover -s tests -p test_model_interface.py -v
```

无可选包时运行公式及接口测试；安装对应包后，还会运行 sklearn/PyTorch 合成模型的加载测试。合成模型只验证接口，不代表真实模型精度。

## 7. 泵、塔和管网模型怎么替换

| 对象 | 当前求解 | 必须同步更新 |
| --- | --- | --- |
| 泵功率 | `PumpLookupTable.lookup` 枚举组合并计算三次方功率 | `pump_lookup.py` 候选功率、最优组合比较；`validate_schedule` 泵功率复核 |
| 泵流量/扬程与管网 | 按额定流量反算共用转速，扬程平方律，未解压力平衡 | 泵组合的流量求解、可行性、返回流量/扬程；复核与两侧热平衡 |
| 塔风机功率 | `Plant.tower_dispatch` 中三次方功率 | 塔组合比较；最终风机功率复核 |
| 塔热工模型 | `required_air_fraction` 反算风量，`merkel_approach` 正算逼近度 | 两个正反模型、塔调度与最终逼近度/散热能力复核 |

相关文件为 `power_models.py`、`pump_lookup.py`、`optimize_utils.py`。只改 `affinity_power` 不够，因为组合计算和最终复核还有对应公式。更换时可进一步抽出泵/塔模型对象，让求解与复核调用同一物理模型，同时分别保留约束检查。

若塔模型直接预测 `T_out = f(T_in, wet_bulb, water_flow, fan_speed, active_towers)`，应在每个组合内反求满足目标出塔温度的风机转速。已验证单调时可用括区间二分，非单调时搜索完整允许区间或新增风机转速决策，无可行根则拒绝组合。不要将预测逼近度硬裁剪到 3–8°C，也不要把旧模型“全开必为 3°C”的归一化假设强加给实测模型。

冷却流量依赖主机功率。如果新主机模型反过来还需要冷却流量，就构成耦合方程；应在候选点内迭代功率与流量并检查收敛，或将流量增加为决策变量，同步扩展特征、解码和热平衡。不能用历史流量冒充候选流量。

## 8. 动态模型、性能和可复现性

当前接口是无状态的单点推理。GA 会以不同顺序评估个体和设备组合，不允许在预测器内累积共享 LSTM 隐状态、在线训练或随机采样。需要历史窗口或动态状态时，在 `Plant.evaluate` 中对每条候选序列从同一初始历史独立展开，并让最终复核使用相同的状态展开规则；由未来控制影响的历史量必须随候选方案更新。

模型应在工厂中一次加载，避免每次预测加载权重、启动进程或远程请求。GPU 单点调用未必更快；长时域和大量设备可进一步做批量推理，但当前接口没有批处理支持。

记录模型文件版本/哈希、特征和预处理元数据、依赖版本、有效域与误差指标。GA 种子只能控制搜索，不能消除随机推理导致的不一致。

## 9. 模型不必跟随项目开源

真实权重放 `models/`，私有适配器放 `local_models/`，现场参数放 `config/local*.json`，训练数据放 `inputs/`；这些路径已由 `.gitignore` 排除，也不在公开源码包清单中。配置快照含本地路径与选项，应保留在 `outputs/`。

公开仓库只有虚构公式和适配示例。使用者在本地提供自己的模型、配置路径即可，不需要分享模型权重；默认程序继续只依赖 Python 标准库。
