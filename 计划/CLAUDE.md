# 智能音箱_无实物 项目配置

## 语言要求
- **所有回复必须使用简体中文**，包括代码注释、文档说明、技术讨论
- 代码中的变量名、函数名可使用英文，但解释说明用中文
- 与用户的全部交互界面文本使用中文

## 项目背景
本项目是一个纯软件实现的"无实物智能音箱"系统 —— 网页端 AI 语音交互中枢。
用户通过麦克风说话，系统自动进行语音识别、智能路由分发（小爱设备控制 / 大模型对话 / Reasonix 编程执行），并通过浏览器 TTS 朗读回复。

## 核心架构（已确定）
- 前端：React 18 + TypeScript + Tailwind CSS + Vite
- 后端：Python FastAPI + WebSocket + asyncio
- ASR：Web Speech API（主力）+ Faster-Whisper（离线备用）
- TTS：Edge TTS（主力）+ Web Speech API speechSynthesis（备用）
- 意图路由：三层混合（关键词规则 → sentence-transformers → LLM 兜底）
- 大模型：Ollama + Qwen2.5:7B（本地主力）+ 云端 API 三级 fallback
- 缓存：内存 LRU（热层）+ SQLite（冷层）双层架构
- 三条处理路径：小爱优先（设备控制）→ 大模型兜底（复杂语义）→ Reasonix（编程任务）
- Reasonix 触发名：用户说「工作助手」「克劳德」「贾维斯」即可调用 Reasonix 进行编程/文件操作
- Reasonix 审批流程：编程意图检测后创建待审批任务，用户说「允许」后由 `reasonix run` 执行
- 高频缓存仅针对大模型路径，小爱调用不做缓存

## 项目文件说明
- 核心需求_无实物.txt：需求定义与路由分发逻辑
- 1整体项目初步架构——无实物.txt：总体架构与演示流程
- 2技术设计与实施方案——无实物.txt：初版技术选型与模块设计
- 技术栈重选型方案——无实物.txt：【最新】完整技术栈重选型，含优势分析、替代方案对比、架构流程图、分阶段计划
- .claude/skills/zh-cn-response.md：项目级中文回复 skill
