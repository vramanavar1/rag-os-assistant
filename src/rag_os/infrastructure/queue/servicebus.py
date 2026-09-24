"""Azure Service Bus adapter (keyless). Two queues = two lanes; the priority lane is drained first.

Peek-lock + automatic lock renewal; duplicate detection keyed by message_id (doc_id:version);
max-delivery-count on the queue moves poison messages to the dead-letter sub-queue.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient
from azure.servicebus.aio.management import ServiceBusAdministrationClient

from rag_os.application.ports import MessageQueue, ReceivedMessage
from rag_os.domain.errors import DependencyUnavailable
from rag_os.domain.ingestion import IngestMessage, Lane
from rag_os.infrastructure.registry import QUEUES

log = logging.getLogger(__name__)


@QUEUES.register("servicebus", description="Azure Service Bus (priority + bulk queues, DLQ, KEDA scaling).")
class ServiceBusQueue(MessageQueue):
    def __init__(self, namespace: str, queue_priority: str, queue_bulk: str, lock_renewal_s: int = 1800,
                 **_: Any) -> None:
        if not namespace:
            raise DependencyUnavailable("SERVICEBUS_NAMESPACE is not configured")
        self._cred = DefaultAzureCredential()
        self._client = ServiceBusClient(fully_qualified_namespace=namespace, credential=self._cred)
        self._admin = ServiceBusAdministrationClient(fully_qualified_namespace=namespace, credential=self._cred)
        self._names = {Lane.PRIORITY: queue_priority, Lane.BULK: queue_bulk}
        self._senders: dict[Lane, Any] = {}
        self._receivers: dict[Lane, Any] = {}
        self._renewer = AutoLockRenewer(max_lock_renewal_duration=lock_renewal_s)

    def _sender(self, lane: Lane) -> Any:
        if lane not in self._senders:
            self._senders[lane] = self._client.get_queue_sender(self._names[lane])
        return self._senders[lane]

    def _receiver(self, lane: Lane) -> Any:
        if lane not in self._receivers:
            self._receivers[lane] = self._client.get_queue_receiver(
                self._names[lane], receive_mode=ServiceBusReceiveMode.PEEK_LOCK, prefetch_count=0
            )
        return self._receivers[lane]

    async def send(self, messages: Sequence[IngestMessage]) -> None:
        by_lane: dict[Lane, list[ServiceBusMessage]] = {}
        for m in messages:
            by_lane.setdefault(m.lane, []).append(
                ServiceBusMessage(
                    m.model_dump_json(),
                    message_id=m.message_id,
                    correlation_id=m.correlation_id,
                    content_type="application/json",
                    application_properties={"source_id": m.source_id, "lane": m.lane.value},
                )
            )
        for lane, msgs in by_lane.items():
            sender = self._sender(lane)
            batch = await sender.create_message_batch()
            for sm in msgs:
                try:
                    batch.add_message(sm)
                except ValueError:  # batch full
                    await sender.send_messages(batch)
                    batch = await sender.create_message_batch()
                    batch.add_message(sm)
            if len(batch):
                await sender.send_messages(batch)

    async def receive(self, max_messages: int, wait_seconds: float) -> list[ReceivedMessage]:
        out: list[ReceivedMessage] = []
        pri = await self._receiver(Lane.PRIORITY).receive_messages(max_message_count=max_messages, max_wait_time=1)
        for sm in pri:
            out.append(self._wrap(Lane.PRIORITY, sm))
        remaining = max_messages - len(out)
        if remaining > 0:
            bulk = await self._receiver(Lane.BULK).receive_messages(
                max_message_count=remaining, max_wait_time=wait_seconds if not out else 1
            )
            for sm in bulk:
                out.append(self._wrap(Lane.BULK, sm))
        return out

    def _wrap(self, lane: Lane, sm: Any) -> ReceivedMessage:
        self._renewer.register(self._receiver(lane), sm)
        body = b"".join(bytes(b) for b in sm.body)
        return ReceivedMessage(
            message=IngestMessage.model_validate(json.loads(body)),
            delivery_count=int(sm.delivery_count or 1),
            handle=(lane, sm),
        )

    async def complete(self, msg: ReceivedMessage) -> None:
        lane, sm = msg.handle
        await self._receiver(lane).complete_message(sm)

    async def abandon(self, msg: ReceivedMessage) -> None:
        lane, sm = msg.handle
        await self._receiver(lane).abandon_message(sm)

    async def dead_letter(self, msg: ReceivedMessage, reason: str, description: str) -> None:
        lane, sm = msg.handle
        await self._receiver(lane).dead_letter_message(sm, reason=reason[:200], error_description=description[:1000])

    async def depth(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for lane, name in self._names.items():
            props = await self._admin.get_queue_runtime_properties(name)
            out[f"{lane.value}_active"] = int(props.active_message_count)
            out[f"{lane.value}_dead_letter"] = int(props.dead_letter_message_count)
        return out

    async def peek_dead_letters(self, lane: Lane, max_messages: int) -> list[IngestMessage]:
        async with self._client.get_queue_receiver(self._names[lane], sub_queue=ServiceBusSubQueue.DEAD_LETTER) as r:
            msgs = await r.peek_messages(max_message_count=max_messages)
        out = []
        for sm in msgs:
            try:
                out.append(IngestMessage.model_validate(json.loads(b"".join(bytes(b) for b in sm.body))))
            except Exception:  # noqa: S112 - skip foreign messages
                continue
        return out

    async def aclose(self) -> None:
        await self._renewer.close()
        for s in self._senders.values():
            await s.close()
        for r in self._receivers.values():
            await r.close()
        await self._client.close()
        await self._admin.close()
        await self._cred.close()
