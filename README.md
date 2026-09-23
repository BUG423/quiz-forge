<div align="center">
  <img src="./public/icons/icon-192.png" width="112" height="112" alt="Quiz Forge 图标" />
  <h1>🛡️ Quiz Forge · 安知刷题</h1>
  <p><strong>内置专业题库、可离线使用、也支持自行导入 Excel 的刷题应用</strong></p>
  <p>
    <img alt="Release" src="https://img.shields.io/github/v/release/BUG423/quiz-forge?display_name=tag&style=flat-square" />
    <img alt="Build" src="https://img.shields.io/github/actions/workflow/status/BUG423/quiz-forge/release.yml?style=flat-square&label=release" />
    <img alt="React" src="https://img.shields.io/badge/React-TypeScript-149ECA?style=flat-square&logo=react&logoColor=white" />
    <img alt="PWA" src="https://img.shields.io/badge/PWA-offline-5A0FC8?style=flat-square&logo=pwa&logoColor=white" />
    <img alt="Android" src="https://img.shields.io/badge/Android-Capacitor-3DDC84?style=flat-square&logo=android&logoColor=white" />
  </p>
  <p>
    <a href="https://github.com/BUG423/quiz-forge/releases/latest">📦 下载发布版</a> ·
    <a href="#-快速开始">🚀 快速开始</a> ·
    <a href="#-excel-导入格式">📊 Excel 格式</a> ·
    <a href="#-内置题库与本地数据">📚 内置题库</a>
  </p>
</div>

---

## ✨ 项目简介

Quiz Forge 是一个基于 **React + TypeScript + Vite + IndexedDB + Capacitor** 的离线刷题应用，同时提供浏览器/PWA 与 Android 版本。

公开发布版内置 **7 个专业题库、2183 道题**，安装后无需导入即可刷题。题库内容也包含在本仓库的 `public/data/` 中；用户自己的答题记录和后续导入的 Excel 仍保存在当前设备。

## 📦 下载与使用

前往 [GitHub Releases](https://github.com/BUG423/quiz-forge/releases/latest) 下载：

| 文件 | 用途 |
|---|---|
| 📱 `quiz-forge-android-*.apk` | Android 签名安装包 |
| 🌐 `quiz-forge-pwa-*.zip` | 可部署到静态网站的 PWA/Web 包 |
| 📊 `quiz-forge-import-template.xlsx` | 固定 17 列的 Excel 导入模板 |
| 🔎 `SHA256SUMS.txt` | 发布文件完整性校验值 |

安装或部署后即可选择内置题库。需要添加其他题库时，在“开始练习”卡片中打开设置，点击 **“导入 Excel 文件”**。

## 🌟 主要功能

| 模块 | 能力 |
|---|---|
| 🎯 灵活练习 | 按题库、题型、难度和保命题筛选，支持随机或顺序组卷，并记住上次选择 |
| 📝 多种题型 | 单选、多选、判断、填空和案例题 |
| 🧪 模拟考试 | 按所选题库、题型和数量组卷，统一交卷、自动评分 |
| ⭐ 学习记录 | 按题库管理错题，支持单题移除和清空；保留收藏与未完成练习 |
| 📥 本地导入 | 多个 `.xlsx` 文件批量导入，同名题库可新增或覆盖 |
| 📱 多端使用 | 浏览器、可安装 PWA、Android 原生壳 |
| 📴 离线可用 | 内置题库随安装包提供，题库和进度保存在当前设备的 IndexedDB 中 |

## 🧭 数据流程

```mermaid
flowchart LR
    A[📊 用户 Excel] --> B[🔍 格式校验与解析]
    B --> C[(💾 本地 IndexedDB)]
    G[📚 内置题库] --> C
    C --> D[🎯 练习与考试]
    D --> E[⭐ 错题 / 收藏 / 进度]
    C -. 作答进度不上传 .-> F[🔒 进度留在设备]
```

## 🚀 快速开始

```bash
git clone git@github.com:BUG423/quiz-forge.git
cd quiz-forge
npm install
npm run dev
```

打开终端提示的地址即可。内置题库由仓库中的 `public/data/` 提供。

### 🏗️ 生产构建

```bash
npm run test
npm run build
npm run preview
```

### 📱 Android 构建

```bash
npm run android:sync
cd android
./gradlew assembleRelease
```

本仓库的 GitHub Actions 会使用仓库 Secrets 中的签名密钥生成可安装的 Release APK，密钥不会进入 Git 历史。

## 📊 Excel 导入格式

推荐使用下列 17 列模板；导入器也会在工作表前 30 行中自动寻找表头，因此标题行位于第 2 行或更后面时无需手工调整：

| 列 | 字段 | 必填 | 说明 |
|---:|---|:---:|---|
| 1 | 题型 | ✅ | 单选题、多选题、判断题、填空题或案例题 |
| 2 | 难易度 | ✅ | 可使用模板下拉选项 |
| 3 | 知识类别 | ✅ | 用户自定义分类 |
| 4 | 题干 | ✅ | 题目正文 |
| 5–13 | A–I | 按题型 | 选择题选项 |
| 14 | 答案 | ✅ | 单选写 `A`，多选写 `AB`，判断写 `正确/错误` |
| 15 | 出自规范 | — | 可选来源说明 |
| 16 | 是否保命题 | — | `是/否` |
| 17 | 备注 | — | 填空题多个答案使用英文逗号分隔 |

导入时会自动兼容常见同义列名，例如 `试题难易/难度`、`选项A/A`、`正确答案/标准答案`，缺少“知识类别”时会依次使用 `评价内容`、`知识点`、`能力要素` 等分类列。选项内容中重复的 `A、`、`B.` 等前缀会被自动清理；简答、计算和案例分析题按文本答案导入。隐藏工作表不会导入，额外的说明页会自动跳过。

读取采用流式方式，只保留有值的单元格。即使 Excel 因历史格式遗留了大量仅带样式的空单元格，也不会为这些空格创建题目数据。

在软件设置中可直接下载模板，也可运行：

```bash
npm run template -- ./quiz-forge-import-template.xlsx
```

模板包含筛选、文本格式、题型/难度下拉选项，以及单选、多选、判断、填空各一条小学加减法示例。自动化测试会把这份真实 Excel 二进制重新导入生产解析器，并验证题量、题型、答案与判题结果。

## 📚 内置题库与本地数据

经题库提供者确认，这 7 份题库可以随公开软件发布。标准化后的题目与版本清单保存在 `public/data/`，参与 PWA 和 Android 打包。根目录 `.gitignore` 仍排除：

- `data/` 中的原始题库和转换后的 Excel 文件；它们可留在本地作为维护源文件
- `dist/`、Android Web 资源副本、APK/AAB 与 `release/`
- 环境变量、签名文件和常见服务配置凭据

更新本地 Excel 后，先生成标准导入文件，再刷新公开内置数据：

```bash
npm run convert:banks
npm run bundle:banks
```

转换脚本为每份源文件在 `data/converted/` 生成一个标准 17 列的独立 `.xlsx`；打包脚本用软件的实际导入器把这些文件写入 `public/data/questions.json` 和版本清单。第 2 步生成的 JSON 会公开提交，供安装包直接加载。答案引用缺失选项的源题会跳过并报告行号。现有继保源表第 149 行因此未纳入；第 560 行标为多选但只给一个答案，已保留，建议复核。

提交前建议检查：

```bash
git status --short --ignored
git ls-files
```

## 🧪 测试与质量保障

```bash
npm test
```

测试覆盖：

- ✅ 内置题库首次安装与版本升级后进入本地数据库
- ✅ 无内置数据时加载小学算术演示题作为后备
- ✅ 真实 `.xlsx` 模板生成 → 生产解析器导入 → 自动判题
- ✅ IndexedDB 题库、进度和练习会话持久化
- ✅ 单选、多选、判断、填空的答案规则
- ✅ 练习跳题、滑动、恢复现场与模拟考试交卷
- ✅ GitHub 干净检出环境将内置题库同时打入 PWA 和签名 Android APK

## 🗂️ 项目结构

```text
quiz-forge/
├── src/components/            # 🧩 页面与交互组件
├── src/services/              # 📥 Excel 导入与 IndexedDB
├── src/utils/                 # ✅ 判题规则
├── src/demoDataset.ts         # ➕ 无内置数据时的后备演示题
├── public/data/                # 📚 随软件发布的内置题库
├── scripts/                   # 🛠️ 模板、图标、题库生成工具
├── android/                   # 📱 Capacitor Android 工程
└── .github/workflows/         # 🚀 自动测试、构建与发布
```

---

<div align="center">
  <strong>🎓 打开即可练习内置题库，也可以导入自己的 Excel。</strong>
</div>
