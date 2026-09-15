# 全量考点结构模板

本文件只规定结构，不包含任何正式考点正文。

## 1. 结构化母本

```json
{
  "schema_version": "exam-points/v1",
  "status": "complete",
  "basis": {
    "asset_manifest_sha256": "64位SHA-256",
    "evidence_corpus_sha256": "64位SHA-256",
    "delivery_spec_sha256": "64位SHA-256",
    "structure_template_sha256": "64位SHA-256",
    "old_gpt_fusion_used": false,
    "automatic_point_generation_used": false,
    "authoring_method": "GPT逐资产直接阅读完整ASR+OCR证据并经文件级复核"
  },
  "model": "如实填写本轮GPT模型",
  "catalog_entry_count": 81,
  "asset_count": 91,
  "completed_asset_count": 91,
  "point_count": "全部资产考点数之和",
  "assets": [
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
      "points": [
        {
          "point_id": "A001-KP001",
          "dimension": ["流程顺序", "条件与例外"],
          "statement": "考点完整陈述",
          "question_angles": ["排序题", "情景单选题"],
          "answer_elements": ["必要答案要素1", "必要答案要素2"],
          "traps": ["证据支持的易混边界"],
          "evidence_refs": [
            {
              "modality": "ASR",
              "locator": "/raw_evidence/asr_result/result/segments/0",
              "support": "direct"
            },
            {
              "modality": "OCR",
              "locator": "/raw_evidence/ocr_result/frames/0/lines/0",
              "support": "corroborating"
            }
          ],
          "evidence_status": "ASR+OCR互证"
        }
      ],
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
  ]
}
```

## 2. 单资产 Word 排布

```text
目录序号与目录原文
资产序号、年份、文件名
覆盖的课程目录行（coverage_positions）
来源身份（asset_id / source_sha256 / evidence_sha256）
来源限制（如有）

考点 A001-KP001
考点类型：……
考点陈述：……
可考方式：……
标准答案要素：……
易错点：……
证据状态：……

考点 A001-KP002
……
```

`locator` 必须是可在正式 evidence 单资产记录中解析的 JSON Pointer，并指向相应的 `raw_evidence/asr_result` 或 `raw_evidence/ocr_result` 分区。证据定位同时保留在结构化母本、独立 Word 和 QA 中，不得丢失资产级 SHA 绑定。

## 3. 状态机

```text
门禁等待 → 已绑定正式证据 → 阅读中 → 初稿完成 → 文件级复核 → QA通过 → 已交付
```

任一步出现源 SHA、证据 SHA、目录映射或内容覆盖问题，状态退回“已绑定正式证据”或“阅读中”；禁止在失败状态下标记完成。

## 4. 进度口径

- 正式完成分母固定为 91 个资产，不按目录条数或考点条数替代。
- `初稿完成` 不计入最终完成；只有 `QA通过` 才计作完成资产。
- 同步报告：`已绑定证据 x/91`、`文件级复核 x/91`、`QA通过 x/91`、`累计考点 N`。
- 执行层另输出 20 格文本进度条及每个 `EP-BATCH-XX` 的资产数、QA 通过数和累计考点数，便于并行批次独立核对。
- 跨资产核对只在 `91/91` 单资产完成后开始，不反向阻塞主 Word。
