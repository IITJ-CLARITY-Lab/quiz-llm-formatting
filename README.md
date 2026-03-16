# Quiz LLM Studio

![Sample watermarked quiz output](medium_assets/user_attached_quiz_e_highlight_cropped.png)

Teacher-facing quiz image generation with admin approval, usage oversight, and persistent history.

## What changed

- Teacher signup now creates a `pending` account that must be approved by the admin.
- Admin pages cover dashboard metrics, approvals, user IDs, disable/delete controls, and workspace impersonation.
- Quiz requests are stored in Postgres so teachers and admins can review them later.
- The stack runs as a two-service Docker Compose package: Streamlit app + Postgres database.

## Start

```bash
docker compose up --build
```

The app will be available at [http://localhost:11001](http://localhost:11001).

## Bootstrap admin

The seed admin now follows the same default values used in the sibling ClassCam project:

- Email: `m25ai2043@iitj.ac.in`
- Password: `1312`

If an account with that email already exists, the app skips reseeding even if the password was changed later.

## Notes

- “View as user” is implemented as an explicit admin action in the Users page. Browser right-click menus are not controllable from Streamlit.
- Quiz history stores the question content and uploaded images inside Postgres so requests can be re-rendered later.
- OTP email delivery uses the same sender defaults that existed in ClassCam commit `3dc8e00`, while still allowing overrides through environment variables.
