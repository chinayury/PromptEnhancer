#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Project: 提示词工坊项目
# Copyright (C) 2026 智维ITMSKY
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
厂商预设（配置层）。

设计原则：
  - providers.json 为**单一真源**，界面选厂商自动填 Base URL 与模型名；
    修改该文件即可热更新厂商端点，无需改代码。
  - 内置 DEFAULT_PROVIDERS 仅作为「文件缺失/损坏」时的最终兜底，内容与
    providers.json 保持一致，避免重复维护（以 json 为准）。
"""

import os
import json

# providers.json 与本模块同目录
_PROVIDERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")

DEFAULT_PROVIDERS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "note": "deepseek.com 申请 key"},
    "阿里通义千问 Qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                          "model": "qwen-plus", "note": "阿里云百炼 dashscope"},
    "腾讯混元 Hunyuan": {"base_url": "https://api.hunyuan.cloud.tencent.com/v1",
                         "model": "hunyuan-turbo", "note": "腾讯云混元"},
    "字节火山方舟 Doubao": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",
                        "model": "doubao-seed-1.6-250615", "note": "火山方舟 Ark"},
    "智谱 GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "model": "glm-4-flash", "note": "智谱开放平台"},
    "月之暗面 Kimi": {"base_url": "https://api.moonshot.cn/v1",
                      "model": "moonshot-v1-8k", "note": "Moonshot / Kimi"},
    "百度文心 ERNIE": {"base_url": "https://qianfan.baidubce.com/v2",
                       "model": "ernie-4.5-8k-preview", "note": "百度智能云千帆"},
    "MiniMax": {"base_url": "https://api.minimax.chat/v1", "model": "abab6.5s-chat",
                "note": "MiniMax 开放平台"},
    "阶跃星辰 StepFun": {"base_url": "https://api.stepfun.com/v1", "model": "step-1-flash",
                         "note": "StepFun 开放平台"},
    "小米 MiMo": {"base_url": "https://api.mi.com/v1", "model": "mimo",
                  "note": "端点待官方确认，请按实际填写"},
    "自定义 Custom": {"base_url": "", "model": "", "note": "手动填写 Base URL 与模型名"},
}


def load_providers():
    """加载厂商预设：优先 providers.json，缺失/损坏则回退内置默认值。"""
    if os.path.exists(_PROVIDERS_PATH):
        try:
            with open(_PROVIDERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return dict(DEFAULT_PROVIDERS)
