# Job Application Tracker / 秋招求职看板

一个把求职投递、招聘邮件、测评/笔试/面试待办和复盘资料集中到同一张看板的
个人工具。它可以独立运行，也可以选择连接飞书多维表格、飞书任务、飞书日历和
QQ 邮箱。

这个公开仓库只包含程序和虚构示例。真实邮件、投递记录、账号凭据和面试资料始终
保存在使用者自己的设备或服务器上，不进入 Git。

## 功能

- 求职待办、优先级、截止时间轴和状态管理。
- 投递记录搜索、进展筛选和分页。
- 招聘邮件只读同步、分类、归档与任务匹配。
- 飞书多维表格、任务和截止日历同步。
- 公司简介缓存和面试复盘入口。
- 本地 Windows 入口，以及可选的 Linux systemd 服务模板。

## 最快开始

如果使用 Codex、WorkBuddy 或其他编码代理，直接把仓库交给它并发送：

```text
请完整阅读 AI_SETUP.md、AGENTS.md 和 README.md，帮我配置并启动这个秋招看板。
先使用虚构数据完成离线测试；需要我登录飞书、生成邮箱授权码或确认写入云端时再暂停。
不要显示或提交任何密钥、邮件、投递记录和面试资料。
```

人工安装时，在 `看板程序` 目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m tools.dashboard
```

Windows 使用：

```powershell
py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m tools.dashboard
```

默认地址为 <http://localhost:8765>。

## 配置原则

- `.env.example` 只含占位符；复制为 `.env` 后填写自己的飞书配置。
- `业务数据/`、`历史备份/`、`.env` 和授权码均被 Git 忽略。
- 邮箱同步使用 IMAP 只读方式。授权码应保存在系统凭据管理器或权限为 `0600`
  的仓库外文件中。
- 任何发送邮件、写飞书或批量更新操作，都应先预览并由用户明确确认。

## 项目结构

- `看板程序/`：FastAPI 应用、前端、邮件/飞书工具和测试。
- `业务数据/`：每个用户自己的投递与邮件数据，只保留空目录结构。
- `常用入口/`：Windows 常用启动器。
- `AI_SETUP.md`：供编码代理执行的配置流程。
- `INTEGRATION_SETUP.md`：两套独立项目的 AI 配置、边界和可选协作说明。
- `AGENTS.md`：编码代理必须遵守的数据与操作边界。
- `UPDATES.md`：版本更新和个人数据保留策略。

## 更新

功能更新通过这个公开仓库的版本发布统一提供。每个用户独立决定何时更新；更新程序
不会覆盖 `.env` 和 `业务数据/`。详见 [UPDATES.md](UPDATES.md)。

## License

MIT
