# 上游更新与手动收录

[返回画廊](gallery.md) · [模型目录](models/index.md) · [复核说明](collection-status.md)

每天北京时间10:00由GitHub Actions检查已批准来源。GitHub可能延迟启动，以运行记录为准；AI日报是独立任务。

## 当前来源范围

| 来源 | 自动范围 |
|---|---|
| freestylefly/awesome-gpt-image-2 | 继续跟随原有完整案例/模板范围 |
| jamez-bondos/awesome-gpt4o-images | 本次选定8条；排除OpenAI官方许可例外目录 |
| PicoTrex/Awesome-Nano-Banana-images | 本次选定6条Pro案例，含2条输入输出齐全编辑例 |
| Ezagor-dev/awesome-midjourney-prompts | 本次选定8条，分案例和风格参考 |

后三个来源仅自动跟随已选条目的修改，不把来源全部新内容自动收入；详细ID在sources/registry.json及各selection.json。内容变化使旧看图结论失效，改为待复核；许可、必要输入或解析结构发生异常时暂停该来源，其他来源继续。普通上游删除不删除本库旧资料。

candidate用于待评估来源，manual_only用于择例来源，disabled不再抓取；这些不会进入定时采集。MeiGen不接MCP、不定时采集，只处理用户指定案例。

## 只检查与执行分开

Actions手动运行默认选择check，只查看变化，不提交任何资料。选择sync才更新已批准来源。没有变化不产生空提交。

```text
python -B scripts/upstreams.py check
python -B scripts/upstreams.py check --source picotrex-nano-banana-selected
python -B scripts/upstreams.py sync
python -B scripts/upstreams.py import --source picotrex-nano-banana-selected --ids pro_case28
```

本地执行前在独立环境安装requirements-sync.txt；不要修改不相关的Python环境。check只使用临时目录，返回固定上游版本；import只追加选中项，不推进全源游标，不把其他项标删除。以上本地命令不自动push，最终发布仍需先验证，再按Git并发规则提交。

没有现成适配器的用户指定链接，由Codex取得图文和必要输入、核对来源后，使用library.import_records(..., mode="append")择例导入。不能据此提权为自动来源，也不能将远程图片URL当作已归档文件。

## 暂停与恢复

网络临时失败保留旧库，下次重试。运行暂停和原因保存在对应来源runtime.json，并在Actions结果中展示；不自动改写用户来源策略。修复已知问题后，可明确重试：

```text
python -B scripts/upstreams.py sync --source <来源ID> --resume
```

已批准范围内的适配修复不需重新审批；新增范围仍应记录实际审核依据。源间暂存隔离，最终索引/引用校验失败不发布到远端；发布冲突从最新远端重新计算，不强推、不覆盖个人记录。

## 归档原文配图

来源Markdown的原始字节保存在同名.original.txt，可读页面统一引用本仓库共享图片。来源原文的其他配图仅用于页面完整展示，不自动计入正式案例。外链统计图保存为采集时快照。每次同步同时验证全部可渲染页面的图片引用和媒体文件，不能以正式画廊正常代替原文页面检查。
