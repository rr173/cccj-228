# 分层样本装配器（Python + SQLite）

从多个名单快照装配分层样本，处理资格核验后的失访/拒绝/撤回，按声明顺序补替补，
保留原抽样概率与每一步证据，并支持进程重启与多进程并发下的不重复占用。
在单轮装配之上，支持跨轮次 panel 轮换：由已发布样本/计划与历史账本确定性地
生成下一轮成员清单与逐成员变更账本。

## 目录

```
sample_assembler/          装配器实现（无第三方依赖，仅标准库）
  schema.py                SQLite schema 与关键唯一索引（v3：含回答转移表）
  rules.py                 层规则编译、边界语义、冲突检测
  eligibility.py           资格事件状态机
  assembler.py             单轮导入/装配/预览/发布/追溯 + panel/回答转移委托
  panel.py                 跨轮次轮换：规则导入/计划生成/预览/发布/追溯
  answers.py               回答转移：端点绑定/修订录入/增量配对/汇总
  delivery.py              研究交付包：交付授权/分组求解/停领/领取
  errors.py                领域错误
reference/
  reference_sim.py         单轮装配独立参考模拟程序
  panel_reference_sim.py   panel 轮换独立参考模拟程序（不 import 装配器代码）
tests/
  fixtures.py              单轮手工场景
  panel_fixtures.py        三轮两层 panel 手工场景
  answer_fixtures.py       回答转移场景（四轮已发布计划链 + 离样回归）
  delivery_fixtures.py     研究交付包场景（三层四轮 + 跳轮离样 + 兄弟叶）
  test_assembler.py        单轮端到端断言 + 参考核对（65 项）
  test_panel.py            panel 端到端断言 + 逐轮参考核对（81 项）
  test_answer_transfer.py  回答转移表：配对/更正/冲突/重开（70 项）
  test_delivery.py         研究交付包：授权/分组/停领/幂等/交错
  test_concurrency.py      双进程交错装配测试
  _concurrent_worker.py    并发装配 worker
  _panel_worker.py         并发 panel 计划 worker
```

## 运行测试

```bash
python3 -I tests/test_assembler.py      # 65 项单轮功能/证据/参考核对
python3 -I tests/test_panel.py          # 81 项 panel 轮换/账本/参考核对
python3 -I tests/test_answer_transfer.py  # 70 项回答转移/增量更正/事务
python3 -I tests/test_delivery.py       # 95 项研究交付包/授权/分组/停领/交错
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

## 回答转移表（访谈修订增量维护）

分析员想按调查分层统计“同一人在持续在样期间，某道二值题从哪一轮、哪种答案
转到哪一轮、哪种答案”。本能力只读既有 panel 计划/账本与调查原始数据，
**不重新计算样本或轮换计划**。

### 初始绑定与链范围

```python
asm.init_answer_transfer(plan_id)          # 只接受已发布的端点计划
asm.submit_answer_revisions(revisions)     # 批量录入/更正访谈修订
asm.answer_transfer_summary()              # 直接读持久化汇总（不重跑配对）
asm.answer_transfer_binding()              # 当前绑定与已发布计划链
asm.list_answer_segments(panel_member_id=None)  # 区段与每轮有效答案（核对用）
asm.list_answer_pairs(panel_member_id=None)     # 内部配对明细（核对用）
asm.list_answer_batches()                  # 每批录入审计（受影响成员/区段）

# 研究交付包
asm.grant_delivery_authorization(plan_id, panel_member_id)
asm.revoke_delivery_authorization(plan_id, panel_member_id)
asm.list_delivery_authorizations(plan_id=None)      # 管理员核对
asm.generate_delivery_package(request_key, plan_ids, k, group_tree,
                              endpoint_plan_id=None)
asm.claim_delivery_package(package_id)              # 课题组领取
asm.list_delivery_packages()                        # 管理员核对
```

- 只沿端点计划 `base_plan_id` 回溯得到的**已发布计划链（含端点）**工作：
  不含最初的单轮抽样批次（`base_batch_id` / `published_version`）、不含同轮
  的其他计划（如备选 draft）、也不含端点之后发布的计划。
- 一个库只维护一份转移表：同一端点重复初始化幂等、不清空已有修订/配对/汇总；
  已绑定其他端点时拒绝换绑。

### 在样区段与层

- 成员身份取既有 `panel_member_id`；在样记录取账本 `decision` 为 `retained`
  或 `joined` 的轮次。沿链**连续出现**构成一个在样区段；缺席（该计划账本无该
  成员）或 `exited` 都切断区段，之后重新加入另起区段，**不跨区段配对**。
- 每次在样记录的层取**该计划账本行**的 `stratum_code`，不读成员当前层或锚定
  名单行。回归成员换层时按其新计划账本的层统计。

### 修订语义

`revision` 包含 `{plan_id, panel_member_id, revision, answer}`，`answer` 为
`0` / `1` / `None`（作废）。

- 只接受绑定链上的在样记录；其他计划、缺席/已退出轮次、端点后发布的计划均拒绝。
- 同一 (计划, 成员) 以**最大修订号**为准；没有答案或最新修订作废都不是有效
  端点，但**不切断在样区段**（配对可跨过它们接上）。
- 相同键同号同答案可重送（`duplicate`，不重复落库、不重推）；同号不同答案
  使**整批无效**，即使该修订已不是最新；批内顺序无关，晚到的旧修订只入库、
  不改变有效答案与配对。
- 坏输入、冲突、非链上键一律整批回滚：修订、有效答案、配对明细、汇总与审计
  行在同一 `BEGIN IMMEDIATE` 事务内提交，重开数据库看到完整已提交结果。

### 配对与汇总

转移汇总 = 每个在样区段内**相邻两次有效回答**的配对，可跨过仍在样但没有有效
答案的轮次。键为（起轮、止轮、起点记录的层、前答案、后答案），值为配对数；
没有配对的键不保留。汇总表 `answer_transfer_summary` 由分析员直接读取。

### 增量维护（只处理受影响区段）

- 每批录入只对“该批使有效答案实际变化”的成员重推其区段配对；其他成员的
  修订、区段、配对明细不读、不改。汇总按汇总键做旧值/新值差量更新，不重建整表。
- 审计行 `answer_revision_batch` / `..._item` 记录本批实际落库的键、受影响成员
  与区段数，可据此核对更正范围。

```python
# 绑定、录入、读取
plan_id = asm.list_panel_plans()[-1]["plan_id"]
asm.init_answer_transfer(plan_id)
asm.submit_answer_revisions([
    {"plan_id": plan_id, "panel_member_id": "PM_...", "revision": 1, "answer": 0},
    {"plan_id": plan_id, "panel_member_id": "PM_...", "revision": 2, "answer": None},
])
asm.answer_transfer_summary()
# [{'start_wave': 1, 'end_wave': 4, 'stratum_code': 'S',
#   'from_answer': 0, 'to_answer': 1, 'pair_count': 2}, ...]
```

### 测试与验证（tests/test_answer_transfer.py，70 项）

- 同一在样区段先只有首尾有效答案（1 条长配对），补入中间答案后变为 2 条短配对；
  作废中间答案后接回 1 条长配对。另一位成员贡献同一汇总键时，更正只减掉本人
  计数，验证不会误删别人的计数。
- 离样后回归的成员（BU-05，且回归后换层）证明不跨区段配对，层取自计划账本。
- 乱序修订、晚到旧修订、同批多处更改、批内/已存同号冲突、坏输入、非链上键
  均覆盖；每个汇总键由“计划链账本 + 输入修订”独立推导后与库内结果对照，
  进程重开后再次对照。
- 更正范围验证：用 `sqlite3` 的 `set_trace_callback` 捕获 SQL，断言涉及
  修订/区段/配对/有效答案表的语句只出现受影响成员；读取汇总不触碰这些表。
  另对无关成员的配对明细与有效答案做逐行快照对照。

## 研究交付包（跨轮次答卷出库）

外部课题组要研究同一批受访者在若干指定轮次的回答，但不能拿到内部调查库。
管理员选择轮次、登记交付授权，课题组最终领到可跨轮对照的答卷。本能力只读
既有 panel 计划/账本与回答转移表，**不改变**调查表、轮换规则与回答转移表
行为。

### 用法

```python
asm = SampleAssembler("var/survey.db")
# 1) 管理员按 (plan_id, panel_member_id) 登记授权（持久；未授予视为无权）
asm.grant_delivery_authorization(plan_id=4, panel_member_id="PM_...")
asm.revoke_delivery_authorization(plan_id=4, panel_member_id="PM_...")

# 2) 生成交付包：选定绑定链上的若干计划、最小人数 k>=2、唯一标签分组树
tree = {"label": "G", "children": [
    {"label": "L1", "stratum": "S1"},
    {"label": "L2", "stratum": "S2"}]}
pkg = asm.generate_delivery_package(
    request_key="STUDY-2026-Q4", plan_ids=[1, 4], k=2, group_tree=tree)

# 3) 课题组领取（只返回包标识、轮次顺序、每个成员一条记录）
claim = asm.claim_delivery_package(pkg["package_id"])
# {"package_id": "DP_...", "rounds": [1, 4],
#  "members": [{"code": "C...", "group": "L1", "answers": [0, 0]}, ...]}
```

### 语义要点

- **规格**：一次交付指定一个已绑定回答转移表的库、绑定链上**至少两个不同**
  的已发布 `plan_id`、`k >= 2`、唯一标签分组树（叶子显式对应层，一层一片叶）
  与请求键。计划按**链序**使用（输入顺序无关）；同轮其他计划不能替代。
- **候选**：在每个选定计划都有在样记录 + 有效回答 + 交付授权，并沿**同一
  在样区段**贯穿第一个到最后一个选定计划。中间未选为答卷列的轮次仍**不能
  离样**，但不要求有答案或授权。以**第一个选定计划**账本记录的层作为分组
  起点，不随后续改层漂移。
- **分组**：候选中有未映射的层即拒绝交付。选定节点收下其子树内**全部**
  合格成员且人数须达 `k`；同包选定节点构成反链（不能同时选节点与后代），
  不拆成员凑数；可整棵子树不交付。优化：先最大化交付人数，再最大化各成员
  公开组深度之和（根深度 0）；不输出空组，不用额外表披露更细分组。
- **停领**：任一已交付成员在某选定计划的授权**后来被撤回**，整个包永久
  停领（再次授予也不恢复；出库请用新请求键重算）。撤回与停领标记在同一
  `BEGIN IMMEDIATE` 事务内落库，领取也在同一事务内校验，故无可见中间状态；
  同键重试不能绕过停领。未交付成员、未选轮次的撤回不使包失效。不追回已
  下载副本。
- **幂等/不可变**：同键同规格（轮次乱序、树兄弟顺序不算更改）返回原包标识；
  同键改规格报错；答案后续修订不改变已生成答卷。生成、完整结果与可领取
  状态是同一条提交。代号包内稳定唯一、与原成员标识无关且不跨包关联。

### 测试与验证（tests/test_delivery.py，95 项）

- 两兄弟叶各 2 人、`k=2` 时分别按叶交付（组名 L1/L2），逐条核对交付成员
  与组名、答案按轮次顺序；跳轮选择时首尾在样但中间离样的成员被排除，中间
  未选轮次无答案/无授权不影响。
- 未映射层拒绝、同轮备选 draft 不能替代、链上非在样/未知成员不能登记授权、
  `k<2`、节点标签重复/一层多叶/叶缺 stratum 等规格校验（失败不留半个包）。
- 分组最优：人数相同取更深节点；`k=3` 时提升到父组；含 1 人子树时按“先
  最大化人数”归父组、不输出空组；反链与不拆人不重复。
- 撤回：已交付成员在选定计划撤回使旧包停领（再授予、同键重试均不恢复）；
  新键把剩余三人归到父组；未交付成员/未选轮次撤回不影响。
- 请求重试（乱序/兄弟顺序）、同键改规格报错、答案修订不改旧答卷、重开库
  后授权/停领/幂等持久、撤回再授权循环。
- 两连接交错（领取×撤回、生成×撤回）：成功结果都对应某个完整且仍可领取
  的版本，撤回提交后一律拒绝，无半撤回/缺员版本。

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

# 研究交付包（见上文「研究交付包」章节）
asm.grant_delivery_authorization(plan_id, panel_member_id)
asm.revoke_delivery_authorization(plan_id, panel_member_id)
asm.generate_delivery_package(request_key, plan_ids, k, group_tree)
asm.claim_delivery_package(package_id)
asm.list_delivery_packages()
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

### 研究交付包（tests/test_delivery.py，95 项）

- 三层（S1/S2/S3）四轮已发布计划链 + 同轮备选 draft；样本成员含跳轮离样、
  拒绝离样、冷却后以新名单行回归（同一 panel_member）。
- 两兄弟叶各 2 人、`k=2` 时分别按叶交付（L1/L2），逐条核对交付成员、组名与
  答案序列；跳轮选择时首尾在样但中间离样的成员被排除，中间未选轮次无答案/
  无授权不影响；S3 未映射时拒绝交付。
- 规格校验（同轮备选不能替代、链上非在样/未知成员不能登记授权、`k<2`、
  节点标签重复/一层多叶/叶缺 stratum、候选不足）失败均不留半个包。
- 分组最优：人数相同取更深节点；`k=3` 提升到父组；含 1 人子树时按“先最大
  化人数”归父组、不输出空组；反链且不拆人。
- 撤回：选定计划上的已交付成员撤回使旧包永久停领（再授予、同键重试均不恢
  复），新键把剩余三人归到父组；未交付成员、未选轮次的撤回不影响。
- 幂等：同键同规格（轮次乱序、树兄弟顺序）返回原包；同键改规格报错；答案
  修订不改变已生成答卷；重开后授权/停领/幂等持久。
- 两连接交错（领取×撤回、生成×撤回）：成功结果都对应某完整且仍可领取的
  版本，撤回提交后一律拒绝，无半撤回/缺员版本。
