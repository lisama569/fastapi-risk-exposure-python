# fastapi-risk-exposure-python

基于 FastAPI 的风险敞口聚合与限额校验服务。全部状态保存在本地内存，
仅依赖仓库已有依赖（fastapi[standard]）与标准库。

## 事件模型

敞口变动以事件形式上报（`POST /events`）：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 全局唯一事件标识，幂等去重的依据 |
| `account_id` | 账户标识 |
| `seq` | 账户内单调序号，乱序到达时用于重排 |
| `delta` | 敞口变动量（正增负减） |

账户敞口定义为：**按 `(seq, event_id)` 排序、按 `event_id` 去重后**的事件
序列逐步累加 `delta` 的结果，任何中间值为负时截断为 0。因此：

- 重复投递：同一 `event_id` 只生效一次，不会重复扣减；
- 乱序到达：最终敞口与按序去重后的参考结果一致；
- 敞口不会出现负数。

## 限额规则

- `PUT /limits/{account_id}` 设置账户级敞口上限 `max_exposure`。
- 事件提交时在同一把锁内完成“校验 + 提交”：并发请求被串行化，
  限额不会被突破。
- 判定结论（`Decision`）与可区分原因（`RejectReason`）：
  - `ACCEPTED / NONE`：已接受；
  - `DUPLICATE / NONE`：重复事件，幂等忽略，不改状态；
  - `REJECTED / LIMIT_EXCEEDED`：提交后预计敞口超限，事件不生效；
  - `REJECTED / INVALID_EVENT`：字段非法（空 id、负 seq、非有限 delta）。
- 收敛后重放结论稳定：已接受的事件重放返回 `DUPLICATE`，
  被拒绝的事件重试仍返回相同的拒绝原因。

## 压力情景

- `POST /stress/snapshot`，请求体 `{"name": str, "shocks": {account_id: 冲击比例}}`。
- 重算规则：`stressed = max(0, base * (1 + shock))`，是基准敞口的纯函数，
  不改变基准状态。
- 快照包含基准敞口、冲击后敞口与内容 SHA-256 哈希；相同情景 + 相同事件
  序列必然得到相同快照（可复现）。
- 显式拒绝（HTTP 400，`reason` 可区分），绝不静默使用默认值：
  - `INVALID_SCENARIO`：名称为空、无冲击项、冲击值非有限或低于 -100%；
  - `MISSING_BASELINE`：冲击引用了没有基准敞口数据的账户。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/events` | 幂等上报事件并做限额校验（拒绝时返回 409） |
| `PUT` | `/limits/{account_id}` | 设置账户限额 |
| `GET` | `/exposure/{account_id}` | 查询当前敞口 |
| `POST` | `/stress/snapshot` | 情景重算并返回确定性快照 |

服务层（`app.service.RiskService`）与 HTTP 层共享同一套判定逻辑，
各入口结论一致。

## 代码结构

- `app/models.py`：事件、限额、情景与判定原因的数据模型；
- `app/store.py`：线程安全的事件存储与“按序去重”敞口聚合；
- `app/limits.py`：锁内“校验 + 提交”的限额判定；
- `app/stress.py`：情景校验、重算与快照哈希；
- `app/service.py`：组合门面；`main.py`：FastAPI 适配层。

## 验证方法

测试使用标准库 `unittest`（无需额外依赖），日志会打印输入事件与判定依据：

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

覆盖范围：

- `tests/test_idempotency.py`：幂等重放、乱序到达，与参考实现比对；
- `tests/test_limits.py`：10 线程并发提交验证限额不被突破、收敛后
  结论稳定、拒绝原因可区分；
- `tests/test_stress.py`：快照可复现、非法情景与缺失基准被明确拒绝、
  重算不改变基准；
- `tests/test_http.py`：HTTP 入口与服务层结论一致。
