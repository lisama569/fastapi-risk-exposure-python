# 风险敞口聚合与限额校验 — 设计说明

本地内存状态模拟账户敞口与压力情景，仅依赖 FastAPI/Pydantic 与 Python 标准库。
金额全部使用 `Decimal`（JSON 中以字符串传输），杜绝浮点误差导致的结论不一致。

## 1. 事件模型

`Event`（`POST /events`）：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 全局唯一事件 ID，**幂等去重键** |
| `account_id` | 账户 ID（账户级并发粒度） |
| `seq` | 单调逻辑序号（≥0），与 `event_id` 共同构成规范顺序 `(seq, event_id)` |
| `type` | `INCREASE`（增加敞口）/ `DECREASE`（释放敞口） |
| `amount` | 非负、有限的十进制金额；方向由 `type` 决定 |
| `asset_class` | 资产类别（情景折扣与分资产敞口用），默认 `DEFAULT` |
| `timestamp` | 可选上报时间，仅用于展示，不参与排序 |

### 1.1 幂等与保序语义

- 所有成功通过金额校验的事件都会进入账户的**已见事件台账**（包括到达时
  暂时无法生效的 `DECREASE`），按 `(seq, event_id)` 保序存放。
- 每次上报都对**全部已见事件**执行一次保序贪心重放：
  - `INCREASE` 总是生效；
  - `DECREASE` 仅当释放后总敞口不会为负时生效，否则**暂时跳过**
    （不产生负敞口），并以 `NEGATIVE_EXPOSURE` 作为本次判定原因。
- 因此：
  - **重复投递**：相同 `event_id` + 相同载荷，不重复扣减，响应中
    `duplicate=true`，返回该事件在当前收敛状态下的判定；
  - **乱序到达**：无论事件以什么顺序到达，重放都按规范顺序进行。
    在先的 `INCREASE` 后补到达后，之前被跳过的 `DECREASE` 自动生效，
    最终敞口与“按序去重”严格一致；
  - **同一事件多次提交**：幂等键保证只入账一次；载荷冲突（相同 ID
    不同金额/序号/方向/账户/资产类别）以 `DUPLICATE_CONFLICT` 拒绝，
    状态不变。
- 负敞口防护是序列级不变量：收敛后的总敞口恒 ≥ 0。

## 2. 限额规则

- `POST /accounts/{account_id}` 建立账户基准与限额；未配置限额的账户
  提交限额请求会被拒绝（`ACCOUNT_NOT_FOUND`，detail 区分“无基准台账”
  与“未配置限额”），**绝不静默使用默认限额**。
- `POST /limits/check`：请求体 `{request_id, account_id, amount}`，
  语义为“在当前敞口上占用 `amount` 额度”。
- 判定：`exposure_before + amount > limit` 即拒绝，原因 `LIMIT_EXCEEDED`，
  detail 给出当前敞口、申请额、投影值、限额与剩余额度；拒绝不占用任何额度。
- `request_id` 幂等：重复提交返回首次结论。

### 2.1 并发语义

- 每个账户一把可重入锁；事件上报、限额提交、情景重算都在账户锁内完成
  “读取—判定—写入”，临界区串行化。
- 因此并发提交下：
  - 限额**永不被突破**（所有通过请求的金额之和 ≤ 限额，最终敞口 ≤ 限额）；
  - 收敛后每个请求的通过/拒绝结论稳定、可复现（由 `request_id` 固化）；
  - 拒绝原因可区分：`LIMIT_EXCEEDED` / `INVALID_AMOUNT` /
    `ACCOUNT_NOT_FOUND`。

## 3. 压力情景规则

- `POST /scenarios` 注册 `ScenarioSpec`：
  - `haircuts`：**非空**的 `{资产类别: 折扣率}` 映射，折扣率必须是
    `[0, 1]` 内的有限 `Decimal`；
  - `scenario_limit`（可选）：非负有限值；
  - 任一参数非法 → 422 `INVALID_SCENARIO`，明确拒绝。
- `GET /scenarios/{scenario_id}/accounts/{account_id}` 执行重算：
  1. 情景不存在 → `SCENARIO_NOT_FOUND`；
  2. 账户无基准数据 → `MISSING_BASELINE`；
  3. 对已见事件做保序贪心重放，取**生效事件**覆盖到的资产类别；
  4. 任一资产类别在 `haircuts` 中缺失 → `UNCLASSIFIED_ASSET`
     （不静默套用默认折扣）；
  5. `stressed_exposure = Σ 分资产收敛敞口 × 折扣率`；
  6. 配置了 `scenario_limit` 时比较并给出 `within_limit`，超限原因
     `SCENARIO_LIMIT_EXCEEDED`（HTTP 仍返回 200，快照中带原因）。
- **可复现性**：重算是纯函数 `(规范有序事件序列, 情景定义) -> 快照`，
  与到达顺序、调用次数无关；相同情景与相同事件序列必然得到相同的
  `baseline_exposure / stressed_exposure / per_asset / state_version`。
  每次结果同时留存为快照（`ScenarioEngine.last_snapshot`）。

## 4. 拒绝原因一览

| reason | 触发场景 |
| --- | --- |
| `INVALID_AMOUNT` | 事件/限额申请金额为负或非有限值；建户限额非法 |
| `DUPLICATE_CONFLICT` | 相同 `event_id` 载荷冲突 |
| `NEGATIVE_EXPOSURE` | 保序重放中 `DECREASE` 超出在先 `INCREASE`，暂不生效 |
| `LIMIT_EXCEEDED` | 限额提交投影值超过账户限额 |
| `SCENARIO_LIMIT_EXCEEDED` | 压力后敞口超过情景限额 |
| `ACCOUNT_NOT_FOUND` | 缺账户基准或未配置限额 |
| `SCENARIO_NOT_FOUND` | 情景未注册 |
| `INVALID_SCENARIO` | 情景参数非法（HTTP 422） |
| `UNCLASSIFIED_ASSET` | 事件资产类别缺少情景折扣率 |
| `MISSING_BASELINE` | 情景重算时账户基准缺失 |

每个结论都带 `detail`（人类可读的判定依据）、`state_version`
（账户状态版本，仅在敞口实际变化时递增，便于判断重复投递是否产生副作用）。

## 5. 模块划分

- `app/models.py`：Pydantic 类型与枚举。
- `app/store.py`：线程安全内存状态（账户锁、幂等台账、保序事件序列、情景表）。
- `app/service.py`：`greedy_replay` 保序贪心重放、事件幂等上报、限额并发提交。
- `app/scenarios.py`：情景参数校验、压力重算与快照。
- `app/server.py` / `main.py`：FastAPI 入口。
- `tests/`：标准库 `unittest`，用例日志打印输入事件与判定依据。

## 6. 验证方法

```bash
uv sync
# 全量单测（日志中可见每个用例的输入与判定依据）
.venv/bin/python -m unittest discover -s tests -v
# 启动服务
.venv/bin/python -m uvicorn main:app --reload
```

覆盖场景：
- `tests/test_idempotency.py`：重复投递、乱序收敛、负敞口防护、载荷冲突、
  释放先于增加到达后的收敛；
- `tests/test_concurrency.py`：多线程并发限额提交（限额不突破、通过/拒绝
  数量精确、重放结论稳定）、拒绝原因区分；
- `tests/test_scenarios.py`：重算可复现、与喂入顺序无关、情景限额突破、
  缺折扣率/缺基准/未知情景/非法参数；
- `tests/test_api.py`：HTTP 幂等、HTTP 并发、422 错误体与领域结论一致性。
