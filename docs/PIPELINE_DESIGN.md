# 求助流水线设计（2026-10-19，Opus 设计，供实现参考）

## 用户规则（原话归纳）

- **积分 < 500**：求助只能一条一条提交（不批量）；可以同时有多条在"等待应助"。
  但**一旦有任意 1 条收到应助**，必须先把它处理完（采纳或拒绝），才能继续提交新求助。
- **积分 ≥ 500**：可以批量提交。用户未给出额外限制，本设计对 ≥500 不设"处理完才能继续"的闸门，但仍按串行浏览器逐条执行。
- 积分未知（从未签到/同步到余额）一律按 < 500 处理（保守）。

## 条目阶段 `stage`（只对 source=="website" 或已导入的本人条目；demo 不参与）

| stage | 含义 | 判定（按优先级） |
|---|---|---|
| attention | 出错/状态不认识，需人工 | status=="attention"，或 website_status 不在已知映射 |
| downloading | 正在下载 | status=="downloading" |
| done | 已采纳/已完结/已关闭 | adopt_action.status=="confirmed"，或 website_status ∈ DONE |
| awaiting_decision | 已下载核对，等采纳/拒绝 ——**闸门条目** | 有 pdf（且未被拒绝归档） |
| fulfilled | 已有应助文件，待下载 ——**闸门条目** | website_status ∈ FULFILLED |
| submitted | 已发布，等待应助 | website_status ∈ WAITING，或 publish_action.status=="confirmed" |
| queued | 本地导入、未发布 | 其余（无 assist_url） |

状态字映射集中放在 `pipeline.py` 常量里：
`DONE = ("已完结","已采纳","已关闭","关闭")`；`WAITING`、`FULFILLED` 先放候选值，并标注 **未经真实页面核实**（目前真实记录只见到"已完结"）。不认识的状态 → attention，绝不猜。

拒绝确认后（reject_action.status=="confirmed" 且求助未完结）：条目回到 submitted，原 pdf/verification/download_file_url 移入 `rejected_files` 列表保留（**不删文件**），以便下载下一条应助。

## 闸门

```
gate_blocked = tier=="single" and any(stage in {fulfilled, downloading, awaiting_decision})
```

闸门只拦"提交新求助"，不拦下载/核对/处理已到的应助（这些正是解锁闸门的步骤）。

## 下一步规划（纯函数 `next_step(items, account, prefs) -> Step`）

优先级：
1. 任一 attention → `wait_user`（显示原因，流水线暂停）
2. 有 downloading → `wait`（浏览器忙）
3. 有 awaiting_decision →
   - 自动拒绝模式且 rejection_decision 为 auto_reject → `reject_auto`
   - 自动采纳开启且 verification 状态 identity_match 且 bibliography.status=="match" → `adopt`
   - 否则 `wait_user`（"请采纳或拒绝《…》后流水线继续"）
4. 有 fulfilled → `download`（最早的一条）
5. 有 submitted 且距上次同步 ≥ interval → `sync`
6. 有 queued 且闸门未锁 → `publish`（tier=="single" 一次一条；"batch" 也逐条，但不等应助）
   - auto_publish 关闭 → `prepare`（填表停在发布前，交给用户）→ `wait_user`
7. 否则 `idle`

Step 结构：`{action, item_id, label, requires_user: bool, reason}`。

## 执行

- `Pipeline` 调度线程：enabled 时每 30 秒 tick 一次；agent.busy 时跳过；只执行 requires_user==False 的步骤；每步通过 `agent.enqueue` 进入既有串行执行器。
- 任一步骤失败 → 条目进 attention，流水线自然暂停（规则 1）。
- 切换账户 → 流水线自动关闭。重启 → enabled 恢复为 False（需用户重新开启），阶段由持久字段重新推导。
- 积分：沿用 account_metrics[active].points；sync 时顺便解析首页余额（若可得）。

## 新浏览器动作（适配器均**未经真实页面验收**）

- `publish(item_id)`：在 prepare 基础上点击发布；发布前再检查一次闸门与积分档位；结果不确定记 uncertain、不重试。仅在用户开启 auto_publish 时由流水线调用。
- `adopt(item_id)`：仅对 awaiting_decision、当前账户、文件 sha256 未变的条目；在对应应助文件条目卡片内唯一定位"采纳"控件；确认文字出现才记 confirmed，否则 uncertain 不重试。手动按钮需 confirmed=true。

## 偏好与接口

偏好：`pipeline_enabled`、`pipeline_auto_publish`(默认 False)、`pipeline_auto_adopt`(默认 False)、`pipeline_sync_minutes`(默认 10，范围 5–120)、`pipeline_rules_version`。开启 auto_publish / auto_adopt 需提交当前 `PIPELINE_RULES_VERSION`。

- `GET /api/state` 增加：每个 item 的 `stage`；顶层 `pipeline = {enabled, auto_publish, auto_adopt, sync_minutes, rules_version, points, tier, gate:{blocked, item_id, reason}, next_step, last_tick}`。
- `POST /api/pipeline`：更新上述偏好（布尔/整数校验）。
- `POST /api/pipeline/step`：手动执行一次 next_step（requires_user 时返回 400 并给原因）。
- `POST /api/browser` 允许 `publish`、`adopt`；adopt 需 `confirmed: true`。MCP 不新增开启这些偏好的工具。

## 不变的约束

真实发布/采纳/拒绝/下载/高速/AI 在用户通知闲时之前不实测；测试全部隔离（临时 data_root + 假 agent/假页面）。

## 积分 500 档的预测判定（batch-guard）

- 悬赏在发布时即扣除，所以下一条能否按“≥500 连续提交”处理，看的是**发布后的预测积分** = 当前积分 − 该条悬赏（`request_reward`，缺省取默认悬赏偏好）。预测值 < 500、或积分未知/过期（None）→ 按逐条规则：只要存在已应助未处理的求助，闸门即锁。`pipeline.tier(points, reward)` / `gate(items, points, reward)` 实现；计划文案为“发布后积分将低于 500，按逐条求助规则处理”。
- 积分新鲜度：每次发布确认成功后，存储的积分减去已确认悬赏；`BrowserAgent.publish` 在 `/assist/create` 页面已读到实时余额（不额外加载页面），若原先记录为 batch 档而实时余额使预测值 < 500，则抛 `PreconditionError`、不提交，积分已更新，由流水线重新规划。
- 站点自身拒绝：提示含“需先处理应助”类措辞（`publish_policy` 失败类型 `gate`，不暂停流水线、不进 attention、条目保持 queued）→ 账户积分标记为需刷新（置 None）并写入 `site_gate` 时间；`next_step` 在 `site_gate` 之后尚无更新的同步前不再发布（有待同步条目则先 sync，否则 wait_user），用户处理应助并同步后恢复。
- 批量页 `/assist/create-batch` 仍未实现；真实站点措辞与扣分时机待实测核对。
