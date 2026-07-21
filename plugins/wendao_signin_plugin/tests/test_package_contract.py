from __future__ import annotations

from pathlib import Path

import yaml


PLUGIN_ROOT = Path(__file__).parents[1]


def test_manifest_declares_langbot_plugin_and_expected_defaults() -> None:
    manifest = yaml.safe_load((PLUGIN_ROOT / "manifest.yaml").read_text(encoding="utf-8"))

    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "Plugin"
    assert manifest["metadata"]["author"] == "local"
    assert manifest["metadata"]["name"] == "wendao_signin_plugin"
    assert manifest["metadata"]["version"] == "0.4.3"
    assert manifest["metadata"]["repository"] == "https://github.com/langbot-app/LangBot"
    assert manifest["metadata"]["icon"] == "assets/icon.svg"
    assert manifest["execution"]["python"] == {
        "path": "main.py",
        "attr": "WendaoSigninPlugin",
    }
    assert manifest["spec"]["components"]["EventListener"]["fromDirs"] == [
        {"path": "components/event_listeners/"}
    ]

    config_items = {item["name"]: item for item in manifest["spec"]["config"]}
    config = {name: item["default"] for name, item in config_items.items()}
    assert config == {
        "timezone": "Asia/Shanghai",
        "default_schedule_time": "08:00",
        "request_timeout_seconds": 15,
        "scheduler_poll_seconds": 30,
        "max_concurrency": 3,
        "login_session_ttl_seconds": 600,
        "captcha_public_base_url": "",
        "captcha_bind_host": "0.0.0.0",
        "captcha_bind_port": 8788,
        "default_auto_signin": True,
        "default_auto_resign": True,
        "default_auto_milestone": True,
        "default_auto_weekly_report": True,
        "admin_user_ids": "",
        "growth_trial_days": 30,
        "promoter_reward_points": 100,
        "invitee_reward_points": 20,
    }

    growth_config_types = {
        "admin_user_ids": "string",
        "growth_trial_days": "integer",
        "promoter_reward_points": "integer",
        "invitee_reward_points": "integer",
    }
    for name, expected_type in growth_config_types.items():
        item = config_items[name]
        assert item["type"] == expected_type
        assert item["required"] is False
        assert set(item["label"]) == {"en_US", "zh_Hans"}
        assert set(item["description"]) == {"en_US", "zh_Hans"}
        assert all(value.strip() for value in item["label"].values())
        assert all(value.strip() for value in item["description"].values())


def test_event_listener_component_points_to_real_python_class() -> None:
    component_path = PLUGIN_ROOT / "components" / "event_listeners" / "wendao_signin_listener.yaml"
    component = yaml.safe_load(component_path.read_text(encoding="utf-8"))

    assert component["kind"] == "EventListener"
    assert component["execution"]["python"] == {
        "path": "wendao_signin_listener.py",
        "attr": "WendaoSigninListener",
    }
    assert (component_path.parent / component["execution"]["python"]["path"]).is_file()


def test_runtime_dependencies_and_documentation_are_complete() -> None:
    requirements = [
        line.strip()
        for line in (PLUGIN_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements == [
        "httpx>=0.27,<1",
        "cryptography>=43,<47",
        "aiohttp>=3.9,<4",
    ]

    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "问道登录 <手机号>",
        "问道验证码 <短信码>",
        "问道验证 <randstr> <ticket>",
        "问道绑定 <登录响应>",
        "问道查询",
        "问道签到",
        "问道补签",
        "问道时间 HH:MM",
        "问道自动 开/关",
        "问道自动补签 开/关",
        "问道自动里程碑 开/关",
        "问道周报",
        "问道自动周报 开/关",
        "问道设置",
        "问道解绑",
        "问道帮助",
        "问道推广",
        "问道邀请 <邀请码>",
        "问道积分",
        "问道商城",
        "问道兑换 <商品ID>",
        "问道兑换记录",
        "问道激活 <卡密>",
        "问道权益",
    ):
        assert command in readme
    for command in (
        "问道管理 商品新增 <名称> <积分> <天数>",
        "问道管理 商品上架 <商品ID>",
        "问道管理 商品下架 <商品ID>",
        "问道管理 库存增加 <商品ID> <数量>",
        "问道管理 商品列表",
        "问道管理 积分规则 <推广人积分> <受邀人积分>",
        "问道管理 积分调整 <用户ID> <数量> <原因>",
        "问道管理 统计",
    ):
        assert command in readme
    for config_name in (
        "admin_user_ids",
        "growth_trial_days",
        "promoter_reward_points",
        "invitee_reward_points",
    ):
        assert config_name in readme
    assert "数据库备份可直接读取 token" in readme
    assert "删除聊天记录中的绑定消息" in readme
    assert "2211133C/Android/16" in readme
    assert "1030620167932793" in readme
    assert "accessTokenValidTime" in readme
    assert "兼容旧版 curl" in readme
    assert "每周一 `09:00`" in readme
    assert "不自动领取分享奖励" in readme
    assert "活动 token 不会写入插件存储" in readme
    assert "1 年按 360 天换算" in readme
    assert "手机号、ticket、randstr 和短信验证码不会写入插件存储" in readme
    assert "guestId" in readme and "动态生成" in readme
    assert "captcha_public_base_url" in readme
    assert "http://服务器IP:8788" in readme
    assert "一次性腾讯验证码网址" in readme
    assert "自动发送短信验证码" in readme
    assert "直接回复短信中的验证码" in readme
    assert "高级兼容入口" in readme
    assert "Docker" in readme and "8788" in readme
    assert (PLUGIN_ROOT / "assets" / "icon.svg").is_file()


def test_growth_modules_and_operational_boundaries_are_documented() -> None:
    for module_name in (
        "growth_models.py",
        "growth_store.py",
        "points.py",
        "referral.py",
        "commerce.py",
        "entitlement.py",
        "growth_service.py",
    ):
        assert (PLUGIN_ROOT / "components" / module_name).is_file()

    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for lifecycle_term in (
        "rollout_at",
        "首次绑定时间",
        "重复绑定不重置",
        "到期只暂停",
        "推广关系、积分、兑换记录和权益",
    ):
        assert lifecycle_term in readme
    for security_term in (
        "卡密是敏感凭据",
        "未激活",
        "激活后不再回显明文",
        "可转赠",
        "只能激活一次",
        "增长密钥",
        "插件存储",
        "备份",
    ):
        assert security_term in readme
    for capacity_term in (
        "单进程",
        "asyncio.Lock",
        "多实例",
        "竞态",
        "1 万用户",
        "5 万个增长存储键",
        "逐账号读取权益",
        "轮询时延",
    ):
        assert capacity_term in readme


def test_growth_config_persistence_is_documented() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "`admin_user_ids` 在每次插件初始化时读取" in readme
    for config_name in (
        "`growth_trial_days`",
        "`promoter_reward_points`",
        "`invitee_reward_points`",
    ):
        assert config_name in readme
    assert "仅在每个 bot 首次创建持久化 `GrowthConfig` 时写入初值" in readme
    assert "后续修改 manifest 不会覆盖已有 `GrowthConfig`" in readme
    assert "既有积分规则" in readme
    assert "既有权益" in readme
    assert "`问道管理 积分规则`" in readme
    assert "只影响之后生效的邀请关系" in readme


def test_card_activation_and_backup_preconditions_are_documented() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "卡密接收人在激活前需至少完成过一次问道账号绑定" in readme
    assert "从未绑定的用户激活会失败，卡密保持未激活且不会被消耗" in readme
    assert (
        "备份必须让增长密钥（卡密加密密钥）与对应插件存储数据"
        "保持同一备份时间点。"
    ) in readme
