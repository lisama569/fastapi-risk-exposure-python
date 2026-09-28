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
  - `REJECTED / LIMIT_EXCEEDED`：提交后预计敞口超账户级上限，事件不生效；
  - `REJECTED / GROUP_LIMIT_EXCEEDED`：账户级通过但账户组整体放不下，
    事件不生效；
  - `REJECTED / INVALID_EVENT`：字段非法（空 id、负 seq、非有限 delta）。
- 收敛后重放结论稳定：已接受的事件重放返回 `DUPLICATE`，
  被拒绝的事件重试仍返回相同的拒绝原因与拒绝层级。

## 账户组共享额度

- `PUT /groups/{group_id}/limit` 设置账户组整体上限；
  `PUT /groups/{group_id}/members/{account_id}` 把账户归入账户组
  （一个账户至多属于一个组，保证归属确定）。
- 层级规则：事件提交先过账户级上限，再过所属账户组的整体上限，
  任意一层放不下即拒绝；拒绝原因可直接区分是账户级
  （`LIMIT_EXCEEDED`）还是账户组级（`GROUP_LIMIT_EXCEEDED`）放不下。
- 账户组敞口 = 组内各账户当前敞口之和。多个账户并发向同一账户组
  上报时，校验与提交在同一把锁内串行化，组额度不会被突破；
  全部上报到达并稳定后，各请求的通过/拒绝结论与拒绝层级不再变化。
- `GET /groups/{group_id}/usage` 返回额度占用明细：逐账户占用、
  总占用 `used`、剩余 `remaining`；明细与逐笔上报的判定结论可对账
  （每个账户的占用等于其被接受事件的生效 delta 之和）。

## 事件更正（冲正）

- `POST /corrections`，请求体
  `{"correction_id", "target_event_id", "new_delta"?  , "cancel"?}`：
  - `cancel=true` 整笔撤销（生效 delta 视为 0）；
  - 否则用 `new_delta` 替换原始事件的 delta；二者互斥且必居其一。
- 识别与生效语义：
  - 更正按 `target_event_id` 引用原始上报，按 `correction_id` 幂等去重；
    同一份更正重复提交返回 `DUPLICATE`，不重复生效，也不会被当成新的
    敞口变动事件；
  - 每个目标事件至多被更正一次；
  - 最终敞口是 {事件集合, 更正集合} 的确定性函数：更正先到、后到或与
    原始上报交错到达，收敛（被拒更正重放）后账户层与账户组层口径一致；
  - 改大金额的更正同样做账户级 + 账户组级限额试算，超限即拒绝。

### 更正生效前后的限额判定边界（逐生效时点，而非最新总额）

更正是**追溯改写**历史事件的生效 delta，因此限额校验不只看更正后的
最新总敞口，而是按更正后的事件序列**逐生效时点重放**：

- 账户层：按 `(seq, event_id)` 排序重放该账户全部事件，每一个中间时点
  （含最新时点）的累计敞口都不得超过账户上限；
- 账户组层：把组内各账户的有序事件流按 `(seq, account_id, event_id)`
  确定性合并为一条全局时序，每个时点的组共享敞口（当时各账户累计之和）
  都不得超过组上限。

因此存在一种必须拒绝的情形：**某笔早期上报原本通过，随后的扣减把当前
敞口拉回低位，此时把早期上报改大到超限金额——即使更正后的最新总敞口
并未超限，只要它在任一历史生效时点曾超限，更正就必须拒绝**。否则历史
口径会被静默改成一次本来就过不了限额的状态。

- 边界取值：累计敞口**等于**上限视为合法（`>` 才拒绝），与事件上报口径
  一致；账户层检查先于组层，账户层放不下时直接返回账户级原因。
- 调小金额、整笔撤销、以及重放后每个时点都不超限的更正，走原有合法
  路径正常生效，不会被误伤。
- 拒绝层级与拒绝原因：
  - `LIMIT_EXCEEDED`：账户层某生效时点（`detail` 给出具体事件时点与
    当时敞口）放不下；
  - `GROUP_LIMIT_EXCEEDED`：账户层通过，但账户组在某生效时点共享敞口
    放不下（`detail` 给出当时触发的账户/事件时点）。

### 拒绝后的状态保证

限额试算是**只读**的：更正先在不落盘的临时覆盖上完成逐点重放，只有
两级校验全部通过后，才在同一把锁内登记更正。因此：

- 被拒更正不会留下任何痕迹：账户与账户组的金额、`GET /groups/{id}/usage`
  占用明细、原始事件集合以及既有的通过/拒绝结论全部不变；
- 不存在“先接受、后越过限额”的中间窗口，并发读到的口径始终合法；
- 被拒更正不是已登记更正：用同一 `correction_id` 重放得到的仍是同一
  拒绝原因（不会变成 `DUPLICATE`），换一条合法更正（新的
  `correction_id`）仍可正常提交；
- 其余拒绝口径不变：`UNKNOWN_TARGET_EVENT`（原始事件不存在）、
  `INVALID_CORRECTION`（字段非法、`correction_id` 内容冲突复用、
  目标已被更正过）均按原原因拒绝，重复更正只生效一次。

- 可区分的拒绝原因汇总（绝不静默忽略或当成新增事件）：
  - `UNKNOWN_TARGET_EVENT`：引用的原始事件不存在；
  - `INVALID_CORRECTION`：字段非法（空 id、cancel 与 new_delta 同时
    提供或同时缺失、金额非有限）、`correction_id` 被内容不同的更正
    复用（会让结论不再确定）、目标事件已被更正过；
  - `LIMIT_EXCEEDED` / `GROUP_LIMIT_EXCEEDED`：更正后任一生效时点
    越过对应层级上限（更正未登记，原口径不变，无需回滚）。

## 压力情景

- `POST /stress/snapshot`，请求体 `{"name": str, "shocks": {account_id: 冲击比例}}`。
- 重算规则：`stressed = max(0, base * (1 + shock))`，是基准敞口的纯函数，
  不改变基准状态。
- 快照同时给出账户层（`base_exposure` / `stressed_exposure`）与账户组层
  （`group_base_exposure` / `group_stressed_exposure`，组 = 组内成员之和）
  的结果，并附内容 SHA-256 哈希；相同情景 + 相同事件与更正序列必然得到
  相同快照（可复现），与事件/更正的到达顺序无关。
- 显式拒绝（HTTP 400，`reason` 可区分），绝不静默使用默认值：
  - `INVALID_SCENARIO`：名称为空、无冲击项、冲击值非有限或低于 -100%；
  - `MISSING_BASELINE`：冲击引用了没有基准敞口数据的账户。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/events` | 幂等上报事件并做两级限额校验（拒绝时返回 409） |
| `POST` | `/corrections` | 幂等提交更正/冲正（拒绝时返回 409） |
| `PUT` | `/limits/{account_id}` | 设置账户限额 |
| `PUT` | `/groups/{group_id}/limit` | 设置账户组整体限额 |
| `PUT` | `/groups/{group_id}/members/{account_id}` | 把账户归入账户组 |
| `GET` | `/groups/{group_id}/usage` | 查询账户组额度占用明细 |
| `GET` | `/exposure/{account_id}` | 查询当前敞口 |
| `POST` | `/stress/snapshot` | 情景重算并返回账户层+账户组层确定性快照 |

服务层（`app.service.RiskService`）与 HTTP 层共享同一套判定逻辑，
各入口结论一致。

## 与既有单账户能力的兼容范围

- 单账户限额、幂等上报、乱序收敛、非法情景拒绝的语义完全保留，
  既有用例不回归；
- 未设置账户组或未归属任何组的账户，行为与此前单账户口径一致
  （组级校验自动跳过，快照中组层字段为空表）；
- 账户敞口聚合规则不变，只是聚合时应用已生效更正后的“生效 delta”。

## 代码结构

- `app/models.py`：事件、更正、账户/账户组限额、情景与判定原因的数据模型；
- `app/store.py`：线程安全的事件与更正存储、“按序去重 + 生效 delta”敞口聚合，
  以及只读的逐生效时点路径试算（账户 `effective_path` / 账户组
  `group_effective_path`，支持不落盘的临时更正覆盖）；
- `app/limits.py`：锁内“校验 + 提交”的两级限额判定；更正按逐生效时点
  试算（账户级先于组级），通过后才登记，拒绝不留痕、无需回滚；
- `app/stress.py`：情景校验、账户层+账户组层重算与快照哈希；
- `app/service.py`：组合门面；`main.py`：FastAPI 适配层。

## 验证方法

测试使用标准库 `unittest`（无需额外依赖），日志会打印输入事件与判定依据：

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

只验证“历史时点超限”这一缺陷边界时可定向运行：

```bash
uv run python -m unittest tests.test_correction_history_breach -v
```

核心期望：早期 100（上限 100）通过、后期 -60 扣减后，把早期事件改大为
120 必须返回 `REJECTED / LIMIT_EXCEEDED` 且敞口保持 40；两账户共享组
上限 200 的同类场景返回 `REJECTED / GROUP_LIMIT_EXCEEDED` 且组金额与
占用明细保持不变。

覆盖范围：

- `tests/test_idempotency.py`：幂等重放、乱序到达，与参考实现比对；
- `tests/test_limits.py`：10 线程并发提交验证账户限额不被突破、收敛后
  结论稳定、拒绝原因可区分；
- `tests/test_groups.py`：多账户并发共享组额度不突破、账户级/组级拒绝
  层级可区分、额度占用明细与逐笔判定结论对账；
- `tests/test_corrections.py`：更正在先到/后到/交错/逆序下最终口径一致、
  重复更正不重复生效、未知目标/非法字段/冲突复用/二次更正被可区分拒绝、
  超限更正被拒且状态不留痕；
- `tests/test_correction_history_breach.py`：“更正后最新总额不超限、
  但账户层或账户组层历史生效时点曾超限”必须按层级拒绝；覆盖两账户共享
  组额度、跨账户交错叠加触发的组级超限、拒绝后金额/占用明细/既有结论
  不变、调小/撤销/边界值不误伤、交错与重复提交口径一致、并发下无
  “先接受后越限”中间状态；
- `tests/test_group_stress.py`：组合级快照可复现、相同事件与更正序列任意
  到达顺序得到相同哈希、快照与占用明细一致；
- `tests/test_stress.py`：快照可复现、非法情景与缺失基准被明确拒绝、
  重算不改变基准；
- `tests/test_http.py`：HTTP 入口（含账户组、更正、组合级快照）与服务层
  结论一致。
