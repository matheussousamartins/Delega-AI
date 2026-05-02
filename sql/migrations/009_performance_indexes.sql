-- company_members: PK is (company_id, user_id), so lookups by user_id alone
-- (e.g. identify_user JOIN) cannot use the PK efficiently. This index covers
-- the reverse direction used in every incoming message.
create index if not exists idx_company_members_user_id
    on company_members (user_id);

-- notification_outbox: the existing idx_notification_outbox_due covers
-- (status, next_attempt_at) for the retry job. This index covers the
-- count_failed_notifications query that scans by status='failed' + updated_at.
create index if not exists idx_notification_outbox_failed
    on notification_outbox (status, updated_at)
    where status = 'failed';

-- task_reminders: support the claim query that checks remind_at for stale
-- claims (sent_at IS NULL AND remind_at < now() - 5 minutes).
create index if not exists idx_task_reminders_stale_claims
    on task_reminders (remind_at)
    where sent_at is null;
