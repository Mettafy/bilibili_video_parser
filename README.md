# B站视频解析插件 Maisaka 版

适用于 Maisaka 新插件系统的 B站视频内容解析插件。

当前版本的正式能力边界如下：

1. 视觉分析只支持 Maisaka 宿主视觉任务和 `none` 模式。
2. 总结生成只支持 Maisaka 宿主模型任务。
3. ASR 仍然使用插件独立配置的 OpenAI 兼容音频转写接口。
4. 宿主模型调用超时通过独立的 `[host_timeout]` 配置块控制，单位为分钟。
5. 自动检测链与 `/bili` 命令链的总预算统一通过 `[trigger_timeout]` 配置块控制。

## 注意事项

1. 必须安装插件依赖。
2. 如需使用 ASR，必须在插件配置中填写可用的插件独立 OpenAI 兼容语音转写接口。
3. ffmpeg / ffprobe 是本插件的核心依赖，用于：
   - 视频抽帧
   - 音频提取
   - 视频时长探测
4. 使用 `SESSDATA` 获取字幕可能导致 B站风控，请使用小号。

## 安装前准备

### 安装 ffmpeg

本插件依赖 `ffmpeg` / `ffprobe`。

- Windows
  - 推荐把 ffmpeg 的 `bin` 目录加入系统 `PATH`
  - 也可以在插件配置 `[video].ffmpeg_path` 中填写：
    - `bin` 目录路径
    - 或 `ffmpeg.exe` 完整路径
- Linux
  - Debian / Ubuntu：`sudo apt install -y ffmpeg`
  - Fedora / RHEL：`sudo dnf install -y ffmpeg`

## 安装插件

1. 将 `plugins/bilibili_video_parser_maisaka` 放入 Maisaka 的 `plugins/` 目录。
2. 安装依赖：

```bash
pip install -r plugins/bilibili_video_parser_maisaka/requirements.txt
```

3. 检查插件目录下的 `config.toml`。
4. 重启 Maisaka 或重新加载插件。

## 使用方法

### 自动检测模式

直接发送：

```text
https://www.bilibili.com/video/BV1xx411c7XZ
https://b23.tv/xxxxxx
av12345678
BV1xx411c7XZ
```

插件会解析视频，并把处理后的文本写回消息上下文，再交给 Maisaka 主回复系统继续回复。
自动检测链会先准备视频基础信息，再在剩余预算内尝试字幕、视觉、ASR 与总结。
当自动检测总超时发生时，只要已经拿到标题、作者、简介、时长等 metadata，就会优先用基础信息重写用户消息，而不是退回纯视频 ID 文本。

### 命令模式

```text
/bili https://www.bilibili.com/video/BV1xx411c7XZ
/bili av12345678
/bili BV1xx411c7XZ
```

命令模式会直接返回工具型结果文本，不再在插件内部模拟 bot 人设进行聊天式回复。
`[command]` 配置块只控制这条工具型命令链的提示与参数行为，不再承担旧回复模式开关。
命令链的真实总预算由 `[trigger_timeout].command_total_timeout_sec` 控制；组件外层 RPC 超时通过 `@Command(..., timeout_ms=...)` 静态声明为更高上限，避免默认 60000ms 先切断命令桥。

## 视觉模式

当前只支持两种视觉模式：

1. `host`：使用 Maisaka 宿主已配置的视觉任务分析关键帧。
2. `none`：完全跳过视觉分析，只使用字幕、插件独立 ASR、卡片文字和基础信息。

不再支持插件内置 VLM，也不再支持豆包视频理解。

## 宿主超时覆盖

`[host_timeout]` 配置块用于覆盖插件调用 Maisaka 宿主 LLM 能力时的单次请求超时。

它只控制宿主 LLM 单次请求，不控制自动检测链或 `/bili` 命令链的整条入口总预算。

关键规则：

1. 用户填写单位固定为分钟。
2. 默认值为 `5.0` 分钟。
3. 可以填写小数，例如 `1.2`、`5.2`。
4. 插件内部会向上取整转换为毫秒后注入宿主 LLM 调用。
5. 当前版本中，宿主任务发现与宿主模型请求共享同一套预算策略。
6. 当 `[host_timeout].enabled = false` 时，宿主 LLM 调用都会退回 Runner 默认 RPC 超时。

示例：

1. `1.2` 分钟会转换为 `72000ms`
2. `5.2` 分钟会转换为 `312000ms`

## 降级机制

### Level 1：完整模式

- 下载成功
- 宿主视觉分析成功或部分成功
- 字幕 / ASR 可用
- 总结成功

输出：

1. 自动检测链：总结结果被写回增强后的用户消息
2. 指令链：直接返回基础信息 + 总结结果

### Level 2：字幕模式

- 宿主视觉分析失败或被跳过
- 但字幕 / ASR 仍然可用

输出：

1. 自动检测链：回退到 raw_info 或 metadata 级增强文本
2. 指令链：返回原始信息，并附加总结失败说明

### Level 3：基础信息模式

- 无视觉分析
- 无字幕 / ASR

输出：基础信息文本；在自动检测链中作为 metadata 级回退，在指令链中附加总结失败说明。

### 自动检测总超时

自动检测总超时遵循以下规则：

1. 先保住 metadata 级结果。
2. 内容升级阶段超时时，优先回退为基础信息重写文本。
3. `[trigger_timeout].auto_detect_total_timeout_sec` 是严格总预算，超时后不会再额外发起恢复请求。
4. 只有 metadata 阶段本身未完成时，才会退回最小短文本。

### 命令链总超时

`/bili` 命令链和自然语言自动检测链属于同一类“入口链预算”。

1. `[trigger_timeout].command_total_timeout_sec` 控制命令链真实执行预算。
2. `[host_timeout]` 只控制命令链内部宿主 LLM 单次请求预算。
3. 命令桥外层 RPC 超时通过 Command 组件静态 `timeout_ms` 元数据声明更高上限，避免默认 60000ms 抢先超时。

## 缓存规则

1. 视频缓存除了 `video_id/page`，还会绑定配置指纹，避免模型、提示词、ASR 开关变化后误用旧缓存。
2. `card_visual_text` 属于当前消息上下文，不会写入视频级缓存。
3. 当前消息带来的卡片预览图描述只参与本次结果装配，不跨消息复用。
4. 当前消息带有卡片预览图描述时，不会直接复用不含该描述的历史总结缓存。

## 命令输出

命令链总结失败但仍有原始信息时，会输出基础信息、原始内容详情和失败说明。原始内容详情只包含关键帧、卡片预览图描述、字幕和语音识别内容，不重复输出标题、UP 主、简介和时长。

## 宿主任务可用性

1. 插件不再静默切换到其它宿主任务。
2. 视觉任务不可用时，会显式降级为 `none`。
3. 总结任务不可用时，会显式关闭总结阶段并回退到原始信息或基础信息。

## 配置文件

真实配置文件：

`plugins/bilibili_video_parser_maisaka/config.toml`

请直接编辑该文件。

重点配置块：

1. `[trigger_timeout]`：统一控制自动检测链与命令链的入口总预算。
2. `[analysis]`：控制视觉模式，只允许 `host` 或 `none`。
3. `[analysis.host]`：控制宿主视觉任务名与视觉提示词。
4. `[host_timeout]`：控制宿主模型调用超时覆盖。
5. `[asr]`：控制插件独立 ASR。
6. `[summary]`：`summary_max_chars` 只进入 prompt，`max_tokens` 才是宿主模型真实生成预算。
