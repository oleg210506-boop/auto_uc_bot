"""Async entry point for integrations: dispatch by a persisted FunPay order ID.

Never call a raw Stars adapter to retry a timed-out purchase. The durable Engine
owns idempotency, quantities, payment checks and reconciliation. Cancellation of
an asyncio task does not cancel the underlying journaled worker operation.
"""
from __future__ import annotations
import asyncio
from .utils import BusinessError

class AsyncOrderDispatcher:
    def __init__(self, engine): self.engine=engine

    async def buy_item(self, funpay_order_id: str) -> dict:
        await asyncio.to_thread(self.engine.prepare,funpay_order_id)
        return self._row(funpay_order_id)

    async def check_order_status(self, funpay_order_id: str) -> dict:
        for batch in self.engine.db.rows('SELECT id FROM batches WHERE order_id=?',(funpay_order_id,)):
            await asyncio.to_thread(self.engine.poll_batch,batch['id'])
        return self._row(funpay_order_id)

    async def get_balance(self) -> int:
        """Return USD millionths, not RUB cents or binary floating point."""
        return await asyncio.to_thread(self.engine.read_fazer_wallet)

    def _row(self, oid):
        row=self.engine.db.one('SELECT id,service,quantity,uc,delivered,state FROM orders WHERE id=?',(oid,))
        if not row:raise BusinessError('Заказ не найден')
        return row
