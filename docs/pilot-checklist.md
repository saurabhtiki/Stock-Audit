# Pilot checklist (one company, two Branches)

Tick each box during the pilot. The automatic version of steps 1–10 is
`tests/test_acceptance.py` (`uv run pytest tests\test_acceptance.py`), but these need real people,
real phones and the real server.

Example setup: company **Alpha Traders**, Branches **N1 – North** and **S1 – South**.
Ravi = Auditor in N1 and Manager in S1, Priya = User, Anil = Auditor.

## Before the pilot
- [ ] Server set up by `docs/deploy.md` (A or B); `https://…` opens with a padlock, no warning
- [ ] `check --deploy` says "no issues"; `/health/` shows `ok`
- [ ] App and Caddy start by themselves after a server reboot
- [ ] App works with the office internet cable unplugged (office path A only)
- [ ] First Admin created with `create_admin`; the tried-out steps were rehearsed in **test** first

## Acceptance criteria
1. [ ] Admin creates the users and gives **Ravi different rights in N1 and S1**. Ravi sees only what each right allows.
2. [ ] Priya creates audit "Aug26", uploads an ERP file **with errors**, sees the messages, fixes the file, uploads again (replaces the draft) and confirms.
3. [ ] A Manager sets **quantity and % tolerance** and freezes. The ERP lines can no longer be changed or re-uploaded.
4. [ ] Anil counts some lines **in the app (on a phone)** and others through the **Excel count sheet**. A few lines are left **Not counted**.
5. [ ] At **Checking Done**, queries appear only for Excess / Shortage lines (not OK, not Not counted). Counts can no longer be edited, and nobody can roll back.
6. [ ] One query goes **Open → Reply Submitted → Action Pending → Closed**, with an action owner who marks Done and a verifier who marks Verified.
7. [ ] A query left open **shows in the next audit** of that Branch, and its page shows the **item history** table.
8. [ ] The audit is **locked**, then Admin **reopens** it with a reason. Every step is in the **Activity log**.
9. [ ] The **PDF** and **Excel** downloads match what the screens show (totals, Not counted, remarks, photos ticked for the report).
10. [ ] Admin downloads a **backup zip** (database + media), and it **restores** on a test PC (`docs/environments.md`), with logins, audits and photos all there.

## Also check
- [ ] Every list (Companies, Branches, Departments, Users, Audits, Activity log, Items, Queries): filter, sort, pages, CSV holds all matching rows
- [ ] Photos from a phone camera (including iPhone HEIC) and a video play back
- [ ] Dashboard tabs show the right Branches only for a user with View reports in one Branch
- [ ] Backup reminder banner shows for Admin after 7 days without a backup

Problems found: write them down with the time, user and screen, and check the Activity log for that moment.
