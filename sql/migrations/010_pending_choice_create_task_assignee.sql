alter table pending_task_choices
    drop constraint if exists pending_task_choices_action_check;

alter table pending_task_choices
    add constraint pending_task_choices_action_check
    check (action in ('complete_task', 'reschedule_task', 'confirm_create_task_assignee'));
