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

    config = {item["name"]: item["default"] for item in manifest["spec"]["config"]}
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
    }


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
    ):
        assert command in readme
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
