# Quiz Forge · 离线刷题应用架构

一个基于 React、TypeScript、Vite、IndexedDB 和 Capacitor 的离线刷题应用，同时支持 PWA 与 Android 原生壳。

> 本仓库不包含任何真实业务题库、题干、选项、答案、解析、题库名称或题库构建产物，仅内置几道小学加减法题用于演示软件流程。

## 运行

```bash
npm install
npm run dev
```

打开终端提示的本地地址即可。未提供私有题库时，应用加载“小学加减法”演示题，用户可在设置中导入本地 `.xlsx` 文件形成自己的题库。

生产构建：

```bash
npm run build
npm run preview
```

## 功能

- 按题库、题型、难度和重点标记筛选，支持随机或顺序组卷
- 支持单选题、多选题、判断题、填空题和案例题
- 保存练习进度、答案状态、错题与收藏
- 提供模拟考试和统一交卷评分
- 支持导入、覆盖、复制和删除浏览器本地题库
- 支持下载固定格式的 Excel 导入模板
- 浏览器端数据保存在 IndexedDB 中

## Excel 格式

第一行使用以下列名：

`题型`、`难易度`、`知识类别`、`题干`、`A` 至 `I`、`答案`、`出自规范`、`是否保命题`、`备注(填空题多个填空答案，请用英文逗号隔开)`

其中 `题型`、`难易度`、`知识类别`、`题干`、`答案` 为必填列。选择题答案写选项字母，判断题写 `正确` 或 `错误`。案例材料可单独放在一行无选项、无答案的“案例题”中，后续连续案例小题会自动关联该材料。

## 数据隔离

下载的 Excel 模板固定列名、文本格式、筛选和下拉选项，并附带几道小学加减法示例题。真实题库及其派生产物只应保存在本地。根目录 `.gitignore` 已排除：

- `public/data/` 中生成的题库 JSON 与版本清单
- Excel、压缩包、文档和常见题库截图格式
- `dist/`、Android Web 资源副本、APK/AAB 与 `release/`
- 环境变量、签名文件和常见服务配置凭据

如需在本地生成内置题库数据：

```bash
npm run import:source -- /absolute/path/to/question-bank.zip
```

该命令会在被 Git 忽略的 `public/data/` 下生成私有数据。随后执行 Android 同步时，私有数据会被复制到同样被忽略的 Android Web 资源目录。不要强制添加这些文件，也不要上传构建产物。

提交前建议确认：

```bash
git status --short --ignored
git ls-files
```

## 目录结构

- `src/components`：页面与交互组件
- `src/services/excelImporter.ts`：Excel 解析、校验和空白模板生成
- `src/services/database.ts`：IndexedDB 数据仓库与可选内置数据迁移
- `src/demoDataset.ts`：不涉及业务内容的小学加减法演示题
- `src/utils/answers.ts`：统一判题规则
- `scripts/import-source.mjs`：把本地私有压缩包标准化为本地 JSON 数据
- `android`：Capacitor Android 工程

用户导入的题库只保存在当前浏览器或应用的 IndexedDB 中，不会由本项目代码上传到服务器。
