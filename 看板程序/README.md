# 投递看板程序

公开项目的快速安装与安全边界见上层目录的 `README.md`、`AI_SETUP.md` 和
`AGENTS.md`。本目录只维护程序、环境和运行日志；每个用户自己的公司邮件、同步状态
和待更新结果位于 `../业务数据/`，不会提交到 Git。

## 程序结构

| 目录/文件 | 用途 |
| --- | --- |
| `app/` | FastAPI 接口、飞书访问、邮件读取 |
| `static/`、`assets/` | 看板页面及资源 |
| `tools/mail/` | QQ 读取、增量同步、归档分析和草稿工具 |
| `tools/feishu/` | 投递记录、更新预览、确认写入及维护工具 |
| `tools/dashboard.py` | 看板启动入口 |
| `tests/` | 离线回归测试 |
| `project_paths.py` | 程序、业务数据、导出和备份的统一路径 |
| `run-tool.bat` | Windows 工具启动器，统一解释器和工作目录 |
| `docs/` | 工具文档；`历史说明/` 仅供追溯，不作为当前操作指引 |
| `运行日志/` | 当前与历史服务器日志 |

## 运行

以下命令都在本目录执行：

```powershell
& .\.venv\Scripts\python.exe -m tools.dashboard
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

启动地址保持 http://localhost:8765；重复启动会打开已经运行的看板。

常用命令模块：

- `tools.mail.sync_and_analyze`：同步邮箱并生成本地分析，不自动写飞书。
- `tools.mail.analyze_recruitment_mail`：只分析本地存档，不读取邮箱。
- `tools.mail.create_qq_draft`：写入 QQ 草稿箱，不发送邮件；`--preview` 仅预览。
- `tools.feishu.record_feishu_application`：录入投递，可使用 `--dry-run`。
- `tools.feishu.match_feishu_mail_preview`：生成待确认的匹配结果。
- `tools.feishu.apply_feishu_mail_updates --confirm`：明确确认后写入飞书。
- `tools.feishu.sync_recruitment_tasks --reconcile`：将飞书任务完成/恢复状态回写任务账本。
- `tools.feishu.normalize_company_aliases`：预览公司别名统一方案；明确确认后添加 `--apply` 写入。

## 数据与配置

`app/mail_store.py` 和邮件归档工具共用 `project_paths.py` 中的数据根目录。已有邮件记录中的 `公司投递/...` 相对路径保持不变，无需重读邮箱。

`.env` 保持在本目录，QQ 授权码由 Windows 凭据管理器或仓库外的受限密钥文件
保管。不要公开上传 `.env`、业务数据或历史备份。

备份或迁移时须分别保存程序和业务数据，并保持同级关系；`.venv` 不应上传到其他
操作系统，应在目标系统重新安装依赖。
