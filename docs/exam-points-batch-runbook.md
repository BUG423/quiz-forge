# 全量考点：正式证据批次执行模板

本文件规定正式 `evidence_corpus.json` 到达后的执行方式，不包含考点正文。

## 1. 开始前检查

每次领取一个批次前必须确认：

- 正式证据顶层完整、`file_count = 91`，不是 `.incomplete.json`。
- 本批每项的 `asset_id / source_sha256 / evidence_sha256` 三重绑定有效。
- 资产顺序来自 `docs/exam-points-progress.md`，没有跨批重复或遗漏。
- ASR 证据模型为小米 MiMo `mimo-v2.5-asr`；本地审计文字不在证据正文中。
- 输入中不含旧 GPT 融合稿或已有考点稿。

任何一项失败，只暂停相应资产；其余已通过门禁的资产可继续，不等待主 Word。

## 2. 批次任务头

```text
批次编号：EP-BATCH-XX
资产范围：A___ 至 A___（以具体任务清单为准）
任务数：N
输入证据 SHA：逐资产记录，不使用批次级模糊绑定
输出目录：.work/first-principles-2026/exam-points/results/EP-BATCH-XX/
任务性质：GPT 直接理解与撰写；禁止代码自动生成考点正文
完成口径：逐资产文件级复核与 QA 均通过
```

批次只用于分配工作，不改变 81 条课程目录和 91 个资产的最终顺序。按证据体量平衡批次时，仍需保留全局 `order`。

## 3. 单资产模型任务模板

```text
你正在从考官视角处理一个课程资产。请完整阅读该资产的全部 ASR 和 OCR 证据，提取所有可能出题的知识点。此任务不是摘要：不得因为内容看似次要而省略任何可形成判断、单选、多选、填空、排序、匹配、情景或操作题的细节。

身份：
- order: {order}
- batch_id: {batch_id}
- catalog_position: {catalog_position}
- catalog_suborder: {catalog_suborder}
- catalog_entry: {catalog_entry}
- coverage_positions: {coverage_positions}
- asset_id: {asset_id}
- source_name: {source_name}
- source_sha256: {source_sha256}
- evidence_sha256: {evidence_sha256}
- task_sha256: {task_sha256}

必须逐项检查：定义概念；数字日期比例单位；专名；主体职责；制度条款；范围条件例外；流程顺序；风险禁令；案例事实与结论；系统菜单字段按钮；对比辨析与易错点；画面/附件独有信息；否定词和强调词。

每条考点必须是完整、可检索、可独立理解的陈述，并列出可考角度、标准答案要素、证据支持的易错点、稳定证据定位和证据状态。ASR/OCR 冲突时标记冲突，不编造裁决。不得读取旧 GPT 融合稿，不得用程序、关键词规则或机械拼接生成正文。

超长证据可以分段阅读，但完成分段阅读后必须做一次文件级最终通读：检查所有数字、专名、条件、例外、否定、顺序、按钮和字段，随后一次性提交本资产的正式 points。

输出严格遵循 docs/exam-points-structure-template.md 的单资产对象结构。
```

## 4. 单资产结果文件模板

文件名建议为 `{order:04d}-{source_sha256前16位}.json`，内容为：

```json
{
  "order": 1,
  "batch_id": "EP-BATCH-01",
  "catalog_position": 1,
  "catalog_suborder": 1,
  "catalog_entry": "目录原文",
  "coverage_positions": [1],
  "asset_id": "year/kind/source_sha256",
  "source_name": "原文件名",
  "source_sha256": "64位SHA-256",
  "evidence_sha256": "64位SHA-256",
  "task_sha256": "64位SHA-256",
  "model": "如实填写本轮GPT模型",
  "source_limitations": [],
  "dimension_review": {
    "concept_definition": "reviewed|not_applicable",
    "number_date_unit": "reviewed|not_applicable",
    "proper_noun": "reviewed|not_applicable",
    "subject_responsibility": "reviewed|not_applicable",
    "rule_scope": "reviewed|not_applicable",
    "process_sequence": "reviewed|not_applicable",
    "risk_prohibition": "reviewed|not_applicable",
    "case_fact_conclusion": "reviewed|not_applicable",
    "system_ui_operation": "reviewed|not_applicable",
    "comparison_confusion": "reviewed|not_applicable",
    "visual_only_information": "reviewed|not_applicable",
    "emphasis_and_negation": "reviewed|not_applicable"
  },
  "points": [],
  "asset_review": {
    "full_evidence_read": true,
    "file_level_final_review": true,
    "numbers_rechecked": true,
    "names_rechecked": true,
    "conditions_negations_rechecked": true,
    "process_order_rechecked": true,
    "ui_fields_rechecked": true,
    "unresolved_conflicts": []
  }
}
```

`points` 只能由 GPT 直接理解证据后撰写。机械合并程序只验证身份、字段、顺序、唯一性和非空，不编辑或补写任何 `statement / question_angles / answer_elements / traps`。

## 5. 文件级复核提示

正式提交单资产前，GPT 必须按以下顺序反查：

1. 全部 ASR 段中出现的阿拉伯数字、中文数字、百分号、日期、单位、序号和型号是否进入考点。
2. 全部 OCR 页/帧中的标题、表格、流程箭头、系统字段、按钮、状态和注释是否进入考点。
3. “不、不得、禁止、严禁、除外、仅、至少、首先、之前、之后、同时”等边界词是否与对应事实一起保留。
4. 人名、部门、机构、制度、工程、系统、品牌和产品名称是否保留原称谓。
5. 流程是否保持原顺序，条件与例外是否没有被拆丢。
6. 案例是否同时包含行为、原因、处置、结果、责任和启示中证据实际提供的部分。
7. ASR 与 OCR 是否存在无法消解的冲突；冲突是否被如实标记而非静默改写。
8. 是否存在“略、等等、其他、详见原文、主要包括”后未展开的内容；如有必须回读补齐。

## 6. 批次交接格式

```text
批次：EP-BATCH-XX
任务数：N
已绑定正式证据：N/N
已完整阅读：N/N
已完成文件级复核：N/N
QA通过：N/N
累计考点：K
证据冲突资产：列出 order + asset_id
来源缺口资产：列出 order + asset_id
未完成原因：逐资产列出；不得用旧稿或自动抽取兜底
```

批次完成后更新 `docs/exam-points-progress.md`。只有 `QA通过` 才计入正式完成数。

## 7. 91/91 后的全局复核

- 同名不同 SHA 的资产分别比较，记录版本差异，不互相覆盖。
- 同一目录下视频、课件、图片、PDF 和 DOCX 的相同事实可建立交叉索引，但不删除逐资产考点。
- 检查制度名称、数字、日期、比例、流程和职责在不同资产间是否存在冲突。
- 建立跨课程易混辨析和综合情景题视角，所有条目必须回指资产及证据。
- 全局复核通过后，才机械生成独立 Word 和结构化母本；两者不得写入主 Word。

## 8. 执行命令

正式证据就绪后按以下顺序运行；这些命令不生成考点正文：

```bash
.venv/bin/python scripts/exam_points_pipeline.py prepare
.venv/bin/python scripts/exam_points_pipeline.py progress
.venv/bin/python scripts/exam_points_pipeline.py merge
.venv/bin/python scripts/exam_points_pipeline.py export
.venv/bin/python scripts/exam_points_pipeline.py qa
```

`prepare` 在正式证据缺失、状态不完整、存在 blocker、不是 `authoritative_evidence`、ASR 不是经验证的小米 MiMo 或文件名含 `incomplete` 时，一律在创建任务目录前拒绝。GPT 逐资产撰写发生在 `prepare` 与 `progress/merge` 之间，结果可按 `EP-BATCH-XX` 子目录并行保存；`merge` 只验证和原样搬运正文。
