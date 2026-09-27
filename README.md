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

## 两层限额：账户级 + 账户组级

- `PUT /limits/{account_id}` 设置账户级敞口上限 `max_exposure`；
- `PUT /groups/{group_id}/limit` 设置账户组（组合）共享上限；
- `PUT /accounts/{account_id}/group` 把账户归入账户组（一个账户至多属于
  一个组，重复设置即迁移）。

事件提交时在同一把共享锁内完成“校验 + 提交”，并发请求被串行化，
两层额度都不会被突破。判定顺序与拒绝层级：

1. 非法事件 → `REJECTED / INVALID_EVENT`；
2. 重复 `event_id` → `DUPLICATE`（不改状态）；
3. 账户层放不下 → `REJECTED / LIMIT_EXCEEDED`，`level=ACCOUNT`；
4. 账户组层放不下 → `REJECTED / GROUP_LIMIT_EXCEEDED`，`level=GROUP`；
5. 否则 `ACCEPTED`。

账户层先判、组层后判，因此响应里的 `reason` + `level` 可以一眼区分是哪
一层放不下。所有限额校验都基于**更正后生效视图**（见下节）：已撤销事件
不计、已更正金额按新值计。收敛后重放结论稳定：已接受的事件重放返回
`DUPLICATE`，被拒绝的重试仍返回相同原因与层级。

未入组、或所在组未设限额的账户，行为与旧版单账户限额完全一致（向后兼容）。

## 事件更正（冲正）

`POST /corrections` 对一笔**已生效**的原始上报做更正：

| 字段 | 含义 |
| --- | --- |
| `correction_id` | 更正自身的幂等标识 |
| `original_event_id` | 被更正的原始上报 `event_id` |
| `new_delta` | 更正后的金额（与 `cancel` 二选一） |
| `cancel` | 整笔撤销 |

识别与生效语义：

- 更正通过 `original_event_id` 引用原始上报；引用不存在（或原始上报
  此前被限额拒绝、从未生效）→ `REJECTED / ORIGINAL_NOT_FOUND`，
  **可在原始上报到达后重试**，因此更正先到、后到或与原始上报交错到达，
  收敛后的账户层与账户组层口径一致；
- 同一 `correction_id` 重复提交 → `DUPLICATE`，不重复生效，也绝不会被
  当成一笔新的敞口变动（更正不产生新事件）；
- 一笔原始上报至多被一份更正生效，第二份不同 `correction_id` 的更正 →
  `REJECTED / CORRECTION_CONFLICT`；
- 字段非法（空标识、`new_delta` 非有限、金额与撤销并存）→
  `REJECTED / INVALID_CORRECTION`；
- 更正生效前会重放校验两层限额：账户层要求更正后**每一步前缀敞口**都
  不超过账户上限（更正早期事件可能整体抬高后续前缀），组层要求更正后
  组总额不超过组上限；放不下则拒绝（`LIMIT_EXCEEDED` /
  `GROUP_LIMIT_EXCEEDED`，带层级），保证已给出的通过/拒绝结论不再改变。

以上拒绝全部显式返回，绝不悄悄忽略或按新增事件处理。

## 压力情景（账户层 + 组合层）

- `POST /stress/snapshot`，请求体
  `{"name": str, "shocks": {account_id: 冲击}, "group_shocks": {group_id: 冲击}}`
  （`group_shocks` 可省略）。
- 重算规则：`stressed = max(0, base * (1 + shock))`，是基准敞口的纯函数，
  不改变基准状态；基准一律取更正后生效视图。
- 快照包含账户层基准/冲击后敞口，带组合冲击时另含
  `group_base_exposure` / `group_stressed_exposure`（组基准 = 成员基准
  之和），以及全量内容的 SHA-256 哈希。**相同情景 + 相同事件与更正序列
  必然得到相同快照**（可复现），与到达顺序无关。
- 显式拒绝（HTTP 400，`reason` 可区分），绝不静默使用默认值：
  - `INVALID_SCENARIO`：名称为空、无冲击项、冲击值非有限或低于 -100%；
  - `MISSING_BASELINE`：冲击引用了没有基准数据的账户或账户组。
- 不带 `group_shocks` 时快照字段与旧版完全一致（向后兼容）。

## 账户组额度占用明细

`GET /groups/{group_id}/occupancy` 返回：

```json
{
  "group_id": "G",
  "max_exposure": 150.0,
  "per_account": {"A": 60.0, "B": 40.0},
  "used": 100.0,
  "remaining": 50.0
}
```

`per_account` 为各成员当前生效占用（含更正结果），`used` 为其合计，
`remaining = max_exposure - used`。这些明细与逐笔上报的判定结论严格
对得上：占用 = 各账户已接受事件（经更正）之和，被组层拒绝的事件不计入。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/events` | 幂等上报事件并做两层限额校验（拒绝时返回 409） |
| `POST` | `/corrections` | 提交更正（冲正），幂等生效（拒绝时返回 409） |
| `PUT` | `/limits/{account_id}` | 设置账户限额 |
| `PUT` | `/groups/{group_id}/limit` | 设置账户组共享限额 |
| `PUT` | `/accounts/{account_id}/group` | 设置账户所属账户组 |
| `GET` | `/exposure/{account_id}` | 查询当前敞口（更正后生效视图） |
| `GET` | `/groups/{group_id}/occupancy` | 查询账户组额度占用明细 |
| `POST` | `/stress/snapshot` | 情景重算并返回确定性快照 |

服务层（`app.service.RiskService`）与 HTTP 层共享同一套判定逻辑，
各入口结论一致。

## 代码结构

- `app/models.py`：事件、更正、限额（账户/组）、情景与判定原因的数据模型；
- `app/store.py`：线程安全的事件存储与“按序去重”确定性聚合（支持更正视图）；
- `app/groups.py`：账户组注册表（归属、组限额、成员枚举）；
- `app/limits.py`：共享锁内“校验 + 提交”的两层限额判定与更正稳定性校验；
- `app/corrections.py`：更正的识别、幂等、冲突检测与确定性生效视图；
- `app/stress.py`：账户层 + 组合层情景校验、重算与快照哈希；
- `app/service.py`：组合门面；`main.py`：FastAPI 适配层。

## 验证方法

测试使用标准库 `unittest`（无需额外依赖），日志会打印输入事件与判定依据：

```bash
uv sync
uv run python -m unittest discover -s tests -v
```

覆盖范围：

- `tests/test_idempotency.py`：幂等重放、乱序到达，与参考实现比对；
- `tests/test_limits.py`：单账户并发提交限额不突破、收敛后结论稳定、
  拒绝原因可区分；
- `tests/test_groups.py`：账户组共享额度在 10 线程并发下不突破、
  账户层/组层拒绝层级可区分、占用明细与逐笔结论一致；
- `tests/test_corrections.py`：更正在全部到达顺序下收敛口径一致、
  重复更正幂等不重复生效、四类拒绝原因可区分、更正后两层限额重新成立；
- `tests/test_group_stress.py`：组合级情景快照可复现（含不同更正到达
  顺序下哈希唯一）、占用明细一致性、非法组合情景拒绝、旧版快照兼容；
- `tests/test_stress.py`：账户级快照可复现、非法情景与缺失基准拒绝、
  重算不改变基准；
- `tests/test_http.py`：HTTP 入口与服务层结论一致（含组限额与更正）。
