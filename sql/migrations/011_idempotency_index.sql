-- Unique partial index for webhook idempotency.
-- Prevents duplicate task creation when Evolution API retries the same webhook.
-- NULL values are excluded so rows without provider_message_id are not constrained.
create unique index if not exists idx_whatsapp_messages_provider_id
    on whatsapp_messages (provider_message_id)
    where provider_message_id is not null;
