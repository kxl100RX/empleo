-- Ejecutar esto UNA vez en Supabase: proyecto > SQL Editor > New query > pegar > Run

create extension if not exists pgcrypto;

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    email text not null unique,
    keywords text[] not null default '{}',
    skills text[] not null default '{}',
    areas text[] not null default '{}',
    cv_text text,
    created_at timestamptz default now(),
    active boolean default true
  );

create table if not exists sent_jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    job_link text not null,
    sent_at timestamptz default now(),
    unique(user_id, job_link)
  );

alter table users enable row level security;

create policy "cualquiera puede registrarse"
  on users for insert
  to anon
  with check (true);
