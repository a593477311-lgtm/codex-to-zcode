---
name: zcode-dispatch
description: 向 ZCode 桌面端派发编码任务并回收结果的协作工作流：任务以桌面端原生任务形式运行，用户可在 ZCode 界面实时看到任务在跑。Use when 用户要求把某个任务发给 ZCode / zcode-dispatch 执行，或希望由 Codex 负责讨论需求、设计方案、出文档、审查代码、跑测试、git 与部署，而由 ZCode 做具体编码实现且要求过程在 ZCode 界面可见；也用于 ZCode 返回结果后的审查、复跑测试与问题回传。
---

# ZCode 派活（Codex ↔ ZCode 桌面端协作）

## 原理（先读）

Codex 通过 `scripts/dispatch.py` 向 ZCode 桌面端的 automations 调度库
（`~/.zcode/v2/tasks-index.sqlite`）注入一行一次性任务；桌面端调度器每 20s
扫描认领，**在 ZCode 界面创建一个全新任务并流式执行**——用户可以像看普通任务一样
实时看到它跑。默认 `target_task_id=NULL`（每次派活开新任务，等价 dsh web 新会话）；
传 `--target-task <sessionId>` 可固定续在某个任务里（等价 dsh `-ReuseSession`，
但**不要**绑定用户正在使用的活跃会话——忙会话里 prompt 会排队，不会立即执行）。

## 分工铁律

- Codex（本窗口）负责：需求讨论、方案设计、出文档、代码审查、跑测试、git 提交推送、部署。
- ZCode 负责：按 Codex 给的方案做具体编码落地；git 仅限**只读**（`status` / `diff` / `log`）。
- **git 写操作全部禁止交给 ZCode**：`add` / `commit` / `checkout` / `switch` / `stash` /
  `reset` / `branch` / `push` 一律由 Codex 与用户确认后执行。
- 有外部影响的操作（git push、服务器改动、数据库变更、密钥/凭证读写）**不要**交给 ZCode。

## 前置条件

- ZCode 桌面端必须处于**运行状态**（同 dsh web 必须跑在 3080 一样）；桌面端没开时
  调度器不认领，任务会一直躺着。桌面端由用户自己启动，Codex 不代启动、不代关闭。
- 机器上有 Python 3（脚本只用标准库 sqlite3）。

## 派活命令

任务文本一律写成 UTF-8 文件。任务文件与 ZCode 反馈统一放 `<仓库根>\Zcodedispatch\`
（该目录需 gitignore）：

```powershell
# 派活（默认：全新任务、yolo、GLM-5.3、思考 max、45s 后触发、超时 20 分钟）
python "<skill 目录>\scripts\dispatch.py" --task-file "<仓库根>\Zcodedispatch\<任务名>-task.txt" --cwd "<仓库根目录>" --title "<任务标题>"

# 预览（不执行，检查拼接结果）
python "<skill 目录>\scripts\dispatch.py" --task-file ... --cwd ... --title ... --dry-run

# 复用已有任务续跑（同任务线修正轮次；sessionId 来自上次派活的输出）
python "<skill 目录>\scripts\dispatch.py" --task-file ... --cwd ... --target-task "sess_xxxxxxxx"

# 长任务调大超时
python "<skill 目录>\scripts\dispatch.py" --task-file ... --cwd ... --timeout-sec 3600
```

派单后 Codex 侧**不要**去编辑任务涉及的白名单内文件；同一时刻只派一个任务。

## 任务文本五要素（必须自包含）

1. 目标仓库绝对路径 + agent.md 完整路径（**每次都必须带上**，摘录关键铁律，要求先完整读）
   + 方案/交接文档路径，全部要求 ZCode **先完整读**再动手。
2. 改动文件白名单（允许/禁止），白名单外一律不碰。
3. 红线约束逐条列出（明确 git 只读、禁 push/服务器/数据库/密钥）。
4. 完成后要运行的测试命令。
5. **输出落盘**：把「改动文件列表 + 测试结果 + 失败原因」写到
   `<仓库根>\Zcodedispatch\zcode-report.md`（UTF-8），会话里只回一行摘要。
   回收以文件为准，不要依赖终端捕获。

## 回收与审查循环

1. 派活前确认工作区干净、已切到目标分支（沿用 dsh-dispatch 的专用分支约定时同理）。
2. dispatch.py 会轮询并在拿到结果后打印「ZCode 结果 + sessionId」；超时会退出码 1，
   任务可能仍在 ZCode 界面继续，可凭 automation_id/sessionId 追踪。
3. Codex 读 `zcode-report.md` 并自己 `git diff` 审查，**不要**只信它说的「已通过」。
4. Codex 复跑相关测试 + 完整读一遍更新后的代码自查；必要时全量测试。
5. 有偏差 → 先隔离半成品（`git checkout -- <文件>` 或整体回退），与用户讨论后用
   `--target-task <sessionId>` 派修正轮次，任务文本带上轮报告关键摘录与验收标准。
6. 全部通过后由 Codex 负责 git 提交与文档同步。

## 故障排查

- **派单后迟迟没动静**：确认 ZCode 桌面端在运行；看
  `~/.zcode/v2/tasks-index.sqlite` 的 `automations` 行——`dispatch_status=claimed`
  说明已认领在跑；`schema 不符` 报错说明桌面端升级改了表结构，**停下问用户**，
  不要反复重试。
- 任务绑定了用户正在聊天的会话 → prompt 排队不执行：派活一律默认全新任务，
  `--target-task` 只绑专用的派活任务。
- 旧报告误判为新结果：脚本派单前会自动把 `zcode-report.md` 备份到
  `~/.zcode/Zcodedispatch-backup\`。
- 中文乱码：脚本已强制 UTF-8；终端侧 `chcp 65001`。
- 在错误目录执行会改错仓库：始终显式传 `--cwd` 目标仓库根目录。

## 备用通道（UI 可见性不是硬需求时）

`zcode -p "<prompt>" --cwd <仓库> --json --resume <sessionId>`（可执行文件在
`C:\Program Files\ZCode\resources\glm\zcode.cjs`，用 node 跑）：headless 同步执行、
JSON 输出、可续会话，但不进 ZCode 界面任务列表。注入路径不可用时先用它顶上，
并告知用户界面里看不到。

## 边界

- 一次派一个清晰目标；复杂需求先在 Codex 侧拆成方案文档（`docs/` 下）再逐步派单。
- 注入的是 ZCode 桌面端私有库，**升级可能改 schema**：脚本每次派单前会校验列，
  校验失败立即停用并报告，不要绕过。
- 项目级约定（仓库 agent.md 铁律）由 Codex 保证任务文本覆盖，不假设 ZCode 知道。
