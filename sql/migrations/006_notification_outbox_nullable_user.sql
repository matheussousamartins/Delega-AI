alter table notification_outbox
    alter column recipient_user_id drop not null;
