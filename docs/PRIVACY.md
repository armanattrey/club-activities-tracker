# Privacy note

## What is stored
- Accounts: name, email, student code, campus, role, password (bcrypt hash only).
- Activity: club memberships, attendance (time, late flag, geo flag), submissions, scores and comments, event registrations and teams, certificates.
- Uploaded files (documents, photos) and generated certificate PDFs.
- Event gallery publication consent and moderation state. Gallery media is served publicly to signed-in users only after consent and approval.
- An audit log of approvals, corrections, score changes, revocations and result publication, with the acting user and the stated reason.

The phone's GPS position is used to compute a yes/no flag at check-in. The coordinates themselves are not stored.

## Where it is stored
PostgreSQL (all records) and a file volume (uploads and PDFs), both on the machine that runs the deployment. Redis holds only short-lived data (QR session cache, duplicate guard, task queue). Nothing is sent to a third party: there are no external APIs in this system.

## Who can see what
- Students: only their own attendance, submissions, scores (after the club day is closed) and certificates. Students see other students' names only inside teams of events they are registered for.
- Coordinators: their own club's members, submissions, attendance and dashboards.
- Advisors: the clubs they advise.
- Campus admins: their campus. Super admins: everything.
- Anyone, without login: the certificate verify page, which shows the holder's name, activity, club, campus, date and certificate type. It never shows student code, email or the reason for a revocation.
- Students can only access their own transcript, Duty Leave records, and certificates. Campus admins can access transcripts and Duty Leave requests for their own campus.

## Retention
Not decided. The brief sets no period, so this needs a decision from the institution. Proposal for discussion: keep records for the academic year plus a stated number of years, then delete or anonymise.

## Known gaps
- There is no endpoint to delete or deactivate a user, so a request to erase data would have to be done directly in the database.
- No HTTPS is configured. It must be put in front of the API before any real use.
- Seeded demo accounts share a known password and must be removed.
- Uploaded files are not virus-scanned (type, content signature and size are checked).
