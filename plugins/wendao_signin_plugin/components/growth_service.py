from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from components.account_store import AccountStore
from components.commerce import CommerceService
from components.entitlement import (
    EntitlementNotFoundError,
    EntitlementService,
    EntitlementStorageError,
)
from components.growth_models import (
    CardRecord,
    GrowthConfigRecord,
    GrowthOperation,
    PointAccount,
    RedemptionRecord,
    ReferralRecord,
)
from components.growth_store import (
    CARD_PREFIX,
    GROWTH_CONFIG_PREFIX,
    POINT_ACCOUNT_PREFIX,
    REDEMPTION_PREFIX,
    REFERRAL_PREFIX,
    GrowthStore,
    growth_storage_key,
    identity_hash,
    strictly_later_timestamp,
)
from components.models import AccountRecord
from components.points import PointService
from components.referral import ReferralService


_CONFIG_ID = 'runtime'
_MAX_REWARD_POINTS = 1_000_000
_ADMIN_MUTATIONS = frozenset(
    {
        '商品新增',
        '商品上架',
        '商品下架',
        '库存增加',
        '积分规则',
        '积分调整',
    }
)


class GrowthService:
    def __init__(
        self,
        store: GrowthStore,
        account_store: AccountStore,
        *,
        trial_days: int = 30,
        promoter_reward_points: int = 100,
        invitee_reward_points: int = 20,
    ) -> None:
        self._validate_config(
            trial_days,
            promoter_reward_points,
            invitee_reward_points,
        )
        self.store = store
        self.account_store = account_store
        self.points_service = PointService(store)
        self.referral_service = ReferralService(store, self.points_service)
        self.entitlement_service = EntitlementService(store)
        self.commerce_service = CommerceService(
            store,
            self.points_service,
            self.entitlement_service,
        )
        self._default_trial_days = trial_days
        self._default_promoter_points = promoter_reward_points
        self._default_invitee_points = invitee_reward_points
        self._configs: dict[str, GrowthConfigRecord] = {}

    async def initialize(self, *, at: str | None = None) -> None:
        accounts = await self.account_store.list_accounts()
        await self.recover_pending()
        grouped = self._group_accounts(accounts)
        bot_uuids = set(await self.store.list_bot_uuids()) | set(grouped)
        for bot_uuid in sorted(bot_uuids):
            config = await self._config(bot_uuid, at=at)
            bot_accounts = grouped.get(bot_uuid, ())
            if bot_accounts:
                rollout_at = await self.entitlement_service.initialize_existing_accounts(
                    bot_uuid,
                    bot_accounts,
                    trial_days=config.trial_days,
                    at=at,
                )
                for account in bot_accounts:
                    await self._initialize_bound_account(account, at=rollout_at)
            await self.commerce_service.initialize_pending_reservations(bot_uuid)

    async def promotion(self, bot_uuid: str, sender_id: str) -> str:
        if await self.account_store.get(bot_uuid, sender_id) is None:
            return '请先绑定问道账号，再查看推广信息。'
        stats = await self.referral_service.stats(bot_uuid, sender_id)
        return '\n'.join(
            (
                f'推广码：{stats.invite_code}',
                f'登记人数：{stats.registered_count}',
                f'已绑定人数：{stats.bound_count}',
                f'有效人数：{stats.effective_count}',
                f'近 7 天新增有效人数：{stats.recent_effective_count}',
            )
        )

    async def bind_referral(
        self,
        bot_uuid: str,
        sender_id: str,
        invite_code: str,
        *,
        already_bound: bool | None = None,
        at: str | None = None,
    ) -> str:
        del already_bound
        account = await self.account_store.get(bot_uuid, sender_id)
        if account is not None:
            return '当前聊天用户已绑定问道账号，不再登记邀请码。'
        try:
            relation = await self.referral_service.register_invite(
                bot_uuid,
                sender_id,
                invite_code,
                already_bound=False,
                at=at,
            )
        except ValueError as exc:
            return f'邀请码登记失败：{exc}'
        if relation.status == 'rejected':
            return '邀请码登记已记录，但该邀请关系不符合奖励条件。'
        return f'邀请码已登记：{relation.invite_code}。首次绑定并签到成功后生效。'

    async def points(self, bot_uuid: str, sender_id: str) -> str:
        balance = await self.points_service.balance(
            bot_uuid,
            identity_hash(bot_uuid, sender_id),
        )
        return f'当前积分：{balance}'

    async def shop(self, bot_uuid: str) -> str:
        products = tuple(
            item
            for item in await self.commerce_service.list_products(bot_uuid)
            if item.product.enabled
        )
        if not products:
            return '问道商城暂无上架商品。'
        lines = ['问道商城：']
        lines.extend(
            (
                f'{item.product.product_id} {item.product.name} '
                f'积分：{item.product.points_cost} '
                f'期限：{item.product.duration_days} 天 '
                f'库存：{item.available_stock}'
            )
            for item in products
        )
        return '\n'.join(lines)

    async def redeem(
        self,
        bot_uuid: str,
        sender_id: str,
        product_id: str,
        *,
        request_id: str,
    ) -> str:
        normalized_request_id = self._require_request_id(request_id)
        try:
            result = await self.commerce_service.redeem(
                bot_uuid,
                sender_id,
                product_id.strip(),
                request_id=normalized_request_id,
            )
        except ValueError as exc:
            return f'兑换失败：{exc}'
        return '\n'.join(
            (
                f'兑换成功：{result.redemption.product_id}',
                f'扣除积分：{result.redemption.points_cost}',
                f'卡密：{result.card_code}',
                '请妥善保管；未激活前可通过“问道兑换记录”再次查看。',
            )
        )

    async def redemptions(self, bot_uuid: str, sender_id: str) -> str:
        records = await self.commerce_service.list_redemptions(bot_uuid, sender_id)
        if not records:
            return '暂无兑换记录。'
        lines = ['兑换记录：']
        for item in records:
            state = '待激活' if item.status == 'ISSUED' else '已激活'
            line = (
                f'{item.redemption.product_id} '
                f'{item.redemption.duration_days} 天 {state}'
            )
            if item.card_code:
                line += f' 卡密：{item.card_code}'
            lines.append(line)
        return '\n'.join(lines)

    async def activate(
        self,
        bot_uuid: str,
        sender_id: str,
        card_code: str,
        *,
        at: str | None = None,
    ) -> str:
        try:
            result = await self.commerce_service.activate(
                bot_uuid,
                sender_id,
                card_code,
                at=at,
            )
        except ValueError as exc:
            return f'激活失败：{exc}'
        label = '卡密已激活' if result.already_activated else '卡密激活成功'
        return f'{label}，权益到期时间：{result.expires_at}'

    async def entitlement(
        self,
        bot_uuid: str,
        sender_id: str,
        *,
        now: str | None = None,
    ) -> str:
        try:
            status = await self.entitlement_service.get_status(
                bot_uuid,
                sender_id,
                now=now,
            )
        except EntitlementNotFoundError:
            return '尚未获得问道权益，请先绑定问道账号。'
        except EntitlementStorageError:
            return '权益读取失败，请稍后重试。'
        state = '有效' if status.active else '已到期'
        return f'问道权益：{state}\n到期时间：{status.expires_at}'

    async def admin(
        self,
        bot_uuid: str,
        command: str,
        *,
        request_id: str = '',
    ) -> str:
        text = command.strip()
        action = text.split(maxsplit=1)[0] if text else ''
        normalized_request_id = ''
        if action in _ADMIN_MUTATIONS:
            normalized_request_id = self._require_request_id(request_id)
        try:
            if action == '商品新增':
                return await self._admin_create_product(
                    bot_uuid,
                    text.removeprefix(action).strip(),
                    normalized_request_id,
                )
            if action in {'商品上架', '商品下架'}:
                return await self._admin_set_product_enabled(
                    bot_uuid,
                    text.removeprefix(action).strip(),
                    enabled=action == '商品上架',
                    request_id=normalized_request_id,
                )
            if action == '库存增加':
                return await self._admin_add_inventory(
                    bot_uuid,
                    text.removeprefix(action).strip(),
                    normalized_request_id,
                )
            if action == '商品列表':
                return await self._admin_products(bot_uuid)
            if action == '积分规则':
                return await self._admin_reward_rules(
                    bot_uuid,
                    text.removeprefix(action).strip(),
                    normalized_request_id,
                )
            if action == '积分调整':
                return await self._admin_adjust_points(
                    bot_uuid,
                    text.removeprefix(action).strip(),
                    normalized_request_id,
                )
            if action == '统计':
                return await self._admin_stats(bot_uuid)
        except ValueError as exc:
            return f'管理操作失败：{exc}'
        return (
            '管理命令格式错误。支持：商品新增、商品上架、商品下架、'
            '库存增加、商品列表、积分规则、积分调整、统计。'
        )

    async def on_account_bound(
        self,
        account: AccountRecord,
        *,
        at: str | None = None,
    ) -> None:
        config = await self._config(account.bot_uuid, at=at)
        await self.entitlement_service.ensure_for_binding(
            account.bot_uuid,
            account.sender_id,
            trial_days=config.trial_days,
            at=at,
        )
        await self._initialize_bound_account(account, at=at)

    async def on_signin_confirmed(
        self,
        account: AccountRecord,
        *,
        at: str | None = None,
    ) -> None:
        config = await self._config(account.bot_uuid, at=at)
        await self.referral_service.on_signin_confirmed(
            account.bot_uuid,
            account.sender_id,
            promoter_reward_points=config.promoter_reward_points,
            invitee_reward_points=config.invitee_reward_points,
            at=at,
        )

    async def recover_pending(self) -> None:
        accounts = await self.account_store.list_accounts()
        grouped = self._group_accounts(accounts)
        bot_uuids = set(await self.store.list_bot_uuids()) | set(grouped)
        for bot_uuid in sorted(bot_uuids):
            pending = await self.store.list_pending_operations(bot_uuid)
            if pending.skipped_count:
                raise ValueError('增长操作日志包含损坏记录。')
            operations = sorted(pending.records, key=self._recovery_priority)
            for operation in operations:
                if operation.kind == 'entitlement-rollout':
                    config = await self._config(bot_uuid)
                    await self.entitlement_service.initialize_existing_accounts(
                        bot_uuid,
                        grouped.get(bot_uuid, ()),
                        trial_days=config.trial_days,
                    )
                elif operation.kind in {
                    'product-create',
                    'admin-product-enabled',
                    'inventory-add',
                    'redeem',
                    'activate',
                }:
                    await self.commerce_service.recover_operation(operation)
                elif operation.operation_id.startswith('referral-effective:'):
                    await self.referral_service.recover_operation(operation)
                elif operation.kind == 'points':
                    await self.points_service.recover_operation(operation)
                elif operation.kind == 'admin-reward-rules':
                    await self._recover_reward_rules(operation)
                else:
                    raise ValueError(
                        f'未知的待恢复增长操作类型：{operation.kind}'
                    )

    async def _initialize_bound_account(
        self,
        account: AccountRecord,
        *,
        at: str | None,
    ) -> None:
        await self.referral_service.ensure_promoter(
            account.bot_uuid,
            account.sender_id,
            at=at,
        )
        if account.user_identifier:
            await self.referral_service.on_account_bound(
                account.bot_uuid,
                account.sender_id,
                account.user_identifier,
                at=at,
            )

    async def _config(
        self,
        bot_uuid: str,
        *,
        at: str | None = None,
    ) -> GrowthConfigRecord:
        cached = self._configs.get(bot_uuid)
        if cached is not None:
            return cached
        async with self.store.bot_lock(bot_uuid):
            return await self._config_unlocked(bot_uuid, at=at)

    async def _config_unlocked(
        self,
        bot_uuid: str,
        *,
        at: str | None = None,
    ) -> GrowthConfigRecord:
        cached = self._configs.get(bot_uuid)
        if cached is not None:
            return cached
        key = growth_storage_key(GROWTH_CONFIG_PREFIX, bot_uuid, _CONFIG_ID)
        current = await self.store.get(key, GrowthConfigRecord)
        if current is None:
            timestamp = at or _now_iso()
            current = GrowthConfigRecord(
                bot_uuid=bot_uuid,
                config_id=_CONFIG_ID,
                trial_days=self._default_trial_days,
                promoter_reward_points=self._default_promoter_points,
                invitee_reward_points=self._default_invitee_points,
                updated_at=timestamp,
            )
            await self.store.save(key, current)
        self._validate_config(
            current.trial_days,
            current.promoter_reward_points,
            current.invitee_reward_points,
        )
        self._configs[bot_uuid] = current
        return current

    async def _admin_create_product(
        self,
        bot_uuid: str,
        argument: str,
        request_id: str,
    ) -> str:
        parts = argument.rsplit(maxsplit=2)
        if len(parts) != 3:
            raise ValueError('商品新增格式为“商品新增 <名称> <积分> <天数>”。')
        name, cost_text, days_text = parts
        points_cost = self._parse_int(cost_text, '商品积分')
        duration_days = self._parse_int(days_text, '商品天数')
        product = await self.commerce_service.create_product(
            bot_uuid,
            name,
            points_cost,
            duration_days,
            request_id=request_id,
        )
        return f'商品新增成功：{product.product_id} {product.name}'

    async def _admin_set_product_enabled(
        self,
        bot_uuid: str,
        product_id: str,
        *,
        enabled: bool,
        request_id: str,
    ) -> str:
        normalized_product_id = product_id.strip()
        await self.commerce_service.set_enabled(
            bot_uuid,
            normalized_product_id,
            enabled,
            request_id=request_id,
        )
        state = '上架' if enabled else '下架'
        return f'商品{state}成功：{normalized_product_id}'

    async def _admin_add_inventory(
        self,
        bot_uuid: str,
        argument: str,
        request_id: str,
    ) -> str:
        parts = argument.split()
        if len(parts) != 2:
            raise ValueError('库存增加格式为“库存增加 <商品ID> <数量>”。')
        quantity = self._parse_int(parts[1], '库存数量')
        result = await self.commerce_service.add_inventory(
            bot_uuid,
            parts[0],
            quantity,
            request_id=request_id,
        )
        return f'库存增加成功：{result.product_id} +{result.added_count}'

    async def _admin_products(self, bot_uuid: str) -> str:
        products = await self.commerce_service.list_products(bot_uuid)
        if not products:
            return '商品列表为空。'
        lines = ['商品列表：']
        lines.extend(
            (
                f'{item.product.product_id} {item.product.name} '
                f'{"上架" if item.product.enabled else "下架"} '
                f'积分：{item.product.points_cost} '
                f'期限：{item.product.duration_days} 天 '
                f'库存：{item.available_stock}'
            )
            for item in products
        )
        return '\n'.join(lines)

    async def _admin_reward_rules(
        self,
        bot_uuid: str,
        argument: str,
        request_id: str,
    ) -> str:
        parts = argument.split()
        if len(parts) != 2:
            raise ValueError('积分规则格式为“积分规则 <推广人积分> <受邀人积分>”。')
        promoter_points = self._parse_int(parts[0], '推广人积分')
        invitee_points = self._parse_int(parts[1], '受邀人积分')
        self._validate_rewards(promoter_points, invitee_points)
        operation_id = self._reward_rules_operation_id(request_id)
        expected: dict[str, object] = {
            'promoter_reward_points': promoter_points,
            'invitee_reward_points': invitee_points,
        }
        async with self.store.bot_lock(bot_uuid):
            await self._execute_reward_rules_unlocked(
                bot_uuid,
                operation_id,
                expected,
            )
        return f'积分规则已更新：推广人 {promoter_points}，受邀人 {invitee_points}'

    async def _admin_adjust_points(
        self,
        bot_uuid: str,
        argument: str,
        request_id: str,
    ) -> str:
        parts = argument.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError('积分调整格式为“积分调整 <用户ID> <数量> <原因>”。')
        sender_id, amount_text, reason = parts
        amount = self._parse_int(amount_text, '积分数量')
        if amount == 0:
            raise ValueError('积分调整数量不能为零。')
        normalized_reason = reason.strip()
        if not 1 <= len(normalized_reason) <= 200:
            raise ValueError('积分调整原因长度必须在 1 至 200 个字符之间。')
        operation_id = self._admin_operation_id('points-adjust', request_id)
        entry = await self.points_service.adjust(
            bot_uuid,
            identity_hash(bot_uuid, sender_id),
            amount,
            normalized_reason,
            operation_id,
        )
        return f'积分调整成功，当前余额：{entry.balance_after}'

    async def _admin_stats(self, bot_uuid: str) -> str:
        products = await self.commerce_service.list_products(bot_uuid)
        cards = await self._list_records(bot_uuid, CARD_PREFIX, CardRecord)
        referrals = await self._list_records(
            bot_uuid,
            REFERRAL_PREFIX,
            ReferralRecord,
        )
        point_accounts = await self._list_records(
            bot_uuid,
            POINT_ACCOUNT_PREFIX,
            PointAccount,
        )
        redemptions = await self._list_records(
            bot_uuid,
            REDEMPTION_PREFIX,
            RedemptionRecord,
        )
        return '\n'.join(
            (
                '增长统计：',
                f'商品：{len(products)}',
                f'可用库存：{sum(card.status == "AVAILABLE" for card in cards)}',
                f'邀请登记：{sum(item.status != "rejected" for item in referrals)}',
                f'有效邀请：{sum(item.status == "effective" for item in referrals)}',
                f'积分账户：{len(point_accounts)}',
                f'兑换记录：{len(redemptions)}',
            )
        )

    async def _execute_reward_rules_unlocked(
        self,
        bot_uuid: str,
        operation_id: str,
        payload: dict[str, object],
    ) -> None:
        operation = await self.store.get_operation(bot_uuid, operation_id)
        if operation is None:
            await self._recover_pending_reward_rules_unlocked(bot_uuid)
            candidate_timestamp = _now_iso()
            current_config = await self._config_unlocked(
                bot_uuid,
                at=candidate_timestamp,
            )
            timestamp = strictly_later_timestamp(
                candidate_timestamp,
                current_config.updated_at,
            )
            operation = await self.store.begin_operation(
                bot_uuid,
                operation_id,
                'admin-reward-rules',
                payload,
                timestamp,
            )
        else:
            self._reward_rules_payload(operation, expected=payload)
        await self._complete_reward_rules_unlocked(operation)

    async def _recover_reward_rules(self, operation: GrowthOperation) -> None:
        async with self.store.bot_lock(operation.bot_uuid):
            current = await self.store.get_operation(
                operation.bot_uuid,
                operation.operation_id,
            )
            if current is None:
                raise ValueError('管理操作审计记录不存在。')
            await self._complete_reward_rules_unlocked(current)

    async def _recover_pending_reward_rules_unlocked(self, bot_uuid: str) -> None:
        pending = await self.store.list_pending_operations(bot_uuid)
        if pending.skipped_count:
            raise ValueError('增长操作日志包含损坏记录。')
        operations = sorted(
            (
                operation
                for operation in pending.records
                if operation.kind == 'admin-reward-rules'
            ),
            key=lambda operation: _parse_time(operation.created_at),
        )
        for operation in operations:
            await self._complete_reward_rules_unlocked(operation)

    async def _complete_reward_rules_unlocked(
        self,
        operation: GrowthOperation,
    ) -> GrowthConfigRecord:
        payload = self._reward_rules_payload(operation)
        promoter_points = cast(int, payload['promoter_reward_points'])
        invitee_points = cast(int, payload['invitee_reward_points'])
        operation_time = _parse_time(operation.created_at)
        config_key = growth_storage_key(
            GROWTH_CONFIG_PREFIX,
            operation.bot_uuid,
            _CONFIG_ID,
        )
        current = self._configs.get(operation.bot_uuid)
        config_was_missing = False
        if current is None:
            current = await self.store.get(config_key, GrowthConfigRecord)
            if current is not None:
                self._validate_config(
                    current.trial_days,
                    current.promoter_reward_points,
                    current.invitee_reward_points,
                )
                self._configs[operation.bot_uuid] = current
        if current is None:
            if operation.status == 'COMMITTED':
                raise ValueError('已提交的积分规则操作缺少配置记录。')
            config_was_missing = True
            current = GrowthConfigRecord(
                bot_uuid=operation.bot_uuid,
                config_id=_CONFIG_ID,
                trial_days=self._default_trial_days,
                promoter_reward_points=self._default_promoter_points,
                invitee_reward_points=self._default_invitee_points,
                updated_at=operation.created_at,
            )
        expected = replace(
            current,
            promoter_reward_points=promoter_points,
            invitee_reward_points=invitee_points,
            updated_at=operation.created_at,
        )
        current_time = _parse_time(current.updated_at)
        superseded = current_time > operation_time
        if (
            not config_was_missing
            and current_time == operation_time
            and current != expected
        ):
            raise ValueError('积分规则事实与操作时间冲突。')
        if operation.status == 'COMMITTED':
            if not superseded and current != expected:
                raise ValueError('已提交的积分规则操作与配置不一致。')
            return expected
        if 'target-saved' in operation.applied_steps:
            if config_was_missing or (not superseded and current != expected):
                raise ValueError('积分规则操作步骤与配置不一致。')
        else:
            if not superseded and (config_was_missing or current != expected):
                await self.store.save(config_key, expected)
                self._configs[operation.bot_uuid] = expected
            operation = await self.store.mark_step_applied(
                operation.bot_uuid,
                operation.operation_id,
                'target-saved',
                operation.updated_at,
            )
        await self.store.commit_operation(
            operation.bot_uuid,
            operation.operation_id,
            operation.updated_at,
        )
        return expected

    @staticmethod
    def _reward_rules_payload(
        operation: GrowthOperation,
        *,
        expected: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if (
            operation.kind != 'admin-reward-rules'
            or operation.status not in {'PENDING', 'COMMITTED'}
            or set(operation.payload)
            != {'promoter_reward_points', 'invitee_reward_points'}
            or type(operation.payload['promoter_reward_points']) is not int
            or type(operation.payload['invitee_reward_points']) is not int
            or re.fullmatch(
                r'admin-reward-rules:[0-9a-f]{64}',
                operation.operation_id,
            )
            is None
            or tuple(operation.applied_steps) not in {(), ('target-saved',)}
            or (
                operation.status == 'COMMITTED'
                and operation.applied_steps != ('target-saved',)
            )
        ):
            raise ValueError('管理操作审计记录格式错误。')
        promoter_points = cast(int, operation.payload['promoter_reward_points'])
        invitee_points = cast(int, operation.payload['invitee_reward_points'])
        GrowthService._validate_rewards(promoter_points, invitee_points)
        if expected is not None and operation.payload != expected:
            raise ValueError('请求 ID 已用于其他管理操作。')
        return dict(operation.payload)

    async def _list_records(self, bot_uuid: str, prefix: str, record_type):
        listed = await self.store.list_prefix(f'{prefix}{bot_uuid}:', record_type)
        if listed.skipped_count:
            raise ValueError('增长统计包含损坏记录。')
        return listed.records

    @staticmethod
    def _group_accounts(
        accounts: Iterable[AccountRecord],
    ) -> dict[str, tuple[AccountRecord, ...]]:
        grouped: dict[str, list[AccountRecord]] = {}
        for account in accounts:
            grouped.setdefault(account.bot_uuid, []).append(account)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _recovery_priority(operation: GrowthOperation) -> tuple[int, datetime]:
        priorities = {
            'entitlement-rollout': -1,
            'product-create': 0,
            'admin-product-enabled': 1,
            'inventory-add': 2,
            'redeem': 2,
            'activate': 2,
            'points': 4,
            'admin-reward-rules': 4,
        }
        priority = (
            3
            if operation.operation_id.startswith('referral-effective:')
            else priorities.get(operation.kind, 5)
        )
        return priority, _parse_time(operation.created_at)

    @staticmethod
    def _reward_rules_operation_id(request_id: str) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'admin-reward-rules:{request_hash}'

    @staticmethod
    def _admin_operation_id(kind: str, request_id: str) -> str:
        request_hash = hashlib.sha256(request_id.encode('utf-8')).hexdigest()
        return f'admin-{kind}:{request_hash}'

    @staticmethod
    def _require_request_id(request_id: str) -> str:
        if type(request_id) is not str or not request_id.strip():
            raise ValueError('请求 ID 不能为空。')
        return request_id.strip()

    @staticmethod
    def _parse_int(value: str, label: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{label}必须是整数。') from exc

    @staticmethod
    def _validate_rewards(promoter_points: int, invitee_points: int) -> None:
        if (
            type(promoter_points) is not int
            or type(invitee_points) is not int
            or not 0 <= promoter_points <= _MAX_REWARD_POINTS
            or not 0 <= invitee_points <= _MAX_REWARD_POINTS
        ):
            raise ValueError('单项推广奖励必须在 0 至 1000000 之间。')

    @classmethod
    def _validate_config(
        cls,
        trial_days: int,
        promoter_points: int,
        invitee_points: int,
    ) -> None:
        if type(trial_days) is not int or trial_days < 0:
            raise ValueError('试用天数必须是非负整数。')
        cls._validate_rewards(promoter_points, invitee_points)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('增长操作时间必须包含时区。')
    return parsed
