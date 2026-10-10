---
name: ping
description: 平台技能通道冒烟测试：验证内置 Agent SDK 与 WebFetch 工具链路，回一行 pong。适合在「🛠 技能」tab 第一次使用时先跑这个。
argument-hint: "[你的名字]"
---

这是一次技能通道冒烟测试，无需询问任何问题，直接执行：

1. 用 WebFetch 抓取 https://api.github.com/zen ，拿到 GitHub 哲学短语。
2. 只输出一行中文，格式：`pong — SDK 通道 OK，GitHub 说「<zen 短语>」，收到参数：<用户传入的参数，没有则写「无」>`
