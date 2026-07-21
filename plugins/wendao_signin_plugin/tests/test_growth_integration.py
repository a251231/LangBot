from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import components.growth_service as growth_service_module
from components.account_store import AccountStore
from components.account_session import ACCOUNT_NOT_BOUND_MESSAGE, AccountNotBoundError
from components.command_parser import ParsedCommand
from components.entitlement import EntitlementExpiredError
from components.growth_service import GrowthService
from components.growth_models import GrowthConfigRecord, GrowthOperation
from components.growth_store import (
    ENTITLEMENT_PREFIX,
    GROWTH_CONFIG_PREFIX,
    GROWTH_OPERATION_PREFIX,
    PRODUCT_PREFIX,
    PROMOTER_PREFIX,
    GrowthStore,
    identity_hash,
)
from components.models import AccountRecord, Credentials


PLUGIN_MAIN = Path(__file__).parents[1] / 'main.py'
SPEC = importlib.util.spec_from_file_location('wendao_growth_integration_main', PLUGIN_MAIN)
assert SPEC is not None and SPEC.loader is not None
MAIN_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAIN_MODULE)
WendaoSigninPlugin = MAIN_MODULE.WendaoSigninPlugin


class FakeStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_prefix_once = ''
        self.fail_prefix_after_matches: tuple[str, int] | None = None

    async def set_plugin_storage(self, key: str, value: bytes) -> None:
        if self.fail_prefix_once and key.startswith(self.fail_prefix_once):
            self.fail_prefix_once = ''
            raise RuntimeError('simulated growth integration interruption')
        if self.fail_prefix_after_matches is not None:
            prefix, remaining = self.fail_prefix_after_matches
            if key.startswith(prefix):
                if remaining == 0:
                    self.fail_prefix_after_matches = None
                    raise RuntimeError('simulated growth integration interruption')
                self.fail_prefix_after_matches = (prefix, remaining - 1)
        self.values[key] = value

    async def get_plugin_storage(self, key: str) -> bytes:
        return self.values[key]

    async def get_plugin_storage_keys(self) -> list[str]:
        return list(self.values)

    async def delete_plugin_storage(self, key: str) -> None:
        del self.values[key]


def account(bot_uuid: str, sender_id: str, user_identifier: str) -> AccountRecord:
    return AccountRecord.create(
        bot_uuid=bot_uuid,
        sender_id=sender_id,
        credentials=Credentials(
            token='token',
            device='device',
            version='version',
            version_code='1',
            guest_id='guest',
            client_type='client',
        ),
        target_id=f'target-{sender_id}',
        schedule_time='08:00',
        auto_signin=True,
        auto_resign=True,
        auto_milestone=True,
        user_identifier=user_identifier,
    )


def build_service(
    storage: FakeStorage | None = None,
    *,
    trial_days: int = 30,
    promoter_points: int = 100,
    invitee_points: int = 20,
) -> tuple[FakeStorage, AccountStore, GrowthService]:
    backend = storage or FakeStorage()
    account_store = AccountStore(backend)
    growth_store = GrowthStore(backend)
    service = GrowthService(
        growth_store,
        account_store,
        trial_days=trial_days,
        promoter_reward_points=promoter_points,
        invitee_reward_points=invitee_points,
    )
    return backend, account_store, service


def extract_invite_code(text: str) -> str:
    match = re.search(r'\b[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}\b', text)
    assert match is not None
    return match.group(0)


async def invoke_command(
    plugin: WendaoSigninPlugin,
    kind: str,
    argument: str = '',
) -> str:
    return await plugin.handle_wendao_command(
        bot_uuid='bot-a',
        sender_id='user-a',
        target_id='target-user-a',
        is_group=False,
        command=ParsedCommand(kind=kind, argument=argument),
    )


def test_expired_entitlement_blocks_only_manual_wendao_business_commands() -> None:
    async def scenario() -> None:
        backend = FakeStorage()
        plugin = WendaoSigninPlugin()
        plugin.account_store = AccountStore(backend)
        await plugin.account_store.save(account('bot-a', 'user-a', 'wd-a'))
        require_active = AsyncMock(
            side_effect=EntitlementExpiredError('插件使用期限已到期。')
        )
        plugin.entitlement_service = SimpleNamespace(require_active=require_active)
        workflow_execute = AsyncMock()
        weekly_execute = AsyncMock()
        plugin.workflow = SimpleNamespace(execute=workflow_execute)
        plugin.weekly_report = SimpleNamespace(execute=weekly_execute)

        replies = [
            await invoke_command(plugin, kind)
            for kind in ('query', 'signin', 'resign', 'weekly_report')
        ]

        assert all('已到期' in reply and '激活' in reply for reply in replies)
        assert require_active.await_count == 4
        workflow_execute.assert_not_awaited()
        weekly_execute.assert_not_awaited()

    asyncio.run(scenario())


def test_growth_and_shop_commands_remain_available_after_expiry() -> None:
    async def scenario() -> None:
        backend = FakeStorage()
        plugin = WendaoSigninPlugin()
        plugin.account_store = AccountStore(backend)
        await plugin.account_store.save(account('bot-a', 'user-a', 'wd-a'))
        require_active = AsyncMock(
            side_effect=EntitlementExpiredError('插件使用期限已到期。')
        )
        plugin.entitlement_service = SimpleNamespace(require_active=require_active)
        promotion = AsyncMock(return_value='推广入口可用')
        shop = AsyncMock(return_value='商城入口可用')
        plugin.growth_service = SimpleNamespace(promotion=promotion, shop=shop)

        promotion_reply = await invoke_command(plugin, 'promotion')
        shop_reply = await invoke_command(plugin, 'shop')

        assert promotion_reply == '推广入口可用'
        assert shop_reply == '商城入口可用'
        require_active.assert_not_awaited()
        promotion.assert_awaited_once_with('bot-a', 'user-a')
        shop.assert_awaited_once_with('bot-a')

    asyncio.run(scenario())


def test_manual_entitlement_read_error_returns_retry_message_without_business_call() -> None:
    async def scenario() -> None:
        backend = FakeStorage()
        plugin = WendaoSigninPlugin()
        plugin.account_store = AccountStore(backend)
        await plugin.account_store.save(account('bot-a', 'user-a', 'wd-a'))
        plugin.entitlement_service = SimpleNamespace(
            require_active=AsyncMock(
                side_effect=RuntimeError('simulated entitlement storage failure')
            )
        )
        workflow_execute = AsyncMock()
        plugin.workflow = SimpleNamespace(execute=workflow_execute)

        reply = await invoke_command(plugin, 'query')

        assert reply == '权益读取失败，请稍后重试。'
        workflow_execute.assert_not_awaited()

    asyncio.run(scenario())


def test_unbound_manual_commands_keep_existing_not_bound_message() -> None:
    async def scenario() -> None:
        plugin = WendaoSigninPlugin()
        plugin.account_store = AccountStore(FakeStorage())
        require_active = AsyncMock(
            side_effect=AssertionError('unbound account must bypass entitlement lookup')
        )
        plugin.entitlement_service = SimpleNamespace(require_active=require_active)
        workflow_execute = AsyncMock(
            side_effect=AccountNotBoundError(ACCOUNT_NOT_BOUND_MESSAGE)
        )
        weekly_execute = AsyncMock(
            side_effect=AccountNotBoundError(ACCOUNT_NOT_BOUND_MESSAGE)
        )
        plugin.workflow = SimpleNamespace(execute=workflow_execute)
        plugin.weekly_report = SimpleNamespace(execute=weekly_execute)

        replies = [
            await invoke_command(plugin, kind)
            for kind in ('query', 'signin', 'resign', 'weekly_report')
        ]

        assert replies == [ACCOUNT_NOT_BOUND_MESSAGE] * 4
        require_active.assert_not_awaited()
        assert workflow_execute.await_count == 3
        weekly_execute.assert_awaited_once()

    asyncio.run(scenario())


def test_initialize_rolls_out_existing_accounts_and_persists_initial_config() -> None:
    async def scenario() -> None:
        storage, accounts, service = build_service()
        existing = account('bot-a', 'user-a', 'wd-a')
        await accounts.save(existing)

        await service.initialize(at='2026-07-20T00:00:00+08:00')
        entitlement = await service.entitlement(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )
        promotion = await service.promotion('bot-a', 'user-a')

        assert '2026-08-19' in entitlement
        assert extract_invite_code(promotion)

        _, restarted_accounts, restarted = build_service(
            storage,
            trial_days=90,
            promoter_points=999,
            invitee_points=999,
        )
        newcomer = account('bot-a', 'user-b', 'wd-b')
        await restarted_accounts.save(newcomer)
        await restarted.on_account_bound(
            newcomer,
            at='2026-08-01T00:00:00+08:00',
        )
        status = await restarted.entitlement(
            'bot-a',
            'user-b',
            now='2026-08-02T00:00:00+08:00',
        )

        assert '2026-08-31' in status

    asyncio.run(scenario())


def test_referral_binding_and_first_signin_use_persisted_reward_rules() -> None:
    async def scenario() -> None:
        _, accounts, service = build_service()
        promoter = account('bot-a', 'promoter', 'wd-promoter')
        await accounts.save(promoter)
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        code = extract_invite_code(await service.promotion('bot-a', 'promoter'))

        bound = await service.bind_referral(
            'bot-a',
            'invitee',
            code,
            already_bound=False,
            at='2026-07-21T00:00:00+08:00',
        )
        invitee = account('bot-a', 'invitee', 'wd-invitee')
        await accounts.save(invitee)
        await service.on_account_bound(invitee, at='2026-07-21T01:00:00+08:00')
        await service.on_signin_confirmed(
            invitee,
            at='2026-07-22T00:00:00+08:00',
        )

        assert '已登记' in bound
        assert '100' in await service.points('bot-a', 'promoter')
        assert '20' in await service.points('bot-a', 'invitee')

    asyncio.run(scenario())


def test_promotion_requires_binding_and_referral_uses_stored_binding_state() -> None:
    async def scenario() -> None:
        _, accounts, service = build_service()

        unbound = await service.promotion('bot-a', 'unbound-user')
        unbound_entitlement = await service.entitlement('bot-a', 'unbound-user')
        invalid_invite = await service.bind_referral(
            'bot-a',
            'unbound-user',
            'INVALID',
        )
        assert '先绑定' in unbound
        assert '先绑定' in unbound_entitlement
        assert '登记失败' in invalid_invite
        assert not await service.store.has_prefix(f'{PROMOTER_PREFIX}bot-a:')

        promoter = account('bot-a', 'promoter', 'wd-promoter')
        already_bound = account('bot-a', 'already-bound', 'wd-bound')
        await accounts.save(promoter)
        await accounts.save(already_bound)
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        code = extract_invite_code(await service.promotion('bot-a', 'promoter'))

        rejected = await service.bind_referral(
            'bot-a',
            'already-bound',
            code,
            already_bound=False,
        )

        assert '已绑定' in rejected

    asyncio.run(scenario())


def test_admin_commerce_flow_and_cross_user_activation() -> None:
    async def scenario() -> None:
        _, accounts, service = build_service()
        buyer = account('bot-a', 'buyer', 'wd-buyer')
        recipient = account('bot-a', 'recipient', 'wd-recipient')
        await accounts.save(buyer)
        await accounts.save(recipient)
        await service.initialize(at='2026-07-20T00:00:00+08:00')

        created = await service.admin(
            'bot-a',
            '商品新增 30 天使用期限 500 30',
            request_id='admin-product-1',
        )
        assert 'P000001' in created
        await service.admin(
            'bot-a',
            '商品上架 P000001',
            request_id='admin-enable-1',
        )
        await service.admin(
            'bot-a',
            '库存增加 P000001 1',
            request_id='admin-stock-1',
        )
        await service.admin(
            'bot-a',
            '积分调整 buyer 1000 测试发放',
            request_id='admin-points-1',
        )

        shop = await service.shop('bot-a')
        redeemed = await service.redeem(
            'bot-a',
            'buyer',
            'P000001',
            request_id='redeem-1',
        )
        card_code = re.search(r'WD-[A-Za-z0-9_-]{43}', redeemed)
        assert card_code is not None
        pending_redemptions = await service.redemptions('bot-a', 'buyer')
        other_redemptions = await service.redemptions('bot-a', 'recipient')
        activated = await service.activate(
            'bot-a',
            'recipient',
            card_code.group(0),
            at='2026-07-21T00:00:00+08:00',
        )

        assert 'P000001' in shop and '库存：1' in shop
        assert '500' in await service.points('bot-a', 'buyer')
        assert '激活成功' in activated
        assert '2026-09-18' in activated
        assert card_code.group(0) in pending_redemptions
        assert card_code.group(0) not in other_redemptions
        assert card_code.group(0) not in await service.shop('bot-a')
        assert card_code.group(0) not in await service.admin('bot-a', '统计')
        assert card_code.group(0) not in await service.redemptions('bot-a', 'buyer')

    asyncio.run(scenario())


def test_runtime_reward_rules_persist_and_only_affect_later_referrals() -> None:
    async def scenario() -> None:
        storage, accounts, service = build_service()
        promoter = account('bot-a', 'promoter', 'wd-promoter')
        await accounts.save(promoter)
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        code = extract_invite_code(await service.promotion('bot-a', 'promoter'))
        first = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='admin-rules-1',
        )
        replay = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='admin-rules-1',
        )

        _, restarted_accounts, restarted = build_service(storage)
        await restarted.bind_referral(
            'bot-a',
            'invitee',
            code,
            already_bound=False,
        )
        invitee = account('bot-a', 'invitee', 'wd-invitee')
        await restarted_accounts.save(invitee)
        await restarted.on_account_bound(invitee)
        await restarted.on_signin_confirmed(invitee)

        assert '150' in await restarted.points('bot-a', 'promoter')
        assert '30' in await restarted.points('bot-a', 'invitee')
        assert replay == first

    asyncio.run(scenario())


def test_unbind_keeps_growth_balance_and_entitlement() -> None:
    async def scenario() -> None:
        _, accounts, service = build_service()
        record = account('bot-a', 'user-a', 'wd-a')
        await accounts.save(record)
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        await service.admin(
            'bot-a',
            '积分调整 user-a 200 保留测试',
            request_id='admin-points-1',
        )

        await accounts.delete('bot-a', 'user-a')

        assert '200' in await service.points('bot-a', 'user-a')
        assert '2026-08-19' in await service.entitlement(
            'bot-a',
            'user-a',
            now='2026-07-21T00:00:00+08:00',
        )

    asyncio.run(scenario())


def test_entitlement_distinguishes_missing_record_from_corrupt_storage() -> None:
    async def scenario() -> None:
        storage, _, service = build_service()

        missing = await service.entitlement('bot-a', 'missing-user')

        sender_id = 'private-corrupt-user'
        identity = identity_hash('bot-a', sender_id)
        storage.values[f'{ENTITLEMENT_PREFIX}bot-a:{identity}'] = (
            b'{"private-corrupt-user":not-json}'
        )
        _, _, restarted = build_service(storage)
        corrupt = await restarted.entitlement('bot-a', sender_id)

        assert missing == '尚未获得问道权益，请先绑定问道账号。'
        assert corrupt == '权益读取失败，请稍后重试。'
        assert sender_id not in corrupt
        assert 'JSON' not in corrupt
        assert 'Expecting' not in corrupt

    asyncio.run(scenario())


def test_mutating_growth_commands_require_request_id() -> None:
    async def scenario() -> None:
        _, _, service = build_service()

        with pytest.raises(ValueError, match='请求 ID'):
            await service.admin('bot-a', '商品新增 test 1 1', request_id='')
        with pytest.raises(ValueError, match='请求 ID'):
            await service.redeem(
                'bot-a',
                'user-a',
                'P000001',
                request_id='',
            )

    asyncio.run(scenario())


def test_admin_product_creation_request_replay_does_not_duplicate_product() -> None:
    async def scenario() -> None:
        _, _, service = build_service()

        first = await service.admin(
            'bot-a',
            '商品新增 30 天 使用期限 500 30',
            request_id='admin-product-1',
        )
        replay = await service.admin(
            'bot-a',
            '商品新增 30 天 使用期限 500 30',
            request_id='admin-product-1',
        )
        products = await service.admin('bot-a', '商品列表')

        assert first == replay
        assert first.count('P000001') == 1
        assert products.count('P000001') == 1
        assert 'P000002' not in products

    asyncio.run(scenario())


def test_old_admin_request_replay_does_not_overwrite_later_state() -> None:
    async def scenario() -> None:
        _, accounts, service = build_service()
        promoter = account('bot-a', 'promoter', 'wd-promoter')
        await accounts.save(promoter)
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        code = extract_invite_code(await service.promotion('bot-a', 'promoter'))
        first_create = await service.admin(
            'bot-a',
            '商品新增 30 天使用期限 500 30',
            request_id='product-create-1',
        )
        first_enable = await service.admin(
            'bot-a',
            '商品上架 P000001',
            request_id='product-state-1',
        )
        await service.admin(
            'bot-a',
            '商品下架 P000001',
            request_id='product-state-2',
        )
        replay_enable = await service.admin(
            'bot-a',
            '商品上架 P000001',
            request_id='product-state-1',
        )
        first_rules = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='reward-rules-1',
        )
        await service.admin(
            'bot-a',
            '积分规则 200 40',
            request_id='reward-rules-2',
        )
        replay_rules = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='reward-rules-1',
        )
        replay_create = await service.admin(
            'bot-a',
            '商品新增 30 天使用期限 500 30',
            request_id='product-create-1',
        )

        await service.bind_referral('bot-a', 'invitee', code)
        invitee = account('bot-a', 'invitee', 'wd-invitee')
        await accounts.save(invitee)
        await service.on_account_bound(invitee)
        await service.on_signin_confirmed(invitee)
        products = await service.admin('bot-a', '商品列表')

        assert replay_enable == first_enable
        assert replay_create == first_create
        assert '下架' in products
        assert replay_rules == first_rules
        assert '200' in await service.points('bot-a', 'promoter')
        assert '40' in await service.points('bot-a', 'invitee')

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'command',
    (
        '商品上架 invalid',
        '商品下架 P999999',
    ),
)
def test_invalid_admin_product_state_writes_no_pending_operation(
    command: str,
) -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        before = dict(storage.values)

        response = await service.admin(
            'bot-a',
            command,
            request_id='invalid-product-state',
        )

        assert response.startswith('管理操作失败：')
        assert storage.values == before
        assert (await service.store.list_pending_operations('bot-a')).records == ()

        _, _, restarted = build_service(storage)
        await restarted.initialize(at='2026-07-20T00:01:00+08:00')

    asyncio.run(scenario())


def test_recover_pending_reward_rules_uses_created_at_order() -> None:
    async def scenario() -> None:
        _, _, service = build_service()
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        old_id = 'admin-reward-rules:' + hashlib.sha256(
            b'state-old'
        ).hexdigest()
        new_id = 'admin-reward-rules:' + hashlib.sha256(
            b'state-new'
        ).hexdigest()
        await service.store.begin_operation(
            'bot-a',
            old_id,
            'admin-reward-rules',
            {'promoter_reward_points': 150, 'invitee_reward_points': 30},
            '2026-07-20T00:01:00+08:00',
        )
        await service.store.begin_operation(
            'bot-a',
            new_id,
            'admin-reward-rules',
            {'promoter_reward_points': 200, 'invitee_reward_points': 40},
            '2026-07-20T00:02:00+08:00',
        )

        await service.recover_pending()

        config = await service._config('bot-a')
        assert config.promoter_reward_points == 200
        assert config.invitee_reward_points == 40

    asyncio.run(scenario())


def test_legacy_default_reward_rules_pending_persists_missing_config() -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        request_id = 'legacy-default-rules'
        operation_id = 'admin-reward-rules:' + hashlib.sha256(
            request_id.encode()
        ).hexdigest()
        await service.store.begin_operation(
            'bot-a',
            operation_id,
            'admin-reward-rules',
            {'promoter_reward_points': 100, 'invitee_reward_points': 20},
            '2026-07-20T00:01:00+08:00',
        )

        await service.recover_pending()

        config_key = f'{GROWTH_CONFIG_PREFIX}bot-a:runtime'
        persisted = await service.store.get(config_key, GrowthConfigRecord)
        operation = await service.store.get_operation('bot-a', operation_id)
        assert persisted is not None
        assert persisted.promoter_reward_points == 100
        assert persisted.invitee_reward_points == 20
        assert operation is not None
        assert operation.status == 'COMMITTED'
        assert operation.applied_steps == ('target-saved',)

        _, _, restarted = build_service(storage)
        restored = await restarted._config('bot-a')
        assert restored == persisted

    asyncio.run(scenario())


def test_reward_rules_applied_step_without_config_fact_is_rejected() -> None:
    async def scenario() -> None:
        _, _, service = build_service()
        operation_id = 'admin-reward-rules:' + hashlib.sha256(
            b'missing-config-fact'
        ).hexdigest()
        await service.store.begin_operation(
            'bot-a',
            operation_id,
            'admin-reward-rules',
            {'promoter_reward_points': 100, 'invitee_reward_points': 20},
            '2026-07-20T00:01:00+08:00',
        )
        pending = await service.store.mark_step_applied(
            'bot-a',
            operation_id,
            'target-saved',
            '2026-07-20T00:01:00+08:00',
        )

        with pytest.raises(ValueError, match='步骤与配置不一致'):
            await service.recover_pending()

        config = await service.store.get(
            f'{GROWTH_CONFIG_PREFIX}bot-a:runtime',
            GrowthConfigRecord,
        )
        assert config is None
        assert await service.store.get_operation('bot-a', operation_id) == pending

    asyncio.run(scenario())


def test_recover_old_pending_reward_rules_does_not_overwrite_newer_fact() -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        old_id = 'admin-reward-rules:' + hashlib.sha256(
            b'state-old'
        ).hexdigest()
        new_id = 'admin-reward-rules:' + hashlib.sha256(
            b'state-new'
        ).hexdigest()
        old = await service.store.begin_operation(
            'bot-a',
            old_id,
            'admin-reward-rules',
            {'promoter_reward_points': 150, 'invitee_reward_points': 30},
            '2026-07-20T00:01:00+08:00',
        )
        current = await service._config('bot-a')
        await service.store.save(
            f'{GROWTH_CONFIG_PREFIX}bot-a:runtime',
            replace(
                current,
                promoter_reward_points=150,
                invitee_reward_points=30,
                updated_at='2026-07-20T00:01:00+08:00',
            ),
        )
        await service.store.begin_operation(
            'bot-a',
            new_id,
            'admin-reward-rules',
            {'promoter_reward_points': 200, 'invitee_reward_points': 40},
            '2026-07-20T00:02:00+08:00',
        )
        await service.store.save(
            f'{GROWTH_CONFIG_PREFIX}bot-a:runtime',
            replace(
                current,
                promoter_reward_points=200,
                invitee_reward_points=40,
                updated_at='2026-07-20T00:02:00+08:00',
            ),
        )
        await service.store.mark_step_applied(
            'bot-a',
            new_id,
            'target-saved',
            '2026-07-20T00:02:00+08:00',
        )
        await service.store.commit_operation(
            'bot-a',
            new_id,
            '2026-07-20T00:02:00+08:00',
        )

        _, _, restarted = build_service(storage)
        await restarted.recover_pending()

        config = await restarted._config('bot-a')
        recovered = await restarted.store.get_operation('bot-a', old.operation_id)
        assert config.promoter_reward_points == 200
        assert config.invitee_reward_points == 40
        assert recovered is not None
        assert recovered.status == 'COMMITTED'
        assert recovered.applied_steps == ('target-saved',)

    asyncio.run(scenario())


def test_reward_rules_equal_time_conflict_fails_and_new_times_are_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        timestamp = '2026-07-20T00:00:00+08:00'
        _, _, service = build_service()
        original = await service._config('bot-a', at=timestamp)
        conflict_id = 'admin-reward-rules:' + hashlib.sha256(
            b'equal-time-conflict'
        ).hexdigest()
        conflict = await service.store.begin_operation(
            'bot-a',
            conflict_id,
            'admin-reward-rules',
            {'promoter_reward_points': 150, 'invitee_reward_points': 30},
            timestamp,
        )

        with pytest.raises(ValueError, match='时间冲突'):
            await service.recover_pending()

        assert await service._config('bot-a') == original
        assert await service.store.get_operation('bot-a', conflict_id) == conflict

        _, _, monotonic = build_service()
        await monotonic._config('bot-a', at=timestamp)
        candidates = iter(
            (
                timestamp,
                '2026-07-19T23:59:59+08:00',
            )
        )
        monkeypatch.setattr(
            growth_service_module,
            '_now_iso',
            lambda: next(candidates),
        )

        await monotonic.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='monotonic-first',
        )
        first_id = 'admin-reward-rules:' + hashlib.sha256(
            b'monotonic-first'
        ).hexdigest()
        first_operation = await monotonic.store.get_operation('bot-a', first_id)
        assert first_operation is not None
        assert datetime.fromisoformat(first_operation.created_at) > (
            datetime.fromisoformat(timestamp)
        )

        await monotonic.admin(
            'bot-a',
            '积分规则 200 40',
            request_id='monotonic-second',
        )
        second_id = 'admin-reward-rules:' + hashlib.sha256(
            b'monotonic-second'
        ).hexdigest()
        second_operation = await monotonic.store.get_operation('bot-a', second_id)
        assert second_operation is not None
        assert datetime.fromisoformat(second_operation.created_at) > (
            datetime.fromisoformat(first_operation.created_at)
        )
        current = await monotonic._config('bot-a')
        assert current.promoter_reward_points == 200
        assert current.invitee_reward_points == 40

        replay = await monotonic.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='monotonic-first',
        )
        replayed_operation = await monotonic.store.get_operation('bot-a', first_id)
        assert '150' in replay
        assert replayed_operation == first_operation
        assert await monotonic._config('bot-a') == current

    asyncio.run(scenario())


def test_new_reward_rules_request_recovers_older_pending_before_commit() -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service._config('bot-a', at='2026-07-20T00:00:00+08:00')
        storage.fail_prefix_after_matches = (
            f'{GROWTH_OPERATION_PREFIX}bot-a:',
            1,
        )

        with pytest.raises(RuntimeError, match='interruption'):
            await service.admin(
                'bot-a',
                '积分规则 150 30',
                request_id='state-old',
            )

        result = await service.admin(
            'bot-a',
            '积分规则 200 40',
            request_id='state-new',
        )
        replay = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='state-old',
        )

        assert '200' in result
        assert '150' in replay
        config = await service._config('bot-a')
        assert config.promoter_reward_points == 200
        assert config.invitee_reward_points == 40
        assert (await service.store.list_pending_operations('bot-a')).records == ()

    asyncio.run(scenario())


def test_committed_reward_rules_without_matching_fact_is_diagnostic() -> None:
    async def scenario() -> None:
        _, _, service = build_service()
        config = await service._config(
            'bot-a',
            at='2026-07-20T00:00:00+08:00',
        )
        request_id = 'committed-corrupt-rules'
        operation_id = 'admin-reward-rules:' + hashlib.sha256(
            request_id.encode()
        ).hexdigest()
        operation = GrowthOperation(
            bot_uuid='bot-a',
            operation_id=operation_id,
            kind='admin-reward-rules',
            status='COMMITTED',
            payload={
                'promoter_reward_points': 150,
                'invitee_reward_points': 30,
            },
            applied_steps=('target-saved',),
            created_at='2026-07-20T00:01:00+08:00',
            updated_at='2026-07-20T00:01:00+08:00',
        )
        await service.store.save(
            f'{GROWTH_OPERATION_PREFIX}bot-a:{operation_id}',
            operation,
        )

        response = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id=request_id,
        )

        assert response.startswith('管理操作失败：')
        assert await service._config('bot-a') == config

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('operation_id', 'status', 'created_at', 'updated_at', 'applied_steps'),
    (
        (
            'admin-reward-rules:not-a-canonical-hash',
            'PENDING',
            '2026-07-20T00:01:00+08:00',
            '2026-07-20T00:01:00+08:00',
            [],
        ),
        (
            'admin-reward-rules:' + 'b' * 64,
            'PENDING',
            '2026-07-20T00:01:00',
            '2026-07-20T00:01:00',
            [],
        ),
        (
            'admin-reward-rules:' + 'c' * 64,
            'PENDING',
            '2026-07-20T00:02:00+08:00',
            '2026-07-20T00:01:00+08:00',
            [],
        ),
        (
            'admin-reward-rules:' + 'd' * 64,
            'PENDING',
            '2026-07-20T00:01:00+08:00',
            '2026-07-20T00:01:00+08:00',
            ['unexpected-step'],
        ),
    ),
)
def test_invalid_pending_reward_rules_remains_diagnostic_without_fact_change(
    operation_id: str,
    status: str,
    created_at: str,
    updated_at: str,
    applied_steps: list[str],
) -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        await service._config('bot-a', at='2026-07-20T00:00:00+08:00')
        config_key = f'{GROWTH_CONFIG_PREFIX}bot-a:runtime'
        before = storage.values[config_key]
        key = f'{GROWTH_OPERATION_PREFIX}bot-a:{operation_id}'
        storage.values[key] = json.dumps(
            {
                'schema_version': 1,
                'bot_uuid': 'bot-a',
                'operation_id': operation_id,
                'kind': 'admin-reward-rules',
                'status': status,
                'payload': {
                    'promoter_reward_points': 150,
                    'invitee_reward_points': 30,
                },
                'applied_steps': applied_steps,
                'created_at': created_at,
                'updated_at': updated_at,
            },
            separators=(',', ':'),
        ).encode()

        _, _, restarted = build_service(storage)
        with pytest.raises(ValueError):
            await restarted.initialize(at='2026-07-20T00:03:00+08:00')

        assert storage.values[config_key] == before
        assert key in storage.values

    asyncio.run(scenario())


def test_broken_operation_status_blocks_initialize_without_fact_change() -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service.initialize(at='2026-07-20T00:00:00+08:00')
        await service._config('bot-a', at='2026-07-20T00:00:00+08:00')
        config_key = f'{GROWTH_CONFIG_PREFIX}bot-a:runtime'
        before = storage.values[config_key]
        operation_id = 'admin-reward-rules:' + 'e' * 64
        storage.values[f'{GROWTH_OPERATION_PREFIX}bot-a:{operation_id}'] = (
            json.dumps(
                {
                    'schema_version': 1,
                    'bot_uuid': 'bot-a',
                    'operation_id': operation_id,
                    'kind': 'admin-reward-rules',
                    'status': 'BROKEN',
                    'payload': {
                        'promoter_reward_points': 150,
                        'invitee_reward_points': 30,
                    },
                    'applied_steps': [],
                    'created_at': '2026-07-20T00:01:00+08:00',
                    'updated_at': '2026-07-20T00:01:00+08:00',
                },
                separators=(',', ':'),
            ).encode()
        )

        _, _, restarted = build_service(storage)
        with pytest.raises(ValueError, match='损坏'):
            await restarted.initialize(at='2026-07-20T00:03:00+08:00')

        assert storage.values[config_key] == before

    asyncio.run(scenario())


def test_corrupt_runtime_config_fails_before_reward_operation_begins() -> None:
    async def scenario() -> None:
        storage = FakeStorage()
        config_key = f'{GROWTH_CONFIG_PREFIX}bot-a:runtime'
        storage.values[config_key] = json.dumps(
            {
                'schema_version': 1,
                'bot_uuid': 'bot-a',
                'config_id': 'runtime',
                'trial_days': 30,
                'promoter_reward_points': 100,
                'invitee_reward_points': 20,
                'updated_at': 'not-a-time',
            },
            separators=(',', ':'),
        ).encode()
        before = storage.values[config_key]
        _, _, service = build_service(storage)

        with pytest.raises(ValueError, match='配置时间') as captured:
            await service.initialize()

        assert 'Invalid isoformat' not in str(captured.value)
        assert service._configs == {}

        response = await service.admin(
            'bot-a',
            '积分规则 150 30',
            request_id='corrupt-config-rules',
        )

        assert response.startswith('管理操作失败：')
        assert '配置时间' in response
        assert 'Invalid isoformat' not in response
        assert service._configs == {}
        assert (await service.store.list_pending_operations('bot-a')).records == ()
        assert storage.values[config_key] == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("command", "operation_kind", "fact_prefix"),
    (
        ("商品上架 P000001", "admin-product-enabled", PRODUCT_PREFIX),
        ("积分规则 150 30", "admin-reward-rules", GROWTH_CONFIG_PREFIX),
    ),
)
def test_admin_fact_write_failure_leaves_pending_operation_for_startup_recovery(
    command: str,
    operation_kind: str,
    fact_prefix: str,
) -> None:
    async def scenario() -> None:
        storage, _, service = build_service()
        await service.initialize(at="2026-07-20T00:00:00+08:00")
        if operation_kind == "admin-product-enabled":
            await service.admin(
                "bot-a",
                "商品新增 30 天使用期限 500 30",
                request_id="product-create-1",
            )
        else:
            await service._config(
                "bot-a",
                at="2026-07-20T00:00:00+08:00",
            )

        storage.fail_prefix_once = f"{fact_prefix}bot-a:"
        with pytest.raises(RuntimeError, match="simulated"):
            await service.admin("bot-a", command, request_id="interrupted-admin-1")

        [pending] = (await service.store.list_pending_operations("bot-a")).records
        assert pending.kind == operation_kind
        assert pending.applied_steps == ()

        _, _, restarted = build_service(storage)
        await restarted.recover_pending()

        operation = await restarted.store.get_operation(
            "bot-a",
            pending.operation_id,
        )
        assert operation is not None
        assert operation.status == "COMMITTED"
        assert operation.applied_steps == ("target-saved",)
        if operation_kind == "admin-product-enabled":
            assert "上架" in await restarted.admin("bot-a", "商品列表")
        else:
            config = await restarted._config("bot-a")
            assert config.promoter_reward_points == 150
            assert config.invitee_reward_points == 30

    asyncio.run(scenario())


def test_recover_pending_discovers_orphan_bot_and_replays_plain_points() -> None:
    async def scenario() -> None:
        _, _, service = build_service()
        target_hash = identity_hash('orphan-bot', 'detached-user')
        operation = GrowthOperation(
            bot_uuid='orphan-bot',
            operation_id='admin-adjustment:' + 'a' * 64,
            kind='points',
            status='PENDING',
            payload={
                'changes': [
                    {
                        'identity_hash': target_hash,
                        'amount': 25,
                        'entry_type': 'admin_adjustment',
                        'reason': '恢复测试',
                        'step_id': f'adjust:{target_hash}',
                    }
                ]
            },
            applied_steps=(),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await service.store.save(
            'growth-op:v1:orphan-bot:' + operation.operation_id,
            operation,
        )

        await service.recover_pending()

        assert '25' in await service.points('orphan-bot', 'detached-user')
        assert (
            await service.store.get_operation(
                'orphan-bot',
                operation.operation_id,
            )
        ).status == 'COMMITTED'

    asyncio.run(scenario())


def test_recover_pending_rejects_unknown_and_corrupt_operations() -> None:
    async def scenario() -> None:
        _, _, service = build_service()
        unknown = GrowthOperation(
            bot_uuid='orphan-bot',
            operation_id='unknown:' + 'b' * 64,
            kind='unknown',
            status='PENDING',
            payload={},
            applied_steps=(),
            created_at='2026-07-20T00:00:00+08:00',
            updated_at='2026-07-20T00:00:00+08:00',
        )
        await service.store.save(
            'growth-op:v1:orphan-bot:' + unknown.operation_id,
            unknown,
        )

        with pytest.raises(ValueError, match='未知'):
            await service.recover_pending()

        storage, _, corrupt_service = build_service()
        storage.values['growth-op:v1:corrupt-bot:broken'] = b'{not-json'
        with pytest.raises(ValueError, match='损坏'):
            await corrupt_service.recover_pending()

    asyncio.run(scenario())
