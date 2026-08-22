# codex-to-zcode

让 Codex 像控制 dsh（DeepSeek harness）一样把编码任务**派发给 ZCode 桌面端**执行——任务以 ZCode 原生任务的形式出现在界面里，**流式实时可见**，结果落盘回收、由 Codex 审查。

```
Codex（方案/审查/git） ──dispatch.py──▶ ZCode 桌面端调度器（~20s 认领）
                                          │ 创建全新任务，UI 实时可见
                                          ▼
Codex ◀──轮询回收──── zcode-report.md ◀── ZCode 执行（写报告文件）
```

## 原理

ZCode 桌面端有一个 automations 调度库 `~/.zcode/v2/tasks-index.sqlite`，内置调度器每 20 秒扫描一次到期任务并认领执行。本技能的 `dispatch.py` 向该库注入一行一次性（one-shot）任务，桌面端认领后会：

1. 在 ZCode 界面创建一个**全新任务**（侧边栏可见、流式输出）；
2. 以 `desktop-continuous` 模式执行注入的 prompt；
3. 任务按约定把「改动文件列表 + 测试结果 + 失败原因」写到 `<仓库根>\Zcodedispatch\zcode-report.md`；
4. `dispatch.py` 轮询该报告文件回收结果，并打印 sessionId 供修正轮次复用（`--target-task`，等价 dsh 的 `-ReuseSession`）。

## 安装

```powershell
git clone https://github.com/a593477311-lgtm/codex-to-zcode.git
Copy-Item -Recurse codex-to-zcode "$env:USERPROFILE\.codex\skills\zcode-dispatch"
```

依赖：Python 3（仅标准库）；ZCode 桌面端处于运行状态。

## 使用（Codex 侧）

```powershell
# 派活（默认：全新任务、yolo、GLM-5.3、思考 max、5s 后触发、超时 20 分钟）
python "$env:USERPROFILE\.codex\skills\zcode-dispatch\scripts\dispatch.py" `
  --task-file "E:\repo\Zcodedispatch\mytask-task.txt" --cwd "E:\repo" --title "任务标题"

# 修正轮次：续在同一任务（sessionId 来自上次输出）
python ...\dispatch.py --task-file ... --cwd ... --target-task "sess_xxxxxxxx"
```

任务文本必须自包含，固定五要素（详见 `SKILL.md`）：

1. 目标仓库绝对路径 + agent.md 完整路径（要求先完整读）
2. 改动文件白名单（白名单外一律不碰）
3. 红线约束（git 只读、禁 push/服务器/数据库/密钥）
4. 完成后要运行的测试命令
5. 输出落盘到 `<仓库根>\Zcodedispatch\zcode-report.md`

分工：Codex 负责需求讨论、方案设计、代码审查、跑测试、git 提交部署；ZCode 只做编码落地，git 仅限只读。

## 注意事项

- **ZCode 桌面端必须开着**，否则调度器不认领（同 dsh web 必须跑在 3080 一样）。
- 派活默认开全新任务；`--target-task` **不要**绑定用户正在聊天的活跃会话——忙会话里 prompt 只会排队，不会立即执行。
- 注入的是 ZCode 桌面端私有库，**应用升级可能改 schema**：脚本每次派单前校验表结构，校验失败立即报错停用，请勿绕过。
- 建议在目标仓库 `.gitignore` 中加入 `Zcodedispatch/`。
- 备用通道（不需要 UI 可见时）：`node "C:\Program Files\ZCode\resources\glm\zcode.cjs" -p "<prompt>" --cwd <仓库> --json`（需先在 `~/.zcode/cli/config.json` 配置 model provider；`--resume <sessionId>` 可续会话）。

## 文件

- `SKILL.md` — Codex 技能定义（分工铁律、派活流程、回收审查循环、故障排查）
- `scripts/dispatch.py` — 派活与回收脚本
