---
name: hello
description: 最小技能示例：向指定的人打招呼。也是「如何写一个技能」的活模板——复制本目录改名改内容，就是新技能。
argument-hint: "[称呼]"
arguments: {"称呼": {"type": "text", "label": "称呼", "placeholder": "要问候的对象（如：世界）", "required": true}}
---

参数 `$ARGUMENTS` 是称呼。直接输出一行：`你好，$ARGUMENTS！这是一个运行中的 celestial-snow 情报站平台。`；若参数为空，则输出：`你好！这是一个运行中的 celestial-snow 情报站平台。`
