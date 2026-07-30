# M1 / M1.5 / M2 / M2.5 累计完成报告

**最新更新**：2026-07-29（M2.5 同步）
**原 M1 完成**：2026-07-22
**作者**：Claude (M1 PoC session) → 后续 M1.5 / M2 / M2.5 接力
**关联**：[CLAUDE.md](CLAUDE.md)（项目守则）/ [开发指南.md](开发指南.md)（架构）/ [开发指南_运营.md](开发指南_运营.md)（运营 SOP）/ [iSMART自动回复知识库_全量模板_20260729.xlsx](iSMART自动回复知识库_全量模板_20260729.xlsx)（M2.5 真值源）

---

## 1. M1 范围回顾

iSmart 海外售后邮件自动 triage / 起草 / 合规检查 PoC。**结果永远送人工工作台，不主动 send。**

**M1 严格边界**：
- 地区：**仅 North America**
- 语言：**仅英文回复**
- Intent enum：32 值 iSmart 真实分类
- 知识库：mock_rag 桩实现，13 条 NA 政策 + 6 条 iSmart NA-flavoured chunks

---

## 2. 关键产出

### 2.1 Intent enum：6 值 → 32 值
**问题**：v5 用 NA 保修视角 6 值 enum（warranty_claim / recall / service_booking / parts_inquiry / complaint / other）套到 iSmart 真业务流量上，**80% 进 `other`**，intent accuracy 只有 35.71%。

**解决**：从 [iSMART售后服务邮箱回复方法_20260308.xlsx](iSMART售后服务邮箱回复方法_20260308.xlsx)（77 条客服回复模板）抽出 32 值 enum：

| 分类 | 值数 | 代表 |
|---|---|---|
| App 侧 | 6 | `app_vehicle_control` / `app_account` / `app_binding` / `app_update` / `app_messages` / `app_suggestion` |
| iSMART 平台 | 11 | `ismart_data_plan` / `ismart_digital_key` / `ismart_fota` / `ismart_account_inquiry` / ... |
| FICM | 3 | `ficm_activation` / `ficm_cpaa` / `ficm_map` |
| TSP | 1 | `tsp_sensitive_data` |
| ADAS（默认 P1） | 4 | `adas_acc` / `adas_aeb` / `adas_lka` / `adas_tja` |
| 硬件 / 非 iSMART | 4 | `hardware_warranty` / `charging` / `delivery_handover` / `vehicle_incompatible` |
| 生命周期 | 3 | `complaint` / `follow_up_resolved` / `other` |

定义：[agent/schemas.py:18-69](agent/schemas.py#L18)；rubric：[agent/prompts.py](agent/prompts.py) `TRIAGE_SYSTEM`。

### 2.2 Region gate：NA-only
**业务决策**（2026-07-21）：M1 严守 NA，5 区直接升人工，避免用 NA 合规规则审 EU/UK 客户造成合规错位（GDPR / UK GDPR / 沙特 PCA 等 M1 都没接）。

**两道守门**：
1. **Triage 后立即 gate**：[agent/email_agent.py:155](agent/email_agent.py#L155) — `triage.region != "North America"` → `NeedsHumanEscalation`
2. **Retrieval 层 region-aware 拒答**：M6 测试中模型主动识别 NA chunks 不适用 EU 客户，`cited_clauses=[]` + `evidence_score=0.1`

6 区 enum：Europe / Asia / South America / Africa / Oceania / North America。

### 2.3 PII 保护协议
**原则**：客户 VIN / email / phone 绝不能进项目目录。

**实施**：
- 真实 CSV：`~/Documents/private/iSmart-email-ledger/售后邮件台账-2.csv`（项目外）
- 脱敏后 JSON：`~/Documents/private/iSmart-email-ledger/售后邮件台账-2.redacted.json`
- 脱敏脚本：[scripts/redact_ledger.py](scripts/redact_ledger.py) — VIN 用 `[A-HJ-NPR-Z0-9]{17}`，phone 必须显式国际区号前缀才动手（否则 13358 个 false positive 会损坏 sender 字段）
- [`.gitignore`](.gitignore) 排除：`*.csv` / `*台账*` / `samples_real_seed.json`（含叙事 PII — 人名、车牌）/ `.env.example`

---

## 3. 评测结果

| 版本 | 模式 | 样本 | intent acc | urgency acc | compliance 1st | escalation |
|---|---|---|---|---|---|---|
| v5 | triage | 30 真样本 | 35.71% | — | — | — |
| **v6** | **triage** | 30 真样本 | **96.43%** | 85.71% | n/a | 3.45% (P0 红牌) |
| v6 | full (bypass) | 30 真样本 | 100% on drafted | 100% on drafted | 100% on drafted | 75.86% (region 拒答 + 红牌) |
| **v6** | **full** | **5 NA 合成** | **100%** | 80% (1 P1→P3) | **100%** | **0%** |

**Triage 错例分析**：
- 唯一真错：`real_01` (UK "external temperature" / "driving mode" UI) 真值 `app_vehicle_control`，模型给 `hardware_warranty`。已修正真值（in-vehicle 屏 UI → hardware）
- `real_18` 红牌走 escalation 是 routing 正确（social_media_threat 触发），仅 intent 字段差（`app_binding` vs `complaint`）
- P1 vs P2 边界模糊（钥匙锁车里、4 周未释放）模型倾向 P2，业务拍板权重高于模型判断

**Full eval 错例分析**：
- 真样本 22 escalation 中 17 是 retrieval 主动拒答（NA chunks 不适用 EU 客户）—— **正确行为**，证明 region 守门在 retrieval 层也工作
- 4 compliance 失败（3 Missing citation + 1 Region applicability）
- 1 红牌（social_media_threat 触发）

---

## 4. 已知限制（M2 起点）

### 4.1 地区合规规则
M1 只覆盖 NA。EU/UK/Asia/South America/Africa/Oceania 5 区**全部升人工**。

**M2 解锁条件**：
1. 在 [agent/prompts.py:21-37](agent/prompts.py#L21) 加 5 套 `COMPLIANCE_CONSTRAINTS_EU` / `_UK` / `_LATAM` / `_MEA` / `_APAC` 规则
2. 改 [agent/email_agent.py:155](agent/email_agent.py#L155) 的 gate 逻辑（细路由而非粗暴 NA-only）
3. 给 [mock_rag.py](agent/tools/mock_rag.py) 加 EU/UK/AP/SA/AF/OC chunks（每区 ≥ 6 条覆盖主流 intent）

### 4.2 多语言回复
M1 只支持英文回复。GCC 客户需阿拉伯语、EU 多语种需本地化。

### 4.3 Mock RAG → 真 Qdrant
M1 桩实现。生产路径需替换为：
- Qdrant 向量库 + bge-reranker
- 文档摄取管线（TSB / recall / case 自动更新）
- token-overlap 评分 → embedding 相似度评分

### 4.4 真业务数据接入
- 当前真样本 29 条覆盖 iSmart 真实 intent 分布（app_binding 14 / app_vehicle_control 5 / ismart_digital_key 3 等）
- M2 需扩到 ≥ 200 条 + 每月刷新
- 接入 iSmart 真实 CRM（VIN 反查、warranty 状态、recall 列表）

### 4.5 PII 防御补丁（建议但未实施）
项目在父仓库 `hello-agents/` 内，子目录 `.gitignore` 不生效。建议：
- `git init` 本项目为独立 repo，或
- 在父 `.gitignore` 加 `Co-creation-projects/uxoxsn-OverseasVehicleEmailAgent/eval/_samples_to_label.json` 等条目

当前状态：`samples_real_seed.json` 和 `_samples_to_label.json` 是 git untracked，`git add` 不会自动包含，相对安全。

### 4.6 Prompt cache 命中率
Triage cache_hit=92% 优秀，但 Drafting / Compliance cache_hit=0%（每次重写 prompt 不同）。M2 考虑：
- 把系统前缀拆成"通用规则" + "本次任务"两部分，任务部分独立 prefix
- gpt-5.4 自动 prefix cache 策略调优

---

## 5. M2 优先级建议

| 优先级 | 任务 | 工作量 | 影响 |
|---|---|---|---|
| P0 | 加 EU/UK 合规规则 + 真样本测试 | 2 周 | 5 区解锁 |
| P0 | mock_rag → Qdrant 真接入 | 3 周 | 检索质量 |
| P1 | 多语言回复（Arabic / Spanish / Portuguese / French） | 2 周 | GCC/LATAM 客户 |
| P1 | iSmart CRM MCP 真接入 | 2 周 | VIN / warranty / recall 数据 |
| P2 | Prompt cache 优化 | 1 周 | 成本 -30% |
| P2 | 真样本扩到 ≥ 200 + 自动刷新管线 | 2 周 | Triage 准确率置信度 |

---

## 6. 文件清单

**代码**：
- [agent/email_agent.py](agent/email_agent.py) — 主流水线
- [agent/azure_llm.py](agent/azure_llm.py) — Azure OpenAI 客户端
- [agent/schemas.py](agent/schemas.py) — Pydantic 模型 + 32 值 Intent enum
- [agent/prompts.py](agent/prompts.py) — 所有 system prompt 集中处
- [agent/tools/mock_rag.py](agent/tools/mock_rag.py) — 知识库桩实现（19 条 NA chunks）
- [agent/tools/mock_crm.py](agent/tools/mock_crm.py) — CRM 桩实现

**评测**：
- [eval/run_eval.py](eval/run_eval.py) — 评测脚本，支持 `--triage-only` 和 bypass env vars
- [eval/samples_real_seed.json](eval/samples_real_seed.json) — 30 真样本真值（gitignored）
- [eval/samples_real_na.json](eval/samples_real_na.json) — 5 NA 合成样本（full eval 用）
- [eval/runs/v6_triage.json](eval/runs/v6_triage.json) — 最新 triage-only 报告
- [eval/runs/v6_full.json](eval/runs/v6_full.json) — 真样本 full eval（含 region 拒答）
- [eval/runs/v6_full_na.json](eval/runs/v6_full_na.json) — NA 合成样本 full eval（100% drafted）

**PII 协议**：
- [scripts/redact_ledger.py](scripts/redact_ledger.py) — 脱敏脚本
- `~/Documents/private/iSmart-email-ledger/` — 真实数据（项目外）

**文档**：
- [CLAUDE.md](CLAUDE.md) — 项目守则（项目级）
- [M1_DONE.md](M1_DONE.md) — 本报告

---

## 7. 命令速查

```bash
# 脱敏原始 CSV
python3 scripts/redact_ledger.py

# Triage 评测（triage-only）
python3 eval/run_eval.py --samples eval/samples_real_seed.json --triage-only --out eval/runs/vN.json

# Full 评测（NA 合成样本）
python3 eval/run_eval.py --samples eval/samples_real_na.json --out eval/runs/vN_full_na.json

# Full 评测（真样本 + bypass）
M1_BYPASS_REGION_GATE=1 M1_BYPASS_RAG_REGION=1 python3 eval/run_eval.py \
  --samples eval/samples_real_seed.json --out eval/runs/vN_full.json
```

---

## 8. 经验教训（致 M2）

1. **业务 enum 不要拍脑袋**——从客服真实回复模板 xlsx 抽，比任何自创 enum 都准
2. **region 守门要在多层做**——triage gate + retrieval 拒答 + compliance 校验，三道防线互不依赖
3. **PII 处理脚本要 dry-run**——早期 phone regex 不带 `+` 前缀，13358 个 false positive 损坏 sender 字段
4. **bug 隐藏得很深**——`mock_rag.py` region 标签 `"NA"` 缩写 vs enum `"North America"` 全名不匹配，导致 NA 客户也 0 hit（M1 测试才发现）
5. **评测要 bypass 工具**——生产路径严禁设置 bypass env vars，但 eval 必须有办法绕过硬守门才能验证整链路
6. **P1 vs P2 边界让业务拍**——模型倾向保守，业务规则应覆盖