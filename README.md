# 分层样本装配器（Python + SQLite）

从多个名单快照装配分层样本，处理资格核验后的失访/拒绝/撤回，按声明顺序补替补，
保留原抽样概率与每一步证据，并支持进程重启与多进程并发下的不重复占用。

## 目录

```
sample_assembler/          装配器实现（无第三方依赖，仅标准库）
  schema.py                SQLite schema 与关键唯一索引
  rules.py                 层规则编译、边界语义、冲突检测
  eligibility.py           资格事件状态机
  assembler.py             导入/装配/预览/发布/追溯/分页接口
  errors.py                领域错误
reference/
  reference_sim.py         独立参考模拟程序（不 import 装配器代码）
tests/
  fixtures.py              手工场景：两层名单、重复 BU、缺失层、拒绝、
                           逆序事件、跨版本映射、无法补齐层、规则冲突
  test_assembler.py        端到端断言 + 与参考程序逐项核对（65 项）
  test_concurrency.py      双进程交错装配测试
  _concurrent_worker.py    并发测试的子进程 worker
```

## 运行测试

```bash
python3 -I tests/test_assembler.py      # 65 项功能/证据/参考核对
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
