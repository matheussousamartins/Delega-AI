alter table task_reminders
    add column if not exists reminder_kind text not null default 'due_soon';

do $$
begin
    alter table task_reminders
        add constraint uq_task_reminders_task_kind unique (task_id, reminder_kind);
exception
    when duplicate_object then null;
end $$;
