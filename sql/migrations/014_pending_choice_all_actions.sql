alter table pending_task_choices
    drop constraint if exists pending_task_choices_action_check;

alter table pending_task_choices
    add constraint pending_task_choices_action_check
    check (action in (
        'complete_task',
        'reschedule_task',
        'select_task_for_reschedule',
        'start_task',
        'confirm_create_task_assignee',
        'paginate_tasks',
        'show_task_details',
        'cannot_do_task',
        'needs_help_task',
        'reassign_task'
    ));
