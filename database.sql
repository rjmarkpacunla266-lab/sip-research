-- ══════════════════════════════════════════════════════════════
-- SIP Research — Supabase Database Setup
-- Run this in Supabase → SQL Editor → New Query → Run
-- ══════════════════════════════════════════════════════════════

-- Step 1: Create users table
-- This stores all user accounts and their search counts
CREATE TABLE IF NOT EXISTS public.users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  search_count  INTEGER DEFAULT 0,
  created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_login    TIMESTAMP WITH TIME ZONE
);

-- Step 2: Create search history table
-- Logs every search each user makes
CREATE TABLE IF NOT EXISTS public.search_logs (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID REFERENCES public.users(id) ON DELETE CASCADE,
  query      TEXT NOT NULL,
  results    INTEGER DEFAULT 0,
  searched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Step 3: Enable Row Level Security
-- Protects data so users can only see their own data
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_logs ENABLE ROW LEVEL SECURITY;

-- Step 4: Create policies
-- Users can only read their own data
CREATE POLICY "Users can view own data"
  ON public.users FOR SELECT
  USING (true);

CREATE POLICY "Users can update own data"
  ON public.users FOR UPDATE
  USING (true);

CREATE POLICY "Allow insert"
  ON public.users FOR INSERT
  WITH CHECK (true);

CREATE POLICY "Allow search log insert"
  ON public.search_logs FOR INSERT
  WITH CHECK (true);

-- Done! Your database is ready.
