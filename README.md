# 分层样本装配器（Python + SQLite）

从多个名单快照装配分层样本，处理资格核验后的失访/拒绝/撤回，按声明顺序补替补，
保留原抽样概率与每一步证据，并支持进程重启与多进程并发下的不重复占用。
在单轮装配之上，支持跨轮次 panel 轮换：由已发布样本/计划与历史账本确定性地
生成下一轮成员清单与逐成员变更账本。

## 目录

```
sample_assembler/          装配器实现（无第三方依赖，仅标准库）
  schema.py                SQLite schema 与关键唯一索引（v2：含 panel 表）
  rules.py                 层规则编译、边界语义、冲突检测
  eligibility.py           资格事件状态机
  assembler.py             单轮导入/装配/预览/发布/追溯 + panel 接口委托
  panel.py                 跨轮次轮换：规则导入/计划生成/预览/发布/追溯
  errors.py                领域错误
reference/
  reference_sim.py         单轮装配独立参考模拟程序
  panel_reference_sim.py   panel 轮换独立参考模拟程序（不 import 装配器代码）
tests/
  fixtures.py              单轮手工场景
  panel_fixtures.py        三轮两层 panel 手工场景
  test_assembler.py        单轮端到端断言 + 参考核对（65 项）
  test_panel.py            panel 端到端断言 + 逐轮参考核对（81 项）
  test_concurrency.py      双进程交错装配测试
  _concurrent_worker.py    并发装配 worker
  _panel_worker.py         并发 panel 计划 worker
```

## 运行测试

```bash
python3 -I tests/test_assembler.py      # 65 项单轮功能/证据/参考核对
python3 -I tests/test_panel.py          # 81 项 panel 轮换/账本/参考核对
python3 -I tests/test_concurrency.py    # 两个装配进程交错，验证无双重分配
```

`-I` 隔离模式保证不会从当前目录加载第三方同名模块。

## 核心语义

### 名单与身份

- `frame_unit` 以稳定 `frame_id` 为主键，带 `frame_version`、层字段、
  `contactable`、`random_rank`、`inclusion_prob`。
- 同一业务单位在不同版本中是不同行；只有 `identity_map` 中声明的
  `business_unit_id` 才把它们关联起来（可由单位自带 BU 字段自动登记，
  也可在导入时显式传 `identity_mappings`）。
- 替补池对已使用的业务单位去重——跨版本映射命中的候选按
  `business_unit_reused` 排除并留痕。

### 层规则（装配前拒绝）

- 分类层：值集合映射，重复值跨层即冲突。
- 数值层：区间统一左闭右开 `[lower, upper)`；区间/层间重叠即冲突，
  相邻边界（`[0,10)` 与 `[10,20)`）合法。
- 缺失层：用 `field_type="missing"` 与 `missing_stratum` 显式声明；
  层字段缺失的单位进入该层，未声明缺失层则无法归类、装配被拒绝。
- `target_n > capacity`、样本数量与目标不符、名单单位超容量、
  单位无法归入任何层，全部在装配前抛异常，不产生批次。

### 资格事件状态机

状态：`pending → eligible / refused / lost`，`eligible → withdrew`（及
refused/lost）；`refused/lost/withdrew` 为终态。

- 事件按 `(event_time, source_seq)` 定序。
- 幂等：相同 `event_id` 或相同自然键（frame+type+time+seq）重复提交
  返回 `duplicate`，不重复落库、不改状态。
- 逆序事件（序早于已确认最新事件）落库但 `accepted=0`，
  `reject_reason='out_of_order'`，不能静默覆盖较新状态。
- 终态后的非法转移同样落库标记 `illegal_transition`。
- 失访/拒绝/撤回均产生缺口；原始抽中记录保留为 `terminal_fail`，不删除。

### 装配与替补链

- 替补只填同层缺口；候选池按 `(random_rank, frame_id)` 排序。
- 排除条件（每个缺口都在 `gap.considered_json` 留证）：
  `not_contactable`、`terminal_state:*`、`used_in_batch`、
  `occupied_prior_batch`、`business_unit_reused`、`wrong_version`。
- 每次替补生成**新的 assignment 身份**，引用父缺口
  （`parent_gap_id`）、链根 `root_sample_id`、`chain_seq` 与触发事件；
  原抽样概率 `inclusion_prob` 原样保留。
- 无可用候选时缺口标记 `unfillable` 并记录原因；不存在静默 `open` 缺口。
- 批次满足“所有层达到目标 **或** 每个缺口都明确 filled/unfillable”才可发布；
  发布是单条 `published_version` 行的原子 upsert，失败/未发布批次
  不改变上一个版本。

### 持久化与并发（不重复占用的三层保障）

1. 所有状态落 SQLite（WAL 模式），进程退出重开后历史占用仍在；
2. 部分唯一索引
   `ux_assignment_substitute_frame ON assignment(frame_id) WHERE kind='substitute'`
   在数据库层保证同一 frame 全局至多被一次替补占用（跨进程生效）；
3. 写事务使用 `BEGIN IMMEDIATE` + `busy_timeout`，撞索引时整批回滚并在
   最新数据上重试（最多 25 次），因此两个进程同时选中同一候选时，
   只有一个能提交，另一个改取后续候选，绝不双重分配。

原始抽中记录（`kind='original'`）不受该唯一索引限制，允许失败批次
回滚后重建；批次内另有 `UNIQUE(batch_id, frame_id)` 防同批重复。

## panel 轮换（跨轮次）

在单轮装配之上提供“下一轮 panel 计划”：交付一份可发布的下一轮成员清单
和逐成员变更账本。只负责跨轮次轮换，不重实现单轮替补链。

### 轮换规则（声明式）

```python
{
  "name": "rot1",
  "frame_rule_set": "rules_v1",      # 新名单归类沿用的层规则集（name 或 id）
  "max_consecutive_waves": 2,        # 最大连续访问轮数（>=1）
  "cooldown_waves": 1,               # 退出后的冷却轮数（>=0）
  "min_retention_ratio": 0.5,        # 本轮可留存的最低比例 [0,1]
  "strata": [{"code": "S1", "target_n": 2}, ...]   # 目标层与每层目标数
}
```

### 轮换语义

- **基数**：首轮从当前已发布样本批次（`published_version`）出发；后续轮
  只从上一个**已发布**计划与历史账本（`panel_change`）继续，绝不只读
  当前样本表。上一轮 assignment 不是下一轮的自动入选。
- **留存候选**：上一轮仍合格（非 refused/lost/withdrew）、未处于冷却、
  连续轮数未达上限的成员，按稳定排序 `(random_rank, frame_id)` 产生；
  已拒绝/失访/撤回的成员带原因退出（`exited_refused` 等），连续达上限
  退出记 `max_consecutive_reached`。
- **冷却**：退出成员的业务身份在 `cooldown_waves` 轮内不得作为新候选
  回归（排除原因 `cooldown_active`）；冷却到期后可回归，且沿用同一
  `panel_member` 身份（成员锚定 business_unit_id，缺省 `frame:<id>`）。
- **新候选**：只填同层缺口；排除不可联系、终态、历史已占用
  （`occupied_prior_plan`）、本计划已占用、业务身份冲突
  （`business_unit_conflict`）、冷却中候选；每个候选的判定留证。
- **账本**：每个成员每轮一条 `panel_change`（wave、来源 assignment、
  连续轮数、冷却计数、决定原因），只 INSERT 不覆盖历史轮次；
  每层一条 `panel_stratum_gap`（目标/留存/新加入/缺口/最低留存要求/
  不可补齐原因/候选证据）。
- **留存比例**：每层留存数低于 `ceil(min_retention_ratio * 上轮在样数)`
  时明确记录 `retention_below_minimum`。

### 计划生命周期与幂等

- 计算结果先落 `draft`（可重试）；`request_key` 唯一，相同请求键重复
  计算直接返回已有计划——不产生第二个计划、不重复占用候选，进程重开
  后同样成立。
- 发布门：所有层达到目标或明确记录不可补齐，且 wave 未过期（过期
  draft 发布被拒绝）；发布是单语句状态推进，失败/中断不改变上一份
  已发布计划。
- 占用兜底：`ux_panel_change_plan_frame`（计划内一候选只属一个成员）
  与 `ux_panel_change_joined_frame`（全局一候选至多被新加入占用一次）
  两个部分唯一索引 + `BEGIN IMMEDIATE` 整批重试，跨进程不双重分配。

### 接口

```python
asm.import_rotation_rules(spec)                 # 导入轮换规则
asm.generate_next_plan(request_key, frame_version,
                       rotation_rule=None, note=None)  # 生成下一轮计划（draft）
asm.preview_plan_changes(plan_id)               # 只读预览逐成员变更
asm.publish_plan(plan_id)                       # 发布（失败不影响已发布）
asm.trace_panel_member(identifier)              # panel_member / BU / frame 追溯
asm.list_panel_plans(); asm.current_panel()     # 计划列表 / 当前在样 panel
```

## 接口一览

```python
from sample_assembler import SampleAssembler

asm = SampleAssembler("var/survey.db")

asm.import_frame_snapshot(version, units, identity_mappings=None,
                          replace_version=False)
rule_set_id = asm.compile_and_store_rules(rule_spec)
asm.import_draws(draws, rule_set_id=None)          # 数量/目标/概率校验
asm.record_event(event)                             # 幂等，逆序/非法拒绝留痕

result = asm.assemble(frame_version, rule_set_id=None, note=None)
asm.preview_substitutes(batch_id)                  # 不写入的实时候选排序
asm.publish_batch(batch_id)                        # 发布门：无 open 缺口
asm.current_sample()                               # 当前已发布版本在样
asm.list_gaps(batch_id=None)                       # 缺口与候选证据
asm.list_frame(version, stratum_code=None,         # keyset 分页（大名单）
               page_size=500, after_frame_id=None)
asm.trace_unit(identifier)                         # frame_id / BU / sample_id
asm.list_batches()
```

`trace_unit` 返回单位的名单行、身份映射、全部资格事件（含被拒事件）、
抽中记录，以及按 `root_sample_id` 组织的完整替补链（assignment + 缺口 +
每一步候选判定）。

## 测试场景覆盖

- 两层（分类 + 数值）+ 明确缺失层；规则冲突（区间重叠、分类值重复、
  容量不足、缺失层未声明）装配前拒绝；
- 抽中后拒绝、失访、合格、撤回；重复事件幂等；逆序事件不覆盖新状态；
  终态后非法转移拒绝；
- 同层两个缺口竞争候选：先缺口占用排序最前者，后缺口的证据中
  该候选标记 `used_in_batch`；
- 跨版本 BU 映射（单位自带 + 显式映射两种登记方式）的候选排除；
- 无法补齐层（候选全部不可联系/已占用）明确 unfillable 并可发布；
- 进程重开重装配：历史替补不被重复占用，池子耗尽后全部 unfillable；
- 独立参考程序逐单位核对状态、在样集合、替补集合、缺口状态、
  候选排除原因、被拒事件原因；
- 双进程（及 4 进程压力）交错装配：替补集合互不相交，
  数据库唯一索引零重复，候选耗尽后新批次全部 unfillable。

### panel 轮换（tests/test_panel.py，81 项）

- 三轮两层手工场景：连续达上限退出、退出后冷却与到期回归（同一
  panel_member 身份）、资格状态变化退出、留存比例不足记录、
  候选与历史业务单位冲突、层无法补齐并明确记录原因；
- 独立参考模拟器逐轮核对：panel_member、留存/退出/新加入决定、
  连续轮数、冷却计数、缺口原因、候选判定证据、变更账本；
- 相同请求键重试（含进程重开后）幂等，不产生第二个计划、不重复占用；
  备选 draft 不重复占用主计划候选；stale/篡改计划发布被拒绝且上一份
  已发布计划不变；账本跨轮次只增不改；
- 双进程相同请求键并发：只产生一个计划，候选零重复占用。
