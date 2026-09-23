# fastapi-risk-exposure

本地内存状态模拟的风险敞口聚合、限额校验与压力情景重算服务。

- 事件幂等上报：重复投递/乱序到达/多次提交后，敞口与按序去重结果一致，
  不出现负敞口、不重复扣减。
- 限额并发提交：账户级锁串行化，限额不被突破，结论稳定，拒绝原因可区分。
- 压力情景重算：相同情景 + 相同事件序列 ⇒ 相同快照；非法参数与缺失基准
  明确拒绝，不静默使用默认值。

## 快速开始

```bash
uv sync
.venv/bin/python -m unittest discover -s tests -v   # 跑测试（含判定依据日志）
.venv/bin/python -m uvicorn main:app --reload      # 启动 HTTP 服务
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts/{account_id}` | 建立账户基准与限额 |
| POST | `/events` | 幂等上报敞口变动事件 |
| POST | `/limits/check` | 限额校验与额度占用（幂等） |
| POST | `/scenarios` | 注册压力情景（折扣率/情景限额） |
| GET | `/scenarios/{scenario_id}/accounts/{account_id}` | 压力重算快照 |

详细事件模型、限额/情景规则、幂等与并发语义见 [`docs/design.md`](docs/design.md)。

## 调用示例

```bash
curl -s -XPOST localhost:8000/accounts/a1 -H 'content-type: application/json' \
  -d '{"limit":"1000"}'
curl -s -XPOST localhost:8000/events -H 'content-type: application/json' \
  -d '{"event_id":"e1","account_id":"a1","seq":1,"type":"INCREASE","amount":"100","asset_class":"CASH"}'
# 重复投递同一事件：duplicate=true，敞口不变
curl -s -XPOST localhost:8000/events -H 'content-type: application/json' \
  -d '{"event_id":"e1","account_id":"a1","seq":1,"type":"INCREASE","amount":"100","asset_class":"CASH"}'
curl -s -XPOST localhost:8000/scenarios -H 'content-type: application/json' \
  -d '{"scenario_id":"crash","haircuts":{"CASH":"0.5"},"scenario_limit":"80"}'
curl -s localhost:8000/scenarios/crash/accounts/a1
```
