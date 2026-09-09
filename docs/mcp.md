# 自有 Visual Library MCP

通过Codex直接查自己的图文资料库。资料仍完整保存在本GitHub仓库，MCP仅提供只读调用；无需MeiGen MCP、网站、远程服务器或生图API。

## 日常使用

连接名：`visual-library`。可以直接说：

- 从我的视觉资料库找几张人像参考，先看图片。
- 打开刚才那个案例的v1，给我完整原提示词。
- 看看资料库有哪些分类和提示词模板。

四个工具：`search_cases`（关键词/类别/标签/全文/收藏）、`get_case`（指定版本的完整原文和来源）、`get_case_image`（实际图片内容与原图链接）、`list_catalog`（分类、别名与模板）。关键词和标签为组合匹配，不能把它称为视觉语义搜索；已支持model_family、artist、movement、material、record_type、review_status筛选，并显示资料类型和复核状态。

检索先返回轻量结果，再按ID和版本读取图文。每条结果带自有仓库commit；后续传入expected_commit可防重连后换快照。默认最新版本只指当前快照内的最新完整版本，指定不存在的vN会报错，不回退。

## 数据与程序

启动器从自己的仓库获取main最新提交，并在独立缓存创建、验证固定快照；整个MCP进程使用同一快照。重启该MCP才刷新资料，不在每次查询中联网。`fresh`表示启动时刷新成功，并非永远与远端实时一致。

缓存默认在用户目录 `.cache/visual-library-mcp`；它可由GitHub重建，不是唯一存储。查询不读写用户正在编辑的检出目录，也不访问内容上游。网络失败时只允许使用已验证快照，并返回`cached-offline`；无可用快照则启动失败。旧快照不自动删除，避免破坏仍在使用它的会话。

图片工具返回PNG预览及原图链接/哈希，预览最长边最多1600像素；原图不改写。超64MiB文件不生成预览，仍可从案例详情取得原图地址。GIF等多帧资源仅预览首帧，原文件保留。

运行代码与数据缓存分开：使用已安装的代码，不执行下载快照里的脚本；内容更新不自动升级代码或依赖。程序升级需明确更新安装副本，保存旧副本后重启连接。

## 配置方式

Python 3.12+与Git。创建独立虚拟环境并安装`requirements-mcp.txt`；把scripts中的serve_mcp.py、mcp_library.py、library.py、validate_archive.py作为同一套安装文件保存。不要只复制入口而遗漏依赖脚本。

Codex的MCP配置使用STDIO，command指向该虚拟环境Python，args为`["-B", "安装目录/scripts/serve_mcp.py"]`。启动超时建议180秒，工具超时60秒；enabled_tools仅包含上述四项。无需GitHub写入凭证（当前仓库公开）。

先运行`serve_mcp.py --prepare-only`准备缓存；已有克隆可用`--seed <路径>`加速首次对象复制，仍会核对远端。`--offline`显式使用已有快照。准备参数只用于启动器，不是MCP写工具。

配置保存在Codex使用的config.toml。若当前客户端尚未挂载新工具，在设置→MCP servers中重启visual-library连接，再在新任务中验证；配置保存与当前任务工具可用是两种状态。

## 写入与上游维护

MCP不提供收录、删除、自动回源或生成工具。用户要求保存时，Codex沿本库既有流程操作自己的仓库；新资料提交后重启MCP即可读取。MeiGen仅在用户指定案例时择例收录。已有GitHub Actions同步独立运行，MCP不改变它的来源权限或时间。

## 验证

原资料库测试：`python -B -m unittest discover -s tests -v`。
MCP独立环境测试：`python -B -m unittest discover -s tests_mcp -v`。
MCP测试依赖只用于独立工作流，不增加原每日同步的运行依赖。

搜索只返回短摘要和定位信息，完整复核证据、图片角色及多语言原文通过get_case读取。新元数据不改写既有vN.json，个人修正优先。
