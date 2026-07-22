# ClassPurse — API Specification

_Companion to: ClassPurse Implementation Guide_


## Overview

ClassPurse is a departmental contribution collection and verification system. This document is the full endpoint-level API specification referenced throughout the Implementation Guide.

**Sheets in this document:**

1. Auth — registration, login, token refresh
2. Schools & Departments — seeded reference data
3. Reps — onboarding, invite link management
4. Students — onboarding via invite, profile, cross-purse view
5. Purses — creation, editing, transparency views
6. Contributions & Payments — invoice generation, Monnify integration points (pay-in)
7. Settlement & Payouts — settlement account verification, payout request/approval (pay-out)
8. Webhooks — inbound Monnify events (collections and transfers)
9. Audit Log — read-only, immutable, role-scoped endpoints. No edit/delete endpoint exists anywhere in this spec, by design.
10. Admin & Reconciliation — manual reconciliation trigger, cross-department oversight

## Conventions

- **Auth column:** `Public` (no token), `Rep` (rep-role JWT), `Student` (student-role JWT), `Admin` (admin-only), `Webhook` (verified via provider signature, not user auth).
- All authenticated endpoints expect `Authorization: Bearer <token>` unless marked `Public` or `Webhook`.
- All request/response bodies are JSON unless noted otherwise.
- Status codes follow standard REST conventions: 200/201 success, 400 validation error, 401/403 auth error, 404 not found, 409 conflict (e.g. duplicate invite redemption), 422 business-rule violation (e.g. purse closed).

---

## Authentication Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| POST | /auth/register | Public | { email, password, role: 'rep'\|'student' } | { id, email, role, verification_required: true } | Creates a base user account. Role-specific profile (CourseRep/Student) is created separately via onboarding or invite redemption. | Duplicate email returns 409. Role is immutable after creation. |
| POST | /auth/verify-email | Public | { token } | { verified: true } | Confirms email ownership via a signed token sent on registration. | Expired token returns 400 with a 'resend verification' hint. |
| POST | /auth/login | Public | { email, password } | { access_token, refresh_token, role } | Standard credential login. | Unverified email may still log in but is blocked from creating purses/paying until verified, per business rule. |
| POST | /auth/refresh-token | Public | { refresh_token } | { access_token } | Issues a new short-lived access token. | Revoked/expired refresh tokens return 401, forcing re-login. |
| POST | /auth/forgot-password | Public | { email } | { message: 'reset link sent if account exists' } | Triggers a SendByte email with a reset link. | Always returns 200 regardless of whether the email exists, to avoid account enumeration. |
| POST | /auth/reset-password | Public | { token, new_password } | { message: 'password updated' } | Completes the reset flow. | Token is single-use; reused tokens return 400. |

## Schools & Departments (Reference Data)

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| GET | /schools | Public | — (optional ?q=search) | [{ id, name, short_code }] | Lists all seeded schools for the sign-up dropdown. | No free-text school creation by end users — prevents duplicate/typo schools. |
| GET | /schools/{school_id}/departments | Public | — | [{ id, name, short_code }] | Lists departments under a school. | Empty list is valid — frontend should offer a 'request my department' path that notifies admin. |
| POST | /admin/schools | Admin | { name, short_code } | { id, name, short_code } | Adds a new school to the reference list. | short_code uniqueness enforced at DB level. |
| POST | /admin/departments | Admin | { school_id, name, short_code } | { id, school_id, name, short_code } | Adds a new department under a school. | Rejects if school_id does not exist (404). |
| PATCH | /admin/schools/{id} | Admin | { name?, short_code?, active? } | { id, name, short_code, active } | Edits or deactivates a school. | Deactivating a school does not delete existing reps/students/purses tied to it — soft-disable only. |

## Course Rep Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| POST | /reps/onboard | Rep | { school_id, department_id, level } | { id, department_id, level, is_active_rep: true } | Completes rep profile after base registration: selects school, department, level. | A user may hold rep status for only one department/level at a time in v1. |
| GET | /reps/me | Rep | — | { id, department, level, purses_count, students_count } | Returns the rep's own profile summary. | — |
| POST | /reps/invite-links | Rep | { level, purse_id?, expires_in_days, max_uses? } | { id, token, url, expires_at } | Generates a signed invite link scoped to the rep's department + level, optionally tied to one purse. | purse_id omitted = general department join link; provided = purse-specific link. |
| GET | /reps/invite-links | Rep | — | [{ id, url, expires_at, used_count, max_uses, active }] | Lists all invite links the rep has created. | — |
| DELETE | /reps/invite-links/{id} | Rep | — | { revoked: true } | Revokes an invite link immediately. | Already-redeemed uses are unaffected; only future redemptions are blocked. |
| GET | /reps/students | Rep | — (optional ?level=) | [{ id, name, matric_number, level, invite_source, joined_at }] | Lists verified students in the rep's department, with traceable join source. | — |

## Student Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| GET | /invites/{token} | Public | — | { department, level, school, purse_title? } | Resolves an invite token to display department/level (and purse, if scoped) before registration. | Expired or exhausted (max_uses reached) tokens return 410 Gone. |
| POST | /students/join/{token} | Public | { email, password, name, matric_number } | { id, department_id, level, verification_status: 'pending' } | Redeems an invite link: creates the base user (if new) and the Student profile, locked to the token's department/level. | matric_number format validated against the school's configured regex where available; mismatch returns 400 with the expected format. |
| GET | /students/me | Student | — | { id, department, level, verification_status } | Returns the student's own profile. | — |
| PATCH | /students/me | Student | { name?, matric_number? } | { id, name, matric_number } | Updates limited profile fields. | department/level are not editable directly — changing department requires a new invite redemption, logged as a distinct event. |
| GET | /students/me/purses | Student | — | [{ purse_id, title, amount, deadline, contribution_status }] | Lists every purse the student is enrolled in, past and present — the cross-purse transparency view. | Ordered by deadline ascending; closed/archived purses remain visible for history. |

## Purse Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| POST | /purses | Rep | { title, amount, deadline, level, enroll_mode: 'snapshot'\|'auto_enroll' } | { id, title, amount, deadline, status: 'open' } | Creates a purse for the rep's department at the given level. Generates one pending Contribution per eligible student (snapshot mode) or marks the purse for ongoing enrollment (auto_enroll mode). | amount must be > 0; deadline must be in the future. enroll_mode is immutable after creation. |
| GET | /purses | Rep / Student | — (optional ?status=open\|closed) | [{ id, title, amount, deadline, status, paid_count, total_count }] | Rep: lists purses they created. Student: lists purses they're eligible for. | Response shape differs slightly by role — students never see other students' payment details in the list view. |
| GET | /purses/{id} | Rep / Student | — | { id, title, amount, deadline, status, enroll_mode } | Returns full purse detail. | Student view includes their own contribution_status; rep view includes aggregate stats. |
| PATCH | /purses/{id} | Rep | { amount?, deadline? } | { id, amount, deadline } | Edits an open purse's amount or deadline. | Already-paid Contributions retain their original amount_expected — only pending Contributions inherit the new amount. |
| POST | /purses/{id}/close | Rep | — | { id, status: 'closed' } | Manually closes a purse before or at its deadline. | Pending contributions remain visible as 'unpaid — purse closed', not deleted. |
| GET | /purses/{id}/contributions | Rep | — (optional ?status=) | [{ student_id, name, matric_number, status, amount_received, paid_at }] | The core transparency view: every student's contribution status for this purse. | This is the endpoint the 'who has contributed' dashboard is built on. |
| GET | /purses/{id}/summary | Rep | — | { paid_count, pending_count, expired_count, flagged_count, total_collected, percent_complete } | Aggregate stats for a single purse. | total_collected sums only status=paid and status=paid_manual contributions. |

## Contribution & Monnify Payment Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| GET | /contributions/{id} | Rep / Student | — | { id, purse_id, student_id, status, amount_expected, amount_received, account_number, invoice_expires_at } | Returns full contribution detail including current active payment account, if any. | Student can only fetch their own contribution; rep can fetch any within their department's purses. |
| POST | /contributions/{id}/generate-invoice | Student | — | { account_number, bank_name, amount, expires_at } | Calls Monnify's Create Invoice API to generate a fresh dynamic virtual account for this contribution. Called on first view, and again after expiry. | If an unexpired invoice already exists, returns the existing one rather than creating a duplicate. |
| POST | /contributions/{id}/mark-manual | Rep | { amount_received, note } | { id, status: 'paid_manual' } | Logs an offline/cash payment against a contribution. | Always stored as paid_manual, never merged into the webhook-confirmed paid status — preserves audit integrity. |
| GET | /contributions/{id}/history | Rep / Student | — | [{ from_status, to_status, actor_type, actor_id, note, created_at }] | Full state-transition audit trail for a single contribution. | actor_type distinguishes webhook / reconciliation_job / rep_manual so disputes can be traced precisely. |
| POST | /contributions/{id}/resolve-flag | Rep | { resolution: 'accept_partial'\|'request_topup'\|'refund' } | { id, status } | Resolves a flagged_for_review contribution (under/over-payment). | 'refund' triggers a Monnify disbursement/transfer call and is itself logged as a distinct event. |

## Inbound Webhook Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| POST | /webhooks/monnify | Webhook | Monnify event payload (transaction completion, invoice expiry, refund, etc.) | { received: true } (202 Accepted) | Receives all Monnify webhook events. Verifies signature/hash, logs the raw event, then enqueues async processing. | Must respond quickly (<2s) to avoid Monnify retry storms; heavy matching logic happens in a background worker, not inline. |
| — (internal) | Async worker: process_webhook_event | — | WebhookEvent row | — | Matches the event's account/invoice reference to a Contribution, applies the state transition, triggers SendByte email. | Idempotent by provider_event_id — reprocessing the same event is a safe no-op. |
| POST | /webhooks/monnify/transfers | Webhook | Monnify disbursement/transfer status payload | { received: true } (202 Accepted) | Receives transfer completion/failure callbacks for Payouts. Verifies signature, logs raw event, updates the matching Payout's status. | A failed transfer must never silently reduce the purse's available balance — the funds never left, so they remain available to retry. |

## Settlement Account & Payout Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| POST | /departments/{id}/settlement-account/lookup | Rep | { bank_code, account_number } | { account_name, bank_code, account_number } | Calls Monnify's account-name-verification endpoint to resolve the real account holder name for a bank/account number, without saving anything yet. | This is a preview step — nothing is persisted until the rep explicitly confirms the resolved name in the next call. |
| POST | /departments/{id}/settlement-account | Rep | { bank_code, account_number, confirmed_account_name } | { id, bank_name, account_number, account_name_verified: true, verified_at } | Saves the department's settlement account, but only if confirmed_account_name matches the name Monnify resolved in the lookup step. | Mismatch or unverified name returns 422 and nothing is saved. There is no override path for this check. |
| GET | /departments/{id}/settlement-account | Rep | — | { id, bank_name, account_number (masked), account_name_verified, verified_at } | Returns the department's current settlement account, with the account number masked in the response. | — |
| GET | /purses/{id}/available-balance | Rep | — | { purse_id, collected_total, paid_out_total, available_balance } | Computes the live withdrawable balance for a purse from ledger events — never a stored, editable number. | Pending (not-yet-approved) payout requests are subtracted here too, preventing double-spend across two simultaneous requests. |
| POST | /payouts | Rep | { department_id, purse_id (nullable), amount } | { id, status: 'requested', amount } | Requests a payout against one purse's balance, or a sweep across the department's total available balance if purse_id is omitted. | Rejected with 422 if amount exceeds the computed available balance at request time. |
| GET | /payouts | Rep / Admin | — (optional ?status=) | [{ id, department, purse_id, amount, status, requested_by, created_at }] | Rep: lists their department's payouts. Admin: lists all payouts platform-wide, for the approval queue. | — |
| POST | /payouts/{id}/approve | Admin | — | { id, status: 'approved', approved_by } | Approves a requested payout. Does not itself move money — triggers the Monnify Single Transfer call as a subsequent step. | A payout can only be approved once; re-approval attempts return 409. |
| POST | /payouts/{id}/reject | Admin | { reason } | { id, status: 'rejected', reason } | Rejects a requested payout without moving money. | Rejected payouts do not consume the purse's available balance — the rep may submit a corrected request. |
| GET | /payouts/{id} | Rep / Admin | — | { id, status, amount, monnify_transfer_ref, failure_reason? } | Returns full detail and current status of a single payout. | — |

## Audit Log Endpoints (Read-Only by Design)

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| GET | /audit/contributions/{contribution_id} | Rep / Student | — | [{ from_status, to_status, actor_type, actor_id, note, created_at }] | Full immutable history for a single contribution. | Student may only fetch their own; rep may fetch any within their department's purses. |
| GET | /audit/purses/{purse_id} | Rep | — | [{ entity_type, action, actor_type, actor_id, before_state, after_state, created_at }] | Full immutable history for a purse: creation, edits, closures, and every contribution and payout event tied to it. | This is the endpoint a treasury dispute gets resolved against. |
| GET | /audit/payouts/{payout_id} | Rep / Admin | — | [{ from_status, to_status, actor_type, actor_id, created_at }] | Full immutable history for a single payout, from request through approval to completion or failure. | — |
| GET | /audit/departments/{department_id} | Admin | — (optional ?from=&to=) | [{ entity_type, entity_id, action, actor_type, actor_id, created_at }] | Cross-entity audit feed for an entire department — admin oversight view. | — |
| — | No PATCH, PUT, or DELETE endpoint exists for any audit record, anywhere in this specification. | — | — | — | This is a deliberate omission, not an oversight. Enforced additionally at the database level: the application's DB role has UPDATE and DELETE explicitly revoked on all audit tables. | A correction to a mistaken record is made by writing a new, subsequent event — the original entry is never altered or removed. |

## Admin & Reconciliation Endpoints

| Method | Endpoint | Auth | Request Body | Response (200/201) | Description | Key Edge Cases |
| --- | --- | --- | --- | --- | --- | --- |
| GET | /admin/webhook-events | Admin | — (optional ?processed=false) | [{ id, provider_event_id, signature_valid, processed, received_at }] | Audit log of every inbound webhook, processed or not. | Unprocessed events past a time threshold indicate a stuck worker — an alerting candidate. |
| POST | /admin/reconciliation/run | Admin | — (optional { purse_id }) | { checked: N, updated: M } | Manually triggers the reconciliation job outside its normal schedule (e.g. after an incident). | Safe to run repeatedly — only pending/expired contributions past the safe threshold are re-queried. |
| GET | /admin/contributions/flagged | Admin | — | [{ id, purse_id, student_id, amount_expected, amount_received }] | Cross-department view of every contribution awaiting rep resolution. | Useful for spotting a rep who is not resolving flags in a timely way. |
