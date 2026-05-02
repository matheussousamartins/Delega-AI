insert into companies (id, name)
values ('00000000-0000-0000-0000-000000000001', 'Empresa Demo')
on conflict (id) do nothing;

insert into users (id, name, phone)
values
    ('00000000-0000-0000-0000-000000000101', 'Ana', '+5511999999999'),
    ('00000000-0000-0000-0000-000000000102', 'Joao', '+5511988888888'),
    ('00000000-0000-0000-0000-000000000103', 'Maria', '+5511977777777')
on conflict (phone) do nothing;

insert into company_members (company_id, user_id, role)
values
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000101', 'owner'),
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000102', 'member'),
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000103', 'member')
on conflict (company_id, user_id) do nothing;
