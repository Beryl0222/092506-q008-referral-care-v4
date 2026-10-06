# 转诊协同与回执

面向“基层转诊协作”的服务端项目。围绕**转出 → 接收 → 补充材料 → 接诊 → 回转 → 闭环**
建立可恢复的转诊状态流；关键资料只保存脱敏摘要与访问记录，任何一方都看不到
超出授权范围的病历摘要；网络重试、接收拒绝、联系人变更与超时升级全部进入
可校验的审计操作链。

## 目录

- `src/referral_care/domain.py` 状态机、脱敏规则、字段可见性矩阵、哈希链。
- `src/referral_care/store.py` SQLite 持久化：转诊单、事件链、幂等操作、访问记录、链接令牌、通知箱。
- `src/referral_care/service.py` 应用服务：生命周期动作、超时扫描、角色视图、通知投递。
- `src/referral_care/api.py` 进程内 JSON 请求适配层。
- `tests/` 领域边界与验收测试（状态流、可见性、并发幂等、超时、重启）。

## 状态流

```
pending_accept ──accept──▶ accepted ──request_supplement──▶ awaiting_supplement
     │                       │                                    │ supplement
     │                       └──admit──┐                          ▼
     │ reject▶ rejected          supplemented ──admit──▶ admitted
     │                                                       │ return_back
     │ withdraw▶ withdrawn                                   ▼
     │                                                    returned ──confirm_return──▶ closed
     └──超时: reminder_sent → timeout_escalated → auto_returned
```

- 同一患者对同一机构、同一目的只允许一个进行中的转诊（部分唯一索引保证）；
  不同机构或不同目的的转诊可并存，终态后自动释放。
- 接收方超过承诺时限未回应：先自动提醒（`reminder_sent`），仍超时则升级
  自动退回原机构（`timeout_escalated` + `auto_returned`），链接同步失效。
- 进入任一终态即递增 `token_version` 并吊销全部链接令牌，旧链接立即失效。

## 隐私与审计

- 摘要、补充材料、回转小结入库前统一脱敏（手机号/身份证/卡号/邮箱）。
- 字段可见性：转出方全量；接收方看不到内部备注，撤回/超时退回后敏感字段
  一并收回；平台管理方只看流转元数据；无关第三方拒绝并留痕。
- 每次查看（含被拒绝的尝试）写入 `access_log`。
- 全部关键操作追加到哈希链事件日志，`verify_chain` 可检出篡改。
- 通知经 outbox 投递，失败重试的每一次尝试都记录 `notification_retry`。

## 接口

`api.handle(payload, service)` 接收 JSON。既有 `health`/`register` 保持原样；
转诊动作统一返回 `{"ok": bool, "result"|"error": ...}`：

`create_referral` `accept` `reject` `request_supplement` `supplement` `admit`
`return_back` `confirm_return` `withdraw` `change_contact` `tick` `view_case`
`issue_link` `open_link` `list_events` `list_access` `verify_chain` `flush_outbox`

所有变更动作要求幂等操作号 `op_id`：重复回执返回首次结果并记录
`duplicate_ignored`，不会重复改状态。

## 运行

运行测试：`PYTHONPATH=src python3 -m unittest discover -s tests`

检查源码：`python3 -m compileall src`

项目只使用 Python 标准库，测试和运行不需要启动其他服务。
