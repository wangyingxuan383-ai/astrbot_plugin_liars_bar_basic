# astrbot_plugin_liars_bar_basic

AstrBot QQ 群聊《骗子酒馆基础版》插件（3~5 人）。

## 功能概览
- 群聊开房/加入/开始/质疑/剪线/状态/结束/帮助
- 私聊查看手牌、私聊暗出多牌（`/酒馆 出 2 4 5`）
- 一人一房约束（同一玩家同一时间仅可在一个房间）
- 出牌超时淘汰、剪线超时自动剪线
- 本地素材卡牌与炸弹图（不依赖在线文生图）

## 基础规则（当前实现）
- 每小局随机目标牌：太阳 / 月亮 / 星星。
- 每小局会先清空上局手牌，再给当前存活玩家每人固定发 5 张。
- 本大局总牌池在开局时锁定：`开局人数 × 5`（3 人=15 张，4 人=20 张，5 人=25 张）。
- 大局中即使有人淘汰，总牌池也不会变化；只是每小局会留下更多“未发牌”。
- 牌型按比例分配：太阳/月亮/星星/魔术 = `3/3/3/1`（按总牌数做比例换算并取整补齐）。
- 出牌在私聊进行，群里只公布“宣称出牌数量”。
- 多牌判定：任一假即判假，魔术牌视作真。
- 受罚玩家进入剪线阶段：三选一 -> 二选一 -> 一选一必爆。
- 玩家出完手牌后，系统自动触发下家质疑并结算。

## 命令列表

### 群聊命令
- `/酒馆 开房`：创建房间，发起者自动成为房主。
- `/酒馆 加入`：加入本群房间。
- `/酒馆 开始`：房主开局（要求 3~5 人）。
- `/酒馆 状态`：查看当前阶段、轮次、行动人、玩家状态。
- `/酒馆 质疑`：由当前行动玩家发起质疑。
- `/酒馆 剪线 红|蓝|黄`：剪线；兼容数字 `1/2/3`。
- `/酒馆 结束`：房主或管理员结束房间。
- `/酒馆 帮助`：查看帮助。

### 私聊命令
- `/酒馆 手牌`：查看手牌图和序号。
- `/酒馆 出 2 4 5`：按序号一次出多张牌。

### 私聊限制
- 私聊不能使用开房/加入/开始/质疑/剪线/结束等群聊指令。

## 超时规则
- `play_timeout_seconds`（默认 120 秒）：轮到玩家出牌超时则整局淘汰。
- `wire_timeout_seconds`（默认 120 秒）：待剪线玩家超时则自动随机剪线。

## 常用流程
1. 群里：`/酒馆 开房`
2. 其他玩家群里：`/酒馆 加入`
3. 房主群里：`/酒馆 开始`
4. 当前行动玩家私聊：`/酒馆 手牌` -> `/酒馆 出 序号...`
5. 群里：下一家选择质疑或继续轮转
6. 进入惩罚时：受罚玩家群里 `/酒馆 剪线 红|蓝|黄`

## 配置项
- `play_timeout_seconds`：出牌超时秒数（默认 120）
- `wire_timeout_seconds`：剪线超时秒数（默认 120）
- `guide_mode`：是否启用“下一步指令”引导文案
- `hand_image_width`：私聊手牌拼图宽度
- `require_dm_reachable_before_start`：开局前检查私聊可达
- `room_ttl_minutes`：等待阶段房间自动回收分钟数

## 数据持久化目录
- 插件数据目录通过 `StarTools.get_data_dir(PLUGIN_NAME)` 获取。
- 默认会落在 AstrBot 规范目录：`data/plugin_data/astrbot_plugin_liars_bar_basic`。
- 持久化文件：
- `state.json`：房间状态
- `cache/`：临时渲染图缓存

## 字体策略（兼容无本地字体环境）
- 优先级 1：若安装了 `astrbot_plugin_sudoku`，优先复用：
- `astrbot_plugin_sudoku/assets/LXGWWenKai-Regular.ttf`
- 优先级 2：使用本插件内字体目录：
- `assets/fonts/LXGWWenKai-Regular.ttf`
- 优先级 3：系统字体回退（Noto/WenQuanYi/DejaVu）

### 数独插件仓库（用于复用字体）
- `https://github.com/wangyingxuan383-ai/astrbot_plugin_sudoku`

### 提醒
- 复用数独字体是“可选优化”，不是强依赖；未安装数独插件时本插件仍可运行。
- 若数独插件不在标准目录或字体文件名变更，请改用本插件 `assets/fonts/` 方案，避免渲染中文失败。

### 未安装数独插件时，如何准备字体
1. 创建字体目录：
```bash
mkdir -p assets/fonts
```
2. 下载霞鹜文楷（推荐）到本插件目录：
```bash
wget -O assets/fonts/LXGWWenKai-Regular.ttf \
  https://github.com/lxgw/LxgwWenKai/releases/download/v1.510/LXGWWenKai-Regular.ttf
```
3. 若没有 `wget`，可改用：
```bash
curl -L -o assets/fonts/LXGWWenKai-Regular.ttf \
  https://github.com/lxgw/LxgwWenKai/releases/download/v1.510/LXGWWenKai-Regular.ttf
```
4. 下载页面（备用）：`https://github.com/lxgw/LxgwWenKai/releases`

## 目录结构
- `main.py`：核心逻辑（状态机、命令、超时、结算）
- `assets/cards/*.png`：卡牌与目标牌图
- `assets/bombs/*.png`：炸弹状态图与爆炸图
- `assets/fonts/`：本插件可选字体目录（无数独插件时使用）
- `_conf_schema.json`：配置定义
- `requirements.txt`：依赖声明
