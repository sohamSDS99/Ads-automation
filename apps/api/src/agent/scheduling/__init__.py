"""Unattended work: recurring runs, the stale-run reaper, SLA reminders, backups.

Everything in here runs on the worker's arq cron and has no signed-in user
behind it. That is the defining constraint, and it shows up in three places:
`Run.triggered_by` is NULL, `AuditLog.actor_id` is NULL (PRD §15 NF5c allows it
for exactly this case), and nothing may raise — a job that throws takes the next
tick's work down with it.
"""
