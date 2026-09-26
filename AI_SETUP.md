# 秋招看板：AI 配置、数据机制与维护指南

本文件供 Codex、WorkBuddy 或其他具备终端能力的编码代理执行。它只描述秋招看板，
不依赖也不配置 Codex 飞书机器人。

## 完成目标

先使用空白或虚构数据通过离线测试并启动本地看板；用户需要时，再分别接入 QQ 邮箱、
飞书多维表格、飞书任务和飞书日历。真实账号和业务数据不是离线启动的前置条件。

完成后应满足：

- `python -m unittest discover -s tests -v` 全部通过。
- `python -m tools.dashboard` 可启动，浏览器能打开 `http://localhost:8765`。
- `.env`、邮箱授权码、`业务数据/` 和 `历史备份/` 未被 Git 跟踪。
- 已启用的外部集成分别完成只读验收；写入操作经过预览和用户确认。

## 开始前只确认三件事

1. 目标系统是 Windows、macOS 还是 Linux。
2. 用户只需要本地看板，还是还要接入飞书。
3. 是否启用 QQ 邮箱只读同步；默认不启用。

除登录、2FA、管理员审批、邮箱授权码生成和有副作用的写入确认外，其他安装与排查由
代理继续完成。

## 安全边界

- 先完整阅读 `AGENTS.md`、`README.md` 和本文件。
- 不读取、打印、提交或上传真实 `.env`、邮箱授权码、邮件正文、投递记录、面试资料、
  飞书令牌、Codex 登录态和服务器登录信息。
- 不发送邮件。创建草稿、写飞书、同步日历或批量修改记录前必须让用户确认。
- 所有真实数据留在 `业务数据/`，凭据留在 Git 之外。
- 不在源码中写死用户邮箱、服务器地址、飞书资源 ID 或模型名称。

## 看板如何工作

```text
浏览器页面
  -> FastAPI 看板服务
  -> 业务数据/ 中的本地邮件、同步状态和缓存
  -> 可选：飞书多维表格（投递主记录）
  -> 可选：飞书任务（测评/笔试/面试等行动项）
  -> 可选：飞书日历（有明确截止时间的事项）
```

核心数据目录：

- `业务数据/公司投递/`：按公司归档的招聘邮件及索引。
- `业务数据/同步记录/`：邮箱 UID 游标、去重账本、日历状态、公司简介缓存等。
- `业务数据/待更新/`：写入飞书前的匹配和变更预览。
- `业务数据/邮件导出/`：用户主动导出的邮件材料。
- `历史备份/飞书数据快照/`：飞书数据的本地安全快照。

## 首次启动

进入 `看板程序`，创建虚拟环境并安装依赖。

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m tools.dashboard
```

Windows：

```powershell
py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m tools.dashboard
```

确保下列目录存在，但不要往公开仓库写入真实数据：

- `业务数据/公司投递/`
- `业务数据/同步记录/`
- `业务数据/待更新/`
- `业务数据/邮件导出/`
- `历史备份/飞书数据快照/`

## QQ 邮件机制

QQ 邮箱接入使用 IMAP 只读方式，不改变已读状态，也不会发送邮件。

1. 用户自行在 QQ 邮箱开启 IMAP 并生成授权码。
2. Windows 优先存入系统凭据管理器；Linux 使用仓库外且权限为 `0600` 的
   `QQ_MAIL_AUTH_CODE_FILE`。
3. 首次同步按用户指定日期扫描；之后保存 `UIDVALIDITY` 和最后成功 UID，只读取新增 UID。
4. 同一封邮件再用标准化 `Message-ID` 去重；邮箱 UID 体系变化时，只回看有限时间窗口，
   不会从头重复导入全部邮件。
5. 新邮件由已登录 ChatGPT 的 Codex CLI 批量判断是否为招聘邮件，并提取公司、岗位、
   事件、截止时间、操作链接、建议进展和紧急程度。模型不可用时默认不推进 UID 游标，
   留到下次重试，避免把低质量规则结果当成最终结论。
6. 非招聘邮件只写去重状态，不进入看板。招聘邮件按公司保存原始 `.eml`、可读 `.txt`
   和结构化 `.json`，随后重建公司索引。
7. 邮件分析结果先生成看板待办和飞书变更预览。只有高置信度、唯一匹配且属于本次新增
   邮件的项目才可在定时同步中自动应用；不唯一的项目留在预览中等待人工确认。
8. 飞书主记录和飞书原生任务是同一行动项的两个投影，稳定键和幂等 token 防止重复创建。

生产定时服务使用：

```bash
python -m tools.mail.scheduled_sync --apply
```

没有新 UID 时，该命令不会启动 Codex，也不会刷新下游任务。

## 飞书机制（可选）

- 多维表格保存公司的投递主记录和当前进展。
- 飞书任务保存需要行动的测评、笔试、面试和补充材料事项。
- 飞书日历只同步具有明确截止时间的事项，并使用幂等键避免重复事件。
- 本地看板聚合以上数据，提供搜索、筛选、分页、优先级、时间轴和历史入口。
- 任何首次写入或批量变更都应先运行预览，把新增、修改和完成数量展示给用户，得到
  明确确认后才应用。

不要因为配置了一个飞书应用就假定所有表格、任务清单和日历都可写；逐项验证权限。

## 定期更新机制

Linux 部署提供三个相互独立的 systemd 定时器：

- `job-tracker-mail-sync.timer`：开机 2 分钟后首次运行；上次运行结束 15 分钟后再检查，
  并加入少量随机延迟。只处理新增邮件。
- `job-tracker-calendar-sync.timer`：每天上海时间 00:00 和 12:00 同步截止日历；错过的
  执行会由 `Persistent=true` 补跑。
- `job-tracker-company-profile-sync.timer`：开机 4 分钟后首次运行；之后每 15 分钟为当前
  活跃公司补一条缺失简介。简介写入本地缓存，页面刷新不会重复联网生成。

查看状态：

```bash
systemctl list-timers 'job-tracker-*'
journalctl -u job-tracker-mail-sync.service
journalctl -u job-tracker-calendar-sync.service
journalctl -u job-tracker-company-profile-sync.service
```

定时器只负责本看板的数据维护，不会升级程序版本。

## 程序版本更新

源码更新由本仓库的 Git tag 和 GitHub Release 发布。每个用户独立保存 `.env` 和
`业务数据/`，发现新版时只提醒，不自动安装；用户确认后再执行：

1. 备份业务数据和同步状态。
2. 确认工作区没有未提交的个人修改。
3. `git fetch --tags origin` 和 `git pull --ff-only`。
4. 更新依赖并运行完整离线测试。
5. 测试通过后重启服务并做页面健康检查；失败则保留旧版本。

不要用删除工作区、强制拉取或覆盖数据目录的方式解决版本冲突。

## 推荐交给代理的提示词

```text
请把当前仓库配置成我自己的秋招看板。先完整阅读 AI_SETUP.md、AGENTS.md 和 README.md。
先用空白或虚构数据安装依赖、运行离线测试并启动 http://localhost:8765。然后只询问我：
是否接入飞书，以及是否启用 QQ 邮箱只读同步。需要登录、2FA、管理员审批、生成邮箱
授权码或写入飞书时再让我介入。不要显示或提交任何密钥、邮件、投递记录和面试资料；
所有写入先预览并等我确认。最后说明本地看板、邮件增量同步和已启用定时器的验收结果。
```
