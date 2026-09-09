# Visual Library · 个人视觉资料库

仓库地址：https://github.com/yangchengwu2024/visual-library

供 Codex 检索和整理的图片、提示词与模板资料库。没有网站、登录系统或在线生图服务。图片和完整提示词保存在本仓库，原站链接用于溯源；上游断链或删除案例不会自动删除本库内容。

## 查看资料

**[🖼️ 进入画廊总览](../docs/gallery.md)** · [全部案例第 1 册](../docs/gallery-part-1.md) · [提示词模板](../docs/templates.md)

画廊总览提供分类目录和分册入口。点击案例标题即可查看本仓库保存的图片、完整提示词和版本，不需要打开独立网站。

## 让 Codex 查看仓库

把本仓库链接交给 Codex，例如：

> 阅读这个资料库的 AGENTS.md 和 README，帮我找几张复古暖色人像参考。先给案例编号、版本、图片和匹配理由，需要时提供完整提示词。

Codex 先读取 [轻量案例索引](../indexes/catalog.json) 和 [分类与别名](../metadata/taxonomy.json)，再按需读取少量候选案例。判断画面效果时实际看图，不一次读取全库图片与长提示词。本库目录中的版本 JSON 是权威内容，旁边同名 Markdown 是生成的阅读版。

有终端时可检出后使用 Python 3.12+；仅依赖标准库，无需配置生图 API：

```text
git clone https://github.com/yangchengwu2024/visual-library.git
cd visual-library
python -B scripts/library.py query 人像 --limit 5
python -B scripts/library.py query 复古 暖色 --full-text --limit 5
python -B scripts/library.py query --category 人物与角色 --limit 5
python -B scripts/library.py show <案例编号> --version v1
```

默认查询轻量元数据；关键词不够时使用 `--full-text` 查完整提示词。显示结果包含案例 ID 和版本，图片指向本仓库内文件。查询和展示不写入资料库。当前案例数量见索引中的 `case_count`，模板见 [模板索引](../indexes/templates.json)。

## 分类

沿用来源的 13 个导航分类：UI 与界面、图表与信息可视化、海报与排版、商品与电商、品牌与标志、建筑与空间、摄影与写实、插画与艺术、人物与角色、场景与叙事、历史与古风题材、文档与出版物、其他应用场景。

分类、风格、场景分别保存，保留原始标签，常用中英文同义词可用于检索。来源关键词标签只是检索线索，不代表已经逐图核验。个人修正放在 `personal/`，不改写原始提示词。

## 收藏、备注与改写

明确要求保存时，Codex 才执行写入。先同步最新仓库，再针对结果里的具体 ID/版本操作：

```text
python -B scripts/library.py favorite <案例编号> --version v1
python -B scripts/library.py note <案例编号> "我的使用备注" --version v1
python -B scripts/library.py query --favorites
```

个人记录在 `personal/`，提交并推送后，Codex 按仓库内的最新记录读取。收藏固定关联到指定版本。原始提示词不可直接改写；自己的改写存为个人记录或经明确指定作为新案例版本导入。详细命令见 `python -B scripts/library.py --help`。

## 完整存储与版本

- `cases/<id>/vN.json`：原始提示词、图片清单、内容摘要、模型/参数（来源未提供则未知）、来源版本与差异。
- `cases/<id>/vN.md`：从 JSON 生成的可读版本，图片链接指向本库。
- `images/<sha256>.<ext>`：原始图片文件；完全相同的文件只保存一份，各案例/版本保留各自引用。
- `sources/`：来源映射、同步状态、原始画廊和模板文档副本。
- `indexes/`：可重建的轻量索引。
- `docs/`：自动生成的画廊总览、分类目录、案例分册与模板入口。
- `personal/`：收藏、备注和个人修正，与上游资料独立。

完全一样的内容不重复生成版本；同一案例的提示词或图像等实质变化追加 v2、v3。旧版本保留，指定 v1 不会替换成最新版。不同来源参数不明时不强行合并；相似图文交给 Codex 看图读文后决定，可先查看候选：

```text
python -B scripts/library.py similar --case <案例编号>
```

确认是同一案例的变体后才使用 `group-variant`，旧 ID、版本和个人引用会保留映射。只相似但属于不同创意的案例继续独立保存，不自动删除。

完整状态表示该来源明确提供的提示词及图片已保存；有些提示词要求使用者另提供照片，这不代表上游附带了那张照片。来源未提供的生成参数、模型细分版本、作者信息不补造。

## 更新维护

首个来源：[awesome-gpt-image-2](https://github.com/freestylefly/awesome-gpt-image-2)。GitHub Actions 每天北京时间约 09:23 检查一次，也可在 Actions 中手动运行 **Sync visual library**。执行在 GitHub 云端，不需要个人电脑开机；GitHub 定时可能延迟，具体以运行记录为准。

有变化才下载固定上游版本、归档图片和原始文档、导入与生成索引和画廊目录；没有变化不提交。失败不清空旧数据，不将缺图案例标记完整。部分资源缺失时，已取得的图片和其他完整新增仍归档，缺项留待补齐，来源成功版本不提前推进。普通上游删除只改变来源状态，本库已存内容继续可查。

发布使用全新检出副本。如果你或另一个 Codex 任务在期间提交了内容，程序不强推、不直接合并冲突版本文件，而是重新读取最新库并计算导入结果，最多重试 3 次。个人记录不会被同步程序覆盖。手动整理发生同字段冲突时由 Codex 保留双方内容后处理。

这条资料库工作流与 AI 日报无关，不新增 Codex 日报定时任务。

## 验证与维护命令

```text
python -B scripts/library.py validate
python -B scripts/validate_archive.py
python -B -m unittest discover -s tests -v
```

验证只检查本仓库文件、图片哈希、版本和来源引用，不访问上游。清理损坏/无引用资源不由日常同步自动执行。修改源分类别名或个人分类修正后，可使用 `python -B scripts/library.py rebuild` 重建索引。

## 来源与使用范围

保留 [上游 MIT](../notices/upstream-MIT.txt)、[上游内容声明](../notices/upstream-disclaimer.md) 和每条案例的原始来源。完整存储不代表取得商业使用授权，具体见 [THIRD_PARTY.md](../THIRD_PARTY.md)。本库不将第三方图片和提示词统一宣称为 MIT 商用素材。

## MCP入口

支持通过自有只读MCP调用上述资料能力，见[连接与使用说明](mcp.md)。现有图文、版本、来源同步不因启用MCP改变。
