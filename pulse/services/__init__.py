"""Pulse 业务域集合。

本包仅作为各域（content / publish / scheduler / identity / compliance）的父包存在。
按 AGENTS.md §2 的目录所有权，各域只在自己的子包内写代码：

    pulse/services/content/      -> A1 Content
    pulse/services/publish/      -> A2 Publish
    pulse/services/scheduler/    -> A3 Scheduler
    pulse/services/identity/     -> A3 Scheduler（账号与凭据）
    pulse/services/compliance/   -> A4 Compliance

本文件由 root 拥有，子 agent 请勿修改。
"""
